import asyncio
import aiohttp
import json
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urlparse
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
import sys
import re

if sys.platform == "win32":
    import ctypes

DATA_FILE = "/data/domain_data.json"
LOG_FILE = "/data/amp_changes.log"
CHECK_INTERVAL = 600  # 10 menit

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
# HELPER: DELETE MESSAGE SAFELY
# =====================
async def safe_delete(context, chat_id, message_id):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass


# =====================
# HELPER: MENTION USER
# Buat mention link ke user berdasarkan user_id
# Format: [Nama](tg://user?id=USER_ID)
# =====================
def make_mention(user_id: int, username: str = None, first_name: str = "Pemilik") -> str:
    """
    Return mention string untuk Telegram Markdown.
    Jika ada username → @username
    Jika tidak ada username → inline mention via tg://user?id=
    """
    if username:
        return f"@{username}"
    return f"[{first_name}](tg://user?id={user_id})"


# =====================
# HELPER: EKSTRAK STATUS DARI TITLE/H1 HALAMAN
# =====================
def extract_page_status(html: str):
    result = {"code": None, "text": None}
    if not html:
        return result

    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    title_tag = soup.find("title")
    if title_tag and title_tag.text:
        candidates.append(title_tag.text.strip())

    h1_tag = soup.find("h1")
    if h1_tag and h1_tag.text:
        candidates.append(h1_tag.text.strip())

    pattern = re.compile(r"(?:^|\s|error\s*)([3-5]\d{2})(?:\s|$)", re.IGNORECASE)
    for candidate in candidates:
        match = pattern.search(candidate)
        if match:
            code = int(match.group(1))
            result["code"] = code
            result["text"] = candidate
            return result

    return result


