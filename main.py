import asyncio
import aiohttp
import json
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import sys

if sys.platform == "win32":
    import ctypes

DATA_FILE = "/data/domain_data.json"
LOG_FILE = "/data/amp_changes.log"
CHECK_INTERVAL = 600  # 10 menit

# Beberapa User-Agent untuk rotasi agar tidak diblokir
USER_AGENTS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; Pixel 3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
]


# =====================
# DOMAIN NORMALIZER
# =====================
def normalize_domain(input_domain):
    input_domain = input_domain.strip()
    if not input_domain.startswith("http"):
        request_url = "https://" + input_domain
    else:
        request_url = input_domain
    parsed = urlparse(request_url)
    clean_domain = parsed.netloc
    return request_url, clean_domain


def get_display_url(url):
    if not url:
        return "-"
    parsed = urlparse(url)
    domain = parsed.netloc
    path = parsed.path or ""
    return f"{domain}{path}"


# =====================
# FILE HANDLER
# =====================
def load_data():
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except:
        return {}


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def write_log(message):
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{datetime.now()}] {message}\n")
    except:
        pass


# =====================
# DOMAIN STATUS CHECKER
# =====================
async def check_domain_status(url):
    """
    Cek status HTTP domain.
    Return dict: { status_code, ok, error, redirect_url }
    """
    import random
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    result = {
        "status_code": None,
        "ok": False,
        "error": None,
        "redirect_url": None,
    }
    try:
        connector = aiohttp.TCPConnector(ssl=False)
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
            headers=headers,
            connector=connector,
            timeout=timeout
        ) as session:
            async with session.get(url, allow_redirects=True, max_redirects=10) as resp:
                result["status_code"] = resp.status
                result["ok"] = resp.status < 400
                if str(resp.url) != url:
                    result["redirect_url"] = str(resp.url)
    except aiohttp.ClientConnectorError:
        result["error"] = "Koneksi gagal / domain tidak bisa diakses"
    except aiohttp.ClientSSLError:
        result["error"] = "SSL Error"
    except asyncio.TimeoutError:
        result["error"] = "Timeout (server tidak merespons)"
    except Exception as e:
        result["error"] = f"Error: {str(e)[:80]}"
    return result


