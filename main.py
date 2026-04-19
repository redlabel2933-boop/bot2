import asyncio
import aiohttp
import ssl
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
LOG_FILE  = "/data/amp_changes.log"
CHECK_INTERVAL = 600

USER_AGENTS = [
    "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; Pixel 3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    # Tambahan UA fallback
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
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
    return request_url, parsed.netloc


def get_display_url(url):
    if not url:
        return "-"
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path or ''}"


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
# =====================
def make_mention(user_id, username=None, first_name="Pemilik"):
    if username:
        return f"@{username}"
    return f"[{first_name}](tg://user?id={user_id})"


# =====================
# HELPER: BUAT SSL CONTEXT
# Lebih toleran terhadap berbagai konfigurasi SSL
# =====================
def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


# =====================
# HELPER: BUAT SESSION
# =====================
def make_session(ua_index=0):
    """
    Buat aiohttp ClientSession dengan:
    - SSL toleran
    - Header lengkap mirip browser asli
    - Timeout 20 detik
    """
    headers = {
        "User-Agent"      : USER_AGENTS[ua_index % len(USER_AGENTS)],
        "Accept"          : "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language" : "en-US,en;q=0.9,id;q=0.8",
        "Accept-Encoding" : "gzip, deflate, br",
        "Connection"      : "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control"   : "max-age=0",
    }
    connector = aiohttp.TCPConnector(ssl=make_ssl_context())
    timeout   = aiohttp.ClientTimeout(total=20, connect=10)
    return aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout)


# =====================
# HELPER: BACA HTML DENGAN ENCODING AMAN
# Mencegah crash akibat encoding tidak standar
# =====================
async def safe_read_html(resp) -> str:
    """
    Baca response sebagai teks dengan fallback encoding.
    Urutan: utf-8 → iso-8859-1 → windows-1252 → latin-1
    """
    raw = await resp.read()
    for enc in ("utf-8", "iso-8859-1", "windows-1252", "latin-1"):
        try:
            return raw.decode(enc)
        except:
            continue
    return raw.decode("utf-8", errors="replace")


# =====================
# EKSTRAK STATUS DARI TITLE/H1
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
            result["code"] = int(match.group(1))
            result["text"] = candidate
            return result

    return result


# =====================
# DOMAIN STATUS CHECKER
# =====================
async def check_domain_status(url):
    result = {
        "status_code"     : None,
        "page_status_code": None,
        "page_status_text": None,
        "ok"              : False,
        "error"           : None,
        "redirect_url"    : None,
    }
    try:
        async with make_session() as session:
            async with session.get(url, allow_redirects=True, max_redirects=10) as resp:
                result["status_code"] = resp.status
                result["ok"]          = resp.status < 400

                if str(resp.url) != url:
                    result["redirect_url"] = str(resp.url)

                try:
                    html = await safe_read_html(resp)
                    ps   = extract_page_status(html)
                    result["page_status_code"] = ps["code"]
                    result["page_status_text"] = ps["text"]
                except:
                    pass

    except aiohttp.ClientConnectorError as e:
        result["error"] = f"Koneksi gagal: {str(e)[:60]}"
    except aiohttp.ClientSSLError as e:
        result["error"] = f"SSL Error: {str(e)[:60]}"
    except asyncio.TimeoutError:
        result["error"] = "Timeout (server tidak merespons)"
    except Exception as e:
        result["error"] = f"Error: {str(e)[:80]}"

    return result