# =====================
# DOMAIN STATUS CHECKER
# =====================
async def check_domain_status(url):
    import random
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    result = {
        "status_code": None,
        "page_status_code": None,
        "page_status_text": None,
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

                try:
                    html = await resp.text()
                    page_status = extract_page_status(html)
                    result["page_status_code"] = page_status["code"]
                    result["page_status_text"] = page_status["text"]
                except:
                    pass

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
# AMP CHECKER
# Retry 3x dengan User-Agent berbeda
# =====================
async def get_amp_url(domain, retries=3, delay=3):
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

                    if resp.status >= 500:
                        write_log(f"[RETRY {attempt+1}] {domain} -> HTTP {resp.status}")
                        await asyncio.sleep(delay)
                        continue

                    if resp.status >= 400:
                        write_log(f"[ERROR] {domain} -> HTTP {resp.status}")
                        return "HTTP_ERROR"

                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    amp = soup.find("link", rel="amphtml")

                    if amp and amp.get("href"):
                        return amp["href"]

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

    if last_status is None:
        return "CONN_ERROR"
    return None


# =====================
# HELPER: FORMAT STATUS DISPLAY
# =====================
def format_status_display(status: dict) -> str:
    http_code = status.get("status_code")
    page_code = status.get("page_status_code")
    page_text = status.get("page_status_text")
    error = status.get("error")

    if error:
        return error
    if page_code and page_code != http_code:
        return f"{page_code} _(dari halaman: \"{page_text}\")_"
    if page_code and page_text:
        return f"{page_code} — {page_text}"
    return str(http_code) if http_code else "-"


# =====================
# COMMAND TAMBAH
# Simpan user_id, username, first_name untuk keperluan mention
# =====================
async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /tambah example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id

    # Ambil info user pengirim
    user = update.effective_user
    user_id = user.id
    username = user.username or None
    first_name = user.first_name or "Pemilik"

    loading_msg = await update.message.reply_text(
        f"⏳ Mengecek domain `{get_display_url(request_url)}`...",
        parse_mode="Markdown"
    )

    status = await check_domain_status(request_url)
    await safe_delete(context, chat_id, loading_msg.message_id)

    if not status["ok"]:
        err = status["error"] or format_status_display(status)
        await update.message.reply_text(
            "*Domain tidak bisa diakses!*\n"
            "────────────────────\n"
            f"Domain : `{get_display_url(request_url)}`\n"
            f"Status : `{err}`\n"
            "Pastikan domain aktif sebelum ditambahkan.",
            parse_mode="Markdown"
        )
        return

    loading_msg2 = await update.message.reply_text("⏳ Mengambil AMP URL...", parse_mode="Markdown")
    amp_url = await get_amp_url(request_url)
    await safe_delete(context, chat_id, loading_msg2.message_id)

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
        # ── INFO PEMILIK (untuk mention notifikasi) ──
        "owner_user_id": user_id,
        "owner_username": username,
        "owner_first_name": first_name,
        # ─────────────────────────────────────────────
        "change_notified_count": 0,
        "consecutive_no_amp": 0,
        "last_http_status": status["status_code"],
        "last_page_status": status.get("page_status_text"),
        "domain_down_notified": False,
    }
    save_data(data)

    amp_display = get_display_url(amp_url) if amp_url else "Tidak ada AMP"
    status_display = format_status_display(status)
    mention = make_mention(user_id, username, first_name)

    await update.message.reply_text(
        "✅ *DOMAIN DITAMBAHKAN*\n"
        "────────────────────\n"
        f"Domain   : `{get_display_url(request_url)}`\n"
        f"Status   : `{status_display}`\n"
        f"AMP Awal : `{amp_display}`\n"
        f"Pemilik  : {mention}\n"
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
            f"🗑 *Domain Dihapus*\n────────────────────\n`{get_display_url(request_url)}`",
            disable_web_page_preview=True,
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Domain tidak ditemukan.")


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
        amp_display = (
            get_display_url(amp_now)
            if amp_now and amp_now not in ("CONN_ERROR", "HTTP_ERROR")
            else f"Error ({amp_now})"
        )

        page_status = info.get("last_page_status")
        http_status = info.get("last_http_status", "-")
        status_line = f"{http_status} — {page_status}" if page_status else str(http_status)

        owner_uid  = info.get("owner_user_id")
        owner_un   = info.get("owner_username")
        owner_fn   = info.get("owner_first_name", "Pemilik")
        mention    = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else "-"

        msg.append(
            "────────────────────\n"
            f"`{get_display_url(d)}`\n"
            f"AMP Awal     : `{get_display_url(info.get('initial_amp'))}`\n"
            f"AMP Sekarang : `{amp_display}`\n"
            f"Status       : `{status_line}`\n"
            f"Pemilik      : {mention}\n"
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
    chat_id = update.effective_chat.id

    loading_msg = await update.message.reply_text(
        f"⏳ Mengecek `{get_display_url(request_url)}`...",
        parse_mode="Markdown"
    )

    amp, status = await asyncio.gather(
        get_amp_url(request_url),
        check_domain_status(request_url)
    )

    await safe_delete(context, chat_id, loading_msg.message_id)

    if amp == "CONN_ERROR":
        amp_text = "Tidak bisa konek ke domain"
    elif amp == "HTTP_ERROR":
        amp_text = "HTTP Error"
    elif amp is None:
        amp_text = "Tidak ditemukan"
    else:
        amp_text = get_display_url(amp)

    status_display = format_status_display(status)
    redirect_line = ""
    if status.get("redirect_url"):
        redirect_line = f"Redirect ke : `{get_display_url(status['redirect_url'])}`\n"

    await update.message.reply_text(
        "*HASIL PENGECEKAN AMP*\n"
        "────────────────────\n"
        f"Domain      : `{get_display_url(request_url)}`\n"
        f"Status      : `{status_display}`\n"
        f"{redirect_line}"
        f"AMP URL     : `{amp_text}`\n"
        "────────────────────",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# COMMAND STATUS
# =====================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /status example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id

    loading_msg = await update.message.reply_text(
        f"⏳ Mengecek status `{get_display_url(request_url)}`...",
        parse_mode="Markdown"
    )

    status = await check_domain_status(request_url)
    await safe_delete(context, chat_id, loading_msg.message_id)

    status_display = format_status_display(status)

    if status["ok"]:
        kondisi = "✅ Online / Normal"
    elif status["error"]:
        kondisi = f"❌ {status['error']}"
    else:
        kondisi = "⚠️ Bermasalah"

    redirect_line = ""
    if status.get("redirect_url"):
        redirect_line = f"Redirect ke  : `{get_display_url(status['redirect_url'])}`\n"

    page_line = ""
    if status.get("page_status_text"):
        page_line = f"Info Halaman : `{status['page_status_text']}`\n"

    await update.message.reply_text(
        "*STATUS DOMAIN*\n"
        "────────────────────\n"
        f"Domain      : `{get_display_url(request_url)}`\n"
        f"HTTP Status : `{status['status_code'] or '-'}`\n"
        f"{page_line}"
        f"{redirect_line}"
        f"Kondisi     : {kondisi}\n"
        "────────────────────",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# COMMAND UPDATE  ← BARU
# Pemilik update AMP referensi jika AMP memang sengaja diganti
# /update domain.com
# =====================
async def update_amp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Gunakan: `/update example.com`\n"
            "Perintah ini menyimpan AMP terbaru sebagai referensi baru.",
            parse_mode="Markdown"
        )
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id
    user = update.effective_user

    data = load_data()

    # Pastikan domain terdaftar & milik chat yang sama
    if request_url not in data:
        await update.message.reply_text(
            f"⚠️ Domain `{get_display_url(request_url)}` tidak ditemukan di daftar monitoring.",
            parse_mode="Markdown"
        )
        return

    info = data[request_url]

    # Validasi: hanya pemilik domain (owner_user_id) yang boleh update
    if info.get("owner_user_id") and info["owner_user_id"] != user.id:
        await update.message.reply_text(
            "❌ Kamu bukan pemilik domain ini. Hanya pemilik yang bisa melakukan update AMP.",
            parse_mode="Markdown"
        )
        return

    loading_msg = await update.message.reply_text(
        f"⏳ Mengambil AMP terbaru dari `{get_display_url(request_url)}`...",
        parse_mode="Markdown"
    )

    new_amp = await get_amp_url(request_url)
    await safe_delete(context, chat_id, loading_msg.message_id)

    # Gagal ambil AMP
    if new_amp in ("CONN_ERROR", "HTTP_ERROR"):
        await update.message.reply_text(
            "❌ Gagal mengambil AMP. Domain tidak bisa diakses, coba beberapa saat lagi.",
            parse_mode="Markdown"
        )
        return

    old_initial_amp = info.get("initial_amp")
    amp_display_old = get_display_url(old_initial_amp) if old_initial_amp else "-"
    amp_display_new = get_display_url(new_amp) if new_amp else "Tidak ada AMP"

    # Simpan AMP baru sebagai referensi awal & reset semua counter
    data[request_url]["initial_amp"]          = new_amp
    data[request_url]["current_amp"]          = new_amp
    data[request_url]["change_notified_count"] = 0
    data[request_url]["consecutive_no_amp"]    = 0
    data[request_url]["last_checked"]          = str(datetime.now())
    data[request_url]["domain_down_notified"]  = False

    save_data(data)
    write_log(f"[MANUAL UPDATE] {request_url} AMP: {old_initial_amp} -> {new_amp} by user {user.id}")

    mention = make_mention(user.id, user.username, user.first_name)

    await update.message.reply_text(
        "✅ *AMP REFERENSI DIPERBARUI*\n"
        "────────────────────\n"
        f"Domain      : `{get_display_url(request_url)}`\n"
        f"AMP Lama    : `{amp_display_old}`\n"
        f"AMP Baru    : `{amp_display_new}`\n"
        f"Diupdate    : {mention}\n"
        f"Waktu       : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "────────────────────\n"
        "Bot akan memantau AMP baru sebagai referensi.",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# PERIODIC CHECK + MENTION PEMILIK
# =====================
async def periodic_check(app):
    await asyncio.sleep(10)

    while True:
        data = load_data()
        updated = False

        for domain, info in data.items():
            initial_amp       = info.get("initial_amp")
            current_amp       = info.get("current_amp")
            notified_count    = info.get("change_notified_count", 0)
            consecutive_no_amp = info.get("consecutive_no_amp", 0)

            # Buat mention pemilik untuk notifikasi
            owner_uid = info.get("owner_user_id")
            owner_un  = info.get("owner_username")
            owner_fn  = info.get("owner_first_name", "Pemilik")
            mention   = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else ""
            mention_line = f"Pemilik : {mention}\n" if mention else ""

            # ── 1. CEK STATUS DOMAIN ──
            domain_status = await check_domain_status(domain)
            data[domain]["last_http_status"] = domain_status["status_code"]
            if domain_status.get("page_status_text"):
                data[domain]["last_page_status"] = domain_status["page_status_text"]

            if not domain_status["ok"]:
                err_msg = domain_status["error"] or format_status_display(domain_status)

                if not info.get("domain_down_notified", False):
                    try:
                        await app.bot.send_message(
                            chat_id=info["chat_id"],
                            text=(
                                "🚨 *DOMAIN TIDAK BISA DIAKSES*\n"
                                "────────────────────\n"
                                f"Domain  : `{get_display_url(domain)}`\n"
                                f"Status  : `{err_msg}`\n"
                                f"{mention_line}"
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
                continue

            # Domain kembali online, reset flag
            if info.get("domain_down_notified", False):
                data[domain]["domain_down_notified"] = False
                updated = True

            # ── 2. CEK AMP ──
            new_amp = await get_amp_url(domain)

            if new_amp in ("CONN_ERROR", "HTTP_ERROR"):
                write_log(f"[SKIP] {domain} -> {new_amp}")
                data[domain]["last_checked"] = str(datetime.now())
                updated = True
                continue

            data[domain]["last_checked"] = str(datetime.now())

            # ── 3. ANTI FALSE POSITIVE: AMP hilang 3x berturut-turut ──
            if new_amp is None and initial_amp is not None:
                consecutive_no_amp += 1
                data[domain]["consecutive_no_amp"] = consecutive_no_amp
                write_log(f"[NO AMP {consecutive_no_amp}/3] {domain}")

                if consecutive_no_amp >= 3 and current_amp != new_amp and notified_count < 3:
                    data[domain]["current_amp"] = new_amp
                    try:
                        await app.bot.send_message(
                            chat_id=info["chat_id"],
                            text=(
                                "🚨 *AMP TIDAK TERDETEKSI KEMUNGKINAN DI FIX*\n"
                                "────────────────────\n"
                                f"Domain   : `{get_display_url(domain)}`\n"
                                f"AMP Awal : `{get_display_url(initial_amp)}`\n"
                                f"Status   : Hilang (3x berturut-turut)\n"
                                f"Notif    : {notified_count+1}/3\n"
                                f"{mention_line}"
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

                # ── 4. AMP URL BERUBAH (bukan hilang) ──
                if new_amp != initial_amp and new_amp is not None and current_amp != new_amp:
                    data[domain]["current_amp"] = new_amp
                    if notified_count < 3:
                        try:
                            await app.bot.send_message(
                                chat_id=info["chat_id"],
                                text=(
                                    "⚠️ *AMP URL BERUBAH*\n"
                                    "*Segera Cek sekarang!*\n"
                                    "────────────────────\n"
                                    f"Domain   : `{get_display_url(domain)}`\n"
                                    f"AMP Lama : `{get_display_url(initial_amp)}`\n"
                                    f"AMP Baru : `{get_display_url(new_amp)}`\n"
                                    f"Notif    : {notified_count+1}/3\n"
                                    f"{mention_line}"     
                                    "────────────────────"
                                ),
                                disable_web_page_preview=True,
                                parse_mode="Markdown"
                            )
                            data[domain]["change_notified_count"] = notified_count + 1
                        except:
                            pass
                    updated = True

                # ── 5. AMP KEMBALI NORMAL ──
                elif new_amp == initial_amp and current_amp != initial_amp:
                    data[domain]["current_amp"] = new_amp
                    data[domain]["change_notified_count"] = 0
                    try:
                        await app.bot.send_message(
                            chat_id=info["chat_id"],
                            text=(
                                "✅ *AMP KEMBALI NORMAL*\n"
                                "────────────────────\n"
                                f"Domain    : `{get_display_url(domain)}`\n"
                                f"AMP Aktif : `{get_display_url(initial_amp)}`\n"
                                f"{mention_line}"
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
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("update", update_amp))   # ← BARU

    async def startup(app):
        app.create_task(periodic_check(app))

    app.post_init = startup
    app.run_polling()


if __name__ == "__main__":
    main()