# =====================
# AMP CHECKER (AKURAT)
# Retry 3x dengan User-Agent berbeda
# =====================
async def get_amp_url(domain, retries=3, delay=3):
    """
    Cek AMP URL dengan retry 3x.
    Hanya return None jika semua percobaan gagal menemukan AMP.
    Return "ERROR" jika domain tidak bisa diakses sama sekali.
    """
    import random

    last_status = None

    for attempt in range(retries):
        headers = {
            "User-Agent": USER_AGENTS[attempt % len(USER_AGENTS)],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        try:
            connector = aiohttp.TCPConnector(ssl=False)
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(
                headers=headers,
                connector=connector,
                timeout=timeout
            ) as session:
                async with session.get(domain, allow_redirects=True, max_redirects=10) as resp:
                    last_status = resp.status

                    # Jika server error (5xx), skip dan coba lagi
                    if resp.status >= 500:
                        write_log(f"[RETRY {attempt+1}] {domain} -> HTTP {resp.status}")
                        await asyncio.sleep(delay)
                        continue

                    # Jika client error (4xx), domain bermasalah
                    if resp.status >= 400:
                        write_log(f"[ERROR] {domain} -> HTTP {resp.status}")
                        return "HTTP_ERROR"

                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    amp = soup.find("link", rel="amphtml")

                    if amp and amp.get("href"):
                        return amp["href"]

                    # AMP tidak ditemukan di attempt ini, coba lagi
                    write_log(f"[NO AMP attempt {attempt+1}] {domain}")
                    await asyncio.sleep(delay)

        except asyncio.TimeoutError:
            write_log(f"[TIMEOUT attempt {attempt+1}] {domain}")
            await asyncio.sleep(delay)
        except aiohttp.ClientConnectorError:
            write_log(f"[CONN ERROR attempt {attempt+1}] {domain}")
            await asyncio.sleep(delay)
        except Exception as e:
            write_log(f"[EXCEPTION attempt {attempt+1}] {domain} -> {e}")
            await asyncio.sleep(delay)

    # Semua retry habis, return berdasarkan last status
    if last_status is None:
        return "CONN_ERROR"  # Tidak bisa konek sama sekali
    return None  # Bisa konek tapi AMP memang tidak ada


# =====================
# COMMAND TAMBAH
# =====================
async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /tambah example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id

    await update.message.reply_text(f"Mengecek domain `{get_display_url(request_url)}`...", parse_mode="Markdown")

    # Cek status domain dulu
    status = await check_domain_status(request_url)
    if not status["ok"]:
        err = status["error"] or f"HTTP {status['status_code']}"
        await update.message.reply_text(
            f"*Domain tidak bisa diakses!*\n"
            f"Error: `{err}`\n"
            f"Pastikan domain aktif sebelum ditambahkan.",
            parse_mode="Markdown"
        )
        return

    amp_url = await get_amp_url(request_url)

    # Jangan simpan jika hasil error koneksi
    if amp_url in ("CONN_ERROR", "HTTP_ERROR"):
        await update.message.reply_text(
            "Gagal mengambil data AMP karena koneksi bermasalah. Coba lagi nanti.",
            parse_mode="Markdown"
        )
        return

    data = load_data()
    data[request_url] = {
        "initial_amp": amp_url,
        "current_amp": amp_url,
        "last_checked": str(datetime.now()),
        "chat_id": chat_id,
        "change_notified_count": 0,
        "consecutive_no_amp": 0,   # Counter false positive
        "last_http_status": status["status_code"],
    }
    save_data(data)

    amp_display = get_display_url(amp_url) if amp_url else "Tidak ada AMP"

    await update.message.reply_text(
        "✅ *DOMAIN DITAMBAHKAN*\n"
        "────────────────────\n"
        f"Domain   : `{get_display_url(request_url)}`\n"
        f"HTTP     : `{status['status_code']}`\n"
        f"AMP Awal : `{amp_display}`\n"
        "────────────────────",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# COMMAND HAPUS
# =====================
async def hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /hapus example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    data = load_data()

    if request_url in data:
        del data[request_url]
        save_data(data)
        await update.message.reply_text(
            f"Domain Dihapus\n────────────────────\n`{get_display_url(request_url)}`",
            disable_web_page_preview=True,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("Domain tidak ditemukan.")


# =====================
# COMMAND LIST
# =====================
async def list_domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = load_data()
    domains = [d for d, info in data.items() if info.get("chat_id") == chat_id]

    if not domains:
        await update.message.reply_text("Belum ada domain tersimpan.")
        return

    msg = ["*DAFTAR DOMAIN MONITORING*\n"]
    for d in domains:
        info = data[d]
        amp_now = info.get("current_amp")
        amp_display = get_display_url(amp_now) if amp_now and amp_now not in ("CONN_ERROR", "HTTP_ERROR") else f"Error ({amp_now})"
        msg.append(
            "────────────────────\n"
            f"`{get_display_url(d)}`\n"
            f"AMP Awal     : `{get_display_url(info.get('initial_amp'))}`\n"
            f"AMP Sekarang : `{amp_display}`\n"
            f"HTTP Status  : `{info.get('last_http_status', '-')}`\n"
            f"Last Check   : {info.get('last_checked', '-')}"
        )

    await update.message.reply_text(
        "\n".join(msg),
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# COMMAND CEK
# =====================
async def cek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /cek example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    await update.message.reply_text(f"Mengecek `{get_display_url(request_url)}`...", parse_mode="Markdown")

    amp = await get_amp_url(request_url)
    status = await check_domain_status(request_url)

    if amp == "CONN_ERROR":
        amp_text = "Tidak bisa konek ke domain"
    elif amp == "HTTP_ERROR":
        amp_text = f"HTTP Error ({status['status_code']})"
    elif amp is None:
        amp_text = "Tidak ditemukan (AMP tidak ada)"
    else:
        amp_text = get_display_url(amp)

    http_text = str(status["status_code"]) if status["status_code"] else status.get("error", "-")

    redirect_line = ""
    if status.get("redirect_url"):
        redirect_line = f"Redirect ke : `{get_display_url(status['redirect_url'])}`\n"

    await update.message.reply_text(
        "*HASIL PENGECEKAN*\n"
        "────────────────────\n"
        f"Domain      : `{get_display_url(request_url)}`\n"
        f"HTTP Status : `{http_text}`\n"
        f"{redirect_line}"
        f"AMP URL     : `{amp_text}`\n"
        "────────────────────",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# COMMAND STATUS (BARU)
# =====================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cek status HTTP domain secara detail"""
    if not context.args:
        await update.message.reply_text("Gunakan: /status example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    await update.message.reply_text(f"Mengecek status `{get_display_url(request_url)}`...", parse_mode="Markdown")

    status = await check_domain_status(request_url)

    if status["ok"]:
        kondisi = "Online / Normal"
    elif status["error"]:
        kondisi = f"Error: {status['error']}"
    else:
        kondisi = f"Bermasalah (HTTP {status['status_code']})"

    redirect_line = ""
    if status.get("redirect_url"):
        redirect_line = f"Redirect ke : `{get_display_url(status['redirect_url'])}`\n"

    await update.message.reply_text(
        "*STATUS DOMAIN*\n"
        "────────────────────\n"
        f"Domain      : `{get_display_url(request_url)}`\n"
        f"HTTP Status : `{status['status_code'] or '-'}`\n"
        f"Kondisi     : `{kondisi}`\n"
        f"{redirect_line}"
        "────────────────────",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# PERIODIC CHECK (ANTI FALSE POSITIVE)
# AMP dianggap hilang hanya jika 3x berturut-turut tidak terdeteksi
# =====================
async def periodic_check(app):
    await asyncio.sleep(10)  # Tunggu bot siap

    while True:
        data = load_data()
        updated = False

        for domain, info in data.items():
            initial_amp = info.get("initial_amp")
            current_amp = info.get("current_amp")
            notified_count = info.get("change_notified_count", 0)
            consecutive_no_amp = info.get("consecutive_no_amp", 0)

            # 1. Cek status domain dulu
            domain_status = await check_domain_status(domain)
            data[domain]["last_http_status"] = domain_status["status_code"]

            # Jika domain tidak bisa diakses sama sekali, skip pengecekan AMP
            # tapi kirim notif jika domain down
            if not domain_status["ok"]:
                err_msg = domain_status["error"] or f"HTTP {domain_status['status_code']}"

                # Hanya notif jika ini pertama kali down (tidak spam)
                if not info.get("domain_down_notified", False):
                    try:
                        await app.bot.send_message(
                            chat_id=info["chat_id"],
                            text=(
                                "*DOMAIN TIDAK BISA DIAKSES*\n"
                                "────────────────────\n"
                                f"Domain : `{get_display_url(domain)}`\n"
                                f"Error  : `{err_msg}`\n"
                                "Pengecekan AMP ditunda sampai domain kembali online.\n"
                                "────────────────────"
                            ),
                            disable_web_page_preview=True,
                            parse_mode="Markdown"
                        )
                        data[domain]["domain_down_notified"] = True
                        updated = True
                    except:
                        pass
                data[domain]["last_checked"] = str(datetime.now())
                write_log(f"[DOMAIN DOWN] {domain} -> {err_msg}")
                updated = True
                continue  # Skip cek AMP

            # Domain bisa diakses, reset flag down
            if info.get("domain_down_notified", False):
                data[domain]["domain_down_notified"] = False
                updated = True

            # 2. Cek AMP
            new_amp = await get_amp_url(domain)

            # Skip jika hasil error koneksi (jangan ubah data)
            if new_amp in ("CONN_ERROR", "HTTP_ERROR"):
                write_log(f"[SKIP] {domain} -> {new_amp}, tidak update data")
                data[domain]["last_checked"] = str(datetime.now())
                updated = True
                continue

            data[domain]["last_checked"] = str(datetime.now())

            # 3. Anti False Positive: AMP hilang harus 3x berturut-turut
            if new_amp is None and initial_amp is not None:
                consecutive_no_amp += 1
                data[domain]["consecutive_no_amp"] = consecutive_no_amp
                write_log(f"[NO AMP {consecutive_no_amp}/3] {domain}")

                # Baru alert jika sudah 3x berturut-turut tidak ada AMP
                if consecutive_no_amp >= 3:
                    if current_amp != new_amp and notified_count < 3:
                        data[domain]["current_amp"] = new_amp
                        try:
                            await app.bot.send_message(
                                chat_id=info["chat_id"],
                                text=(
                                    "*AMP TIDAK TERDETEKSI*\n"
                                    "────────────────────\n"
                                    f"Domain   : `{get_display_url(domain)}`\n"
                                    f"AMP Awal : `{get_display_url(initial_amp)}`\n"
                                    f"Status   : Tidak ditemukan (3x berturut-turut)\n"
                                    f"Notif    : {notified_count+1}/3\n"
                                    "────────────────────"
                                ),
                                disable_web_page_preview=True,
                                parse_mode="Markdown"
                            )
                            data[domain]["change_notified_count"] = notified_count + 1
                        except:
                            pass
                        updated = True
            else:
                # AMP terdeteksi, reset counter
                if consecutive_no_amp > 0:
                    write_log(f"[AMP BACK] {domain} after {consecutive_no_amp} miss(es)")
                data[domain]["consecutive_no_amp"] = 0

                # 4. Deteksi AMP berubah (bukan hilang, tapi URL beda)
                if new_amp != initial_amp and new_amp is not None:
                    if current_amp != new_amp:
                        data[domain]["current_amp"] = new_amp
                        if notified_count < 3:
                            try:
                                await app.bot.send_message(
                                    chat_id=info["chat_id"],
                                    text=(
                                        "*AMP URL BERUBAH*\n"
                                        "────────────────────\n"
                                        f"Domain    : `{get_display_url(domain)}`\n"
                                        f"AMP Awal  : `{get_display_url(initial_amp)}`\n"
                                        f"AMP Baru  : `{get_display_url(new_amp)}`\n"
                                        f"Notif     : {notified_count+1}/3\n"
                                        "────────────────────"
                                    ),
                                    disable_web_page_preview=True,
                                    parse_mode="Markdown"
                                )
                                data[domain]["change_notified_count"] = notified_count + 1
                            except:
                                pass
                        updated = True

                # 5. AMP kembali normal setelah sebelumnya bermasalah
                elif new_amp == initial_amp and current_amp != initial_amp:
                    data[domain]["current_amp"] = new_amp
                    data[domain]["change_notified_count"] = 0
                    try:
                        await app.bot.send_message(
                            chat_id=info["chat_id"],
                            text=(
                                "*AMP KEMBALI NORMAL*\n"
                                "────────────────────\n"
                                f"Domain   : `{get_display_url(domain)}`\n"
                                f"AMP Aktif : `{get_display_url(initial_amp)}`\n"
                                "────────────────────"
                            ),
                            disable_web_page_preview=True,
                            parse_mode="Markdown"
                        )
                    except:
                        pass
                    updated = True

            data[domain]["last_checked"] = str(datetime.now())

        if updated:
            save_data(data)

        await asyncio.sleep(CHECK_INTERVAL)


# =====================
# MAIN
# =====================
def main():
    TOKEN = "7779977084:AAFBkRL6TCzL1WMcyh5eM9S2BjVmYvwhqdc"

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("tambah", tambah))
    app.add_handler(CommandHandler("hapus", hapus))
    app.add_handler(CommandHandler("list", list_domains))
    app.add_handler(CommandHandler("cek", cek))
    app.add_handler(CommandHandler("status", status_cmd))  # BARU

    async def startup(app):
        app.create_task(periodic_check(app))
        # Heartbeat dihapus sesuai permintaan

    app.post_init = startup
    app.run_polling()


if __name__ == "__main__":
    main()