# =====================
# AMP CHECKER — DIPERBAIKI
# - safe_read_html untuk encoding
# - SSL context lebih toleran
# - Return detail error, bukan langsung CONN_ERROR
# =====================
async def get_amp_url(domain, retries=3, delay=3):
    """
    Return:
      str   → AMP URL ditemukan
      None  → Domain OK tapi tidak ada AMP
      "HTTP_ERROR"  → Server balas 4xx
      "CONN_ERROR"  → Tidak bisa konek sama sekali (semua retry gagal)
    """
    last_exception = None
    last_status    = None

    for attempt in range(retries):
        try:
            async with make_session(ua_index=attempt) as session:
                async with session.get(
                    domain,
                    allow_redirects=True,
                    max_redirects=10
                ) as resp:
                    last_status = resp.status

                    # Server error 5xx → retry
                    if resp.status >= 500:
                        write_log(f"[RETRY {attempt+1}] {domain} -> HTTP {resp.status}")
                        await asyncio.sleep(delay)
                        continue

                    # Client error 4xx → stop
                    if resp.status >= 400:
                        write_log(f"[4xx ERROR] {domain} -> HTTP {resp.status}")
                        return "HTTP_ERROR"

                    # ✅ Baca HTML dengan encoding aman
                    html = await safe_read_html(resp)
                    soup = BeautifulSoup(html, "html.parser")
                    amp  = soup.find("link", rel="amphtml")

                    if amp and amp.get("href"):
                        return amp["href"].strip()

                    # Tidak ada AMP di attempt ini → coba lagi
                    write_log(f"[NO AMP attempt {attempt+1}] {domain}")
                    await asyncio.sleep(delay)

        except asyncio.TimeoutError:
            last_exception = "Timeout"
            write_log(f"[TIMEOUT attempt {attempt+1}] {domain}")
            await asyncio.sleep(delay)

        except aiohttp.ClientSSLError as e:
            # SSL error → coba fallback HTTP sekali
            last_exception = f"SSL: {e}"
            write_log(f"[SSL ERROR attempt {attempt+1}] {domain} -> {e}")

            # Coba dengan http:// jika https:// SSL error
            if domain.startswith("https://") and attempt == 0:
                http_fallback = domain.replace("https://", "http://", 1)
                write_log(f"[SSL FALLBACK] Trying http:// for {domain}")
                try:
                    async with make_session(ua_index=attempt+1) as session:
                        async with session.get(
                            http_fallback,
                            allow_redirects=True,
                            max_redirects=10
                        ) as resp2:
                            if resp2.status < 400:
                                html2 = await safe_read_html(resp2)
                                soup2 = BeautifulSoup(html2, "html.parser")
                                amp2  = soup2.find("link", rel="amphtml")
                                if amp2 and amp2.get("href"):
                                    return amp2["href"].strip()
                                last_status = resp2.status
                except:
                    pass
            await asyncio.sleep(delay)

        except aiohttp.ClientConnectorError as e:
            last_exception = f"ConnError: {e}"
            write_log(f"[CONN ERROR attempt {attempt+1}] {domain} -> {e}")
            await asyncio.sleep(delay)

        except Exception as e:
            last_exception = str(e)
            write_log(f"[EXCEPTION attempt {attempt+1}] {domain} -> {e}")
            await asyncio.sleep(delay)

    # Semua retry habis
    if last_status is not None:
        # Bisa konek, tapi AMP memang tidak ada
        return None
    # Tidak bisa konek sama sekali
    write_log(f"[CONN_ERROR FINAL] {domain} -> {last_exception}")
    return "CONN_ERROR"


# =====================
# FORMAT STATUS DISPLAY
# =====================
def format_status_display(status: dict) -> str:
    http_code  = status.get("status_code")
    page_code  = status.get("page_status_code")
    page_text  = status.get("page_status_text")
    error      = status.get("error")

    if error:
        return error
    if page_code and page_code != http_code:
        return f"{page_code} _(dari halaman: \"{page_text}\")_"
    if page_code and page_text:
        return f"{page_code} — {page_text}"
    return str(http_code) if http_code else "-"


# =====================
# COMMAND TAMBAH
# PERBAIKAN: Jika domain OK tapi CONN_ERROR saat cek AMP
#            → tetap disimpan dengan amp = None, jangan ditolak
# =====================
async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /tambah example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id
    user    = update.effective_user

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

    loading_msg2 = await update.message.reply_text(
        "⏳ Mengambil AMP URL...", parse_mode="Markdown"
    )
    amp_url = await get_amp_url(request_url)
    await safe_delete(context, chat_id, loading_msg2.message_id)

    # ✅ PERBAIKAN: HTTP_ERROR tetap ditolak,
    #    tapi CONN_ERROR → simpan dengan amp = None (domain terbukti OK dari check_domain_status)
    if amp_url == "HTTP_ERROR":
        await update.message.reply_text(
            "❌ Server menolak request (HTTP 4xx). Domain tidak bisa dipantau.",
            parse_mode="Markdown"
        )
        return

    # CONN_ERROR saat cek AMP tapi domain_status OK → simpan, amp dianggap None dulu
    conn_warning = ""
    if amp_url == "CONN_ERROR":
        amp_url      = None
        conn_warning = "\n⚠️ _AMP belum bisa diambil saat ini, akan dicoba kembali saat monitoring._"

    data = load_data()
    data[request_url] = {
        "initial_amp"          : amp_url,
        "current_amp"          : amp_url,
        "last_checked"         : str(datetime.now()),
        "chat_id"              : chat_id,
        "owner_user_id"        : user.id,
        "owner_username"       : user.username or None,
        "owner_first_name"     : user.first_name or "Pemilik",
        "change_notified_count": 0,
        "consecutive_no_amp"   : 0,
        "last_http_status"     : status["status_code"],
        "last_page_status"     : status.get("page_status_text"),
        "domain_down_notified" : False,
    }
    save_data(data)

    amp_display    = get_display_url(amp_url) if amp_url else "Tidak ada / Belum terdeteksi"
    status_display = format_status_display(status)
    mention        = make_mention(user.id, user.username, user.first_name)

    await update.message.reply_text(
        "✅ *DOMAIN DITAMBAHKAN*\n"
        "────────────────────\n"
        f"Domain   : `{get_display_url(request_url)}`\n"
        f"Status   : `{status_display}`\n"
        f"AMP Awal : `{amp_display}`\n"
        f"Pemilik  : {mention}\n"
        f"────────────────────"
        f"{conn_warning}",
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
    data    = load_data()
    domains = [d for d, info in data.items() if info.get("chat_id") == chat_id]

    if not domains:
        await update.message.reply_text("Belum ada domain tersimpan.")
        return

    msg = ["*DAFTAR DOMAIN MONITORING*\n"]
    for d in domains:
        info       = data[d]
        amp_now    = info.get("current_amp")
        amp_display = (
            get_display_url(amp_now)
            if amp_now and amp_now not in ("CONN_ERROR", "HTTP_ERROR")
            else "Error / Tidak terdeteksi"
        )
        page_status = info.get("last_page_status")
        http_status = info.get("last_http_status", "-")
        status_line = f"{http_status} — {page_status}" if page_status else str(http_status)

        owner_uid = info.get("owner_user_id")
        owner_un  = info.get("owner_username")
        owner_fn  = info.get("owner_first_name", "Pemilik")
        mention   = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else "-"

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
        amp_text = "⚠️ Tidak bisa konek (CONN_ERROR)"
    elif amp == "HTTP_ERROR":
        amp_text = "❌ HTTP Error (4xx)"
    elif amp is None:
        amp_text = "Tidak ditemukan (tidak ada amphtml)"
    else:
        amp_text = get_display_url(amp)

    status_display = format_status_display(status)
    redirect_line  = ""
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

    kondisi       = "✅ Online / Normal" if status["ok"] else (f"❌ {status['error']}" if status["error"] else "⚠️ Bermasalah")
    redirect_line = f"Redirect ke  : `{get_display_url(status['redirect_url'])}`\n" if status.get("redirect_url") else ""
    page_line     = f"Info Halaman : `{status['page_status_text']}`\n" if status.get("page_status_text") else ""

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
# COMMAND UPDATE
# =====================
async def update_amp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "Gunakan: `/update example.com`",
            parse_mode="Markdown"
        )
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id
    user    = update.effective_user
    data    = load_data()

    if request_url not in data:
        await update.message.reply_text(
            f"⚠️ Domain `{get_display_url(request_url)}` tidak ditemukan.",
            parse_mode="Markdown"
        )
        return

    info = data[request_url]
    if info.get("owner_user_id") and info["owner_user_id"] != user.id:
        await update.message.reply_text(
            "❌ Kamu bukan pemilik domain ini.",
            parse_mode="Markdown"
        )
        return

    loading_msg = await update.message.reply_text(
        f"⏳ Mengambil AMP terbaru dari `{get_display_url(request_url)}`...",
        parse_mode="Markdown"
    )

    new_amp = await get_amp_url(request_url)
    await safe_delete(context, chat_id, loading_msg.message_id)

    if new_amp == "HTTP_ERROR":
        await update.message.reply_text("❌ Server menolak request saat update AMP.", parse_mode="Markdown")
        return

    if new_amp == "CONN_ERROR":
        await update.message.reply_text("❌ Gagal konek ke domain. Coba lagi nanti.", parse_mode="Markdown")
        return

    old_amp = info.get("initial_amp")
    data[request_url].update({
        "initial_amp"          : new_amp,
        "current_amp"          : new_amp,
        "change_notified_count": 0,
        "consecutive_no_amp"   : 0,
        "last_checked"         : str(datetime.now()),
        "domain_down_notified" : False,
    })
    save_data(data)
    write_log(f"[MANUAL UPDATE] {request_url} {old_amp} -> {new_amp} by {user.id}")

    mention = make_mention(user.id, user.username, user.first_name)
    await update.message.reply_text(
        "✅ *AMP REFERENSI DIPERBARUI*\n"
        "────────────────────\n"
        f"Domain   : `{get_display_url(request_url)}`\n"
        f"AMP Lama : `{get_display_url(old_amp)}`\n"
        f"AMP Baru : `{get_display_url(new_amp) if new_amp else 'Tidak ada AMP'}`\n"
        f"Oleh     : {mention}\n"
        "────────────────────",
        disable_web_page_preview=True,
        parse_mode="Markdown"
    )


# =====================
# PERIODIC CHECK
# =====================
async def periodic_check(app):
    await asyncio.sleep(10)

    while True:
        data    = load_data()
        updated = False

        for domain, info in data.items():
            initial_amp        = info.get("initial_amp")
            current_amp        = info.get("current_amp")
            notified_count     = info.get("change_notified_count", 0)
            consecutive_no_amp = info.get("consecutive_no_amp", 0)

            owner_uid  = info.get("owner_user_id")
            owner_un   = info.get("owner_username")
            owner_fn   = info.get("owner_first_name", "Pemilik")
            mention    = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else ""
            mention_line = f"Pemilik : {mention}\n" if mention else ""

            # ── Cek status domain ──
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
                updated = True
                continue

            if info.get("domain_down_notified", False):
                data[domain]["domain_down_notified"] = False
                updated = True

            # ── Cek AMP ──
            new_amp = await get_amp_url(domain)

            if new_amp in ("CONN_ERROR", "HTTP_ERROR"):
                write_log(f"[SKIP AMP] {domain} -> {new_amp}")
                data[domain]["last_checked"] = str(datetime.now())
                updated = True
                continue

            data[domain]["last_checked"] = str(datetime.now())

            if new_amp is None and initial_amp is not None:
                consecutive_no_amp += 1
                data[domain]["consecutive_no_amp"] = consecutive_no_amp

                if consecutive_no_amp >= 3 and current_amp != new_amp and notified_count < 3:
                    data[domain]["current_amp"] = new_amp
                    try:
                        await app.bot.send_message(
                            chat_id=info["chat_id"],
                            text=(
                                "🚨 *AMP TIDAK TERDETEKSI*\n"
                                "────────────────────\n"
                                f"Domain   : `{get_display_url(domain)}`\n"
                                f"AMP Awal : `{get_display_url(initial_amp)}`\n"
                                f"Status   : Hilang 3x berturut-turut\n"
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
                data[domain]["consecutive_no_amp"] = 0

                if new_amp != initial_amp and new_amp is not None and current_amp != new_amp:
                    data[domain]["current_amp"] = new_amp
                    if notified_count < 3:
                        try:
                            await app.bot.send_message(
                                chat_id=info["chat_id"],
                                text=(
                                    "⚠️ *AMP URL BERUBAH*\n"
                                    "────────────────────\n"
                                    f"Domain   : `{get_display_url(domain)}`\n"
                                    f"AMP Lama : `{get_display_url(initial_amp)}`\n"
                                    f"AMP Baru : `{get_display_url(new_amp)}`\n"
                                    f"Notif    : {notified_count+1}/3\n"
                                    f"{mention_line}"
                                    f"Jika disengaja gunakan: `/update {get_display_url(domain)}`\n"
                                    "────────────────────"
                                ),
                                disable_web_page_preview=True,
                                parse_mode="Markdown"
                            )
                            data[domain]["change_notified_count"] = notified_count + 1
                        except:
                            pass
                    updated = True

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
    app   = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("tambah", tambah))
    app.add_handler(CommandHandler("hapus",  hapus))
    app.add_handler(CommandHandler("list",   list_domains))
    app.add_handler(CommandHandler("cek",    cek))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("update", update_amp))

    async def startup(app):
        app.create_task(periodic_check(app))

    app.post_init = startup
    app.run_polling()


if __name__ == "__main__":
    main()
