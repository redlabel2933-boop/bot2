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
    try:
        parsed = urlparse(url)
        return f"{parsed.netloc}{parsed.path or ''}"
    except:
        return str(url)


# =====================
# ESCAPE MARKDOWN
# Karakter _ * ` [ ] ( ) ~ > # + - = | { } . !
# harus di-escape agar tidak crash di Telegram MarkdownV2
# Kita pakai Markdown biasa, tapi escape backtick & underscore di URL
# =====================
def esc(text: str) -> str:
    """
    Escape karakter yang bisa merusak Markdown mode biasa.
    Khusus untuk teks yang ditaruh dalam backtick tidak perlu,
    tapi untuk teks di luar backtick perlu di-escape.
    """
    if not text:
        return "-"
    # Untuk Markdown biasa, yang berbahaya adalah _ dan *
    return str(text).replace("_", "\\_").replace("*", "\\*")


def safe_display(url) -> str:
    """get_display_url + escape untuk ditampilkan DI LUAR backtick"""
    return esc(get_display_url(url))


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
# HELPER: KIRIM PESAN DENGAN FALLBACK
# Coba kirim dengan Markdown, jika gagal kirim plain text
# INI YANG MENCEGAH BOT DIAM
# =====================
async def safe_send(context_or_bot, chat_id, text, **kwargs):
    """
    Kirim pesan dengan Markdown. Jika gagal karena parse error,
    fallback ke plain text agar user tetap dapat balasan.
    """
    bot = getattr(context_or_bot, "bot", context_or_bot)
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            **kwargs
        )
    except Exception as e:
        write_log(f"[MARKDOWN FAIL] {e} | Fallback plain text")
        # Hapus tag Markdown, kirim plain
        plain = re.sub(r"[*_`\[\]]", "", text)
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=plain,
                **{k: v for k, v in kwargs.items() if k != "parse_mode"}
            )
        except Exception as e2:
            write_log(f"[SEND FAIL TOTAL] {e2}")
            return None


async def safe_reply(update, text, **kwargs):
    """
    Reply ke pesan user dengan Markdown.
    Jika gagal, fallback ke plain text.
    """
    try:
        return await update.message.reply_text(
            text,
            parse_mode="Markdown",
            **kwargs
        )
    except Exception as e:
        write_log(f"[REPLY MARKDOWN FAIL] {e} | Fallback plain text")
        plain = re.sub(r"[*_`\[\]]", "", text)
        try:
            return await update.message.reply_text(
                plain,
                **{k: v for k, v in kwargs.items() if k != "parse_mode"}
            )
        except Exception as e2:
            write_log(f"[REPLY FAIL TOTAL] {e2}")
            return None


# =====================
# HELPER: DELETE MESSAGE SAFELY
# =====================
async def safe_delete(context, chat_id, message_id):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass


# =====================
# HELPER: KIRIM HASIL + HAPUS LOADING
# Urutan BENAR: kirim hasil dulu, hapus loading setelah berhasil
# =====================
async def reply_then_delete(update, context, loading_msg, result_text, **kwargs):
    """
    1. Kirim hasil ke user (dengan fallback)
    2. Baru hapus loading message
    Jika hasil gagal dikirim → loading TIDAK dihapus agar user tahu bot masih proses
    """
    result_msg = await safe_reply(update, result_text, **kwargs)
    if result_msg:
        await safe_delete(context, update.effective_chat.id, loading_msg.message_id)
    return result_msg


# =====================
# HELPER: MENTION USER
# =====================
def make_mention(user_id, username=None, first_name="Pemilik"):
    if username:
        return f"@{username}"
    return f"[{first_name}](tg://user?id={user_id})"


# =====================
# HELPER: SSL CONTEXT
# =====================
def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def make_session(ua_index=0):
    headers = {
        "User-Agent"               : USER_AGENTS[ua_index % len(USER_AGENTS)],
        "Accept"                   : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language"          : "en-US,en;q=0.9,id;q=0.8",
        "Accept-Encoding"          : "gzip, deflate, br",
        "Connection"               : "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control"            : "max-age=0",
    }
    connector = aiohttp.TCPConnector(ssl=make_ssl_context())
    timeout   = aiohttp.ClientTimeout(total=20, connect=10)
    return aiohttp.ClientSession(headers=headers, connector=connector, timeout=timeout)


async def safe_read_html(resp) -> str:
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
    try:
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
    except:
        pass
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
        result["error"] = f"Koneksi gagal"
        write_log(f"[CHECK_STATUS CONN ERROR] {url} -> {e}")
    except aiohttp.ClientSSLError as e:
        result["error"] = f"SSL Error"
        write_log(f"[CHECK_STATUS SSL ERROR] {url} -> {e}")
    except asyncio.TimeoutError:
        result["error"] = "Timeout"
        write_log(f"[CHECK_STATUS TIMEOUT] {url}")
    except Exception as e:
        result["error"] = f"Error tidak diketahui"
        write_log(f"[CHECK_STATUS EXCEPTION] {url} -> {e}")
    return result


# =====================
# AMP CHECKER
# =====================
async def get_amp_url(domain, retries=3, delay=3):
    last_exception = None
    last_status    = None

    for attempt in range(retries):
        try:
            async with make_session(ua_index=attempt) as session:
                async with session.get(domain, allow_redirects=True, max_redirects=10) as resp:
                    last_status = resp.status
                    if resp.status >= 500:
                        write_log(f"[RETRY {attempt+1}] {domain} -> HTTP {resp.status}")
                        await asyncio.sleep(delay)
                        continue
                    if resp.status >= 400:
                        write_log(f"[4xx ERROR] {domain} -> HTTP {resp.status}")
                        return "HTTP_ERROR"
                    html = await safe_read_html(resp)
                    soup = BeautifulSoup(html, "html.parser")
                    amp  = soup.find("link", rel="amphtml")
                    if amp and amp.get("href"):
                        return amp["href"].strip()
                    write_log(f"[NO AMP attempt {attempt+1}] {domain}")
                    await asyncio.sleep(delay)

        except asyncio.TimeoutError:
            last_exception = "Timeout"
            write_log(f"[TIMEOUT attempt {attempt+1}] {domain}")
            await asyncio.sleep(delay)

        except aiohttp.ClientSSLError as e:
            last_exception = f"SSL"
            write_log(f"[SSL ERROR attempt {attempt+1}] {domain}")
            if domain.startswith("https://") and attempt == 0:
                http_fallback = domain.replace("https://", "http://", 1)
                try:
                    async with make_session(ua_index=attempt+1) as session:
                        async with session.get(http_fallback, allow_redirects=True, max_redirects=10) as resp2:
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
            last_exception = "ConnError"
            write_log(f"[CONN ERROR attempt {attempt+1}] {domain}")
            await asyncio.sleep(delay)

        except Exception as e:
            last_exception = str(e)[:80]
            write_log(f"[EXCEPTION attempt {attempt+1}] {domain} -> {e}")
            await asyncio.sleep(delay)

    if last_status is not None:
        return None
    write_log(f"[CONN_ERROR FINAL] {domain} -> {last_exception}")
    return "CONN_ERROR"


# =====================
# FORMAT STATUS DISPLAY
# =====================
def format_status_display(status: dict) -> str:
    http_code = status.get("status_code")
    page_code = status.get("page_status_code")
    page_text = status.get("page_status_text")
    error     = status.get("error")
    if error:
        return str(error)
    if page_code and page_code != http_code:
        return f"{page_code} (dari halaman)"
    if page_code and page_text:
        # Batasi panjang teks agar tidak ganggu Markdown
        short = page_text[:40]
        return f"{page_code} - {short}"
    return str(http_code) if http_code else "-"


# =====================
# COMMAND TAMBAH
# =====================
async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, "Gunakan: `/tambah example.com`")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id
    user    = update.effective_user

    loading_msg = await safe_reply(update, f"Mengecek domain `{get_display_url(request_url)}`...")
    if not loading_msg:
        return

    try:
        status = await check_domain_status(request_url)

        if not status["ok"]:
            err = format_status_display(status)
            await reply_then_delete(
                update, context, loading_msg,
                f"*Domain tidak bisa diakses!*\n"
                f"--------------------\n"
                f"Domain : `{get_display_url(request_url)}`\n"
                f"Status : `{err}`\n"
                f"Pastikan domain aktif sebelum ditambahkan.",
            )
            return

        # Edit loading message daripada hapus-kirim baru
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=loading_msg.message_id,
                text="Mengambil AMP URL...",
                parse_mode="Markdown"
            )
        except:
            pass

        amp_url = await get_amp_url(request_url)

        if amp_url == "HTTP_ERROR":
            await reply_then_delete(
                update, context, loading_msg,
                "Server menolak request (HTTP 4xx). Domain tidak bisa dipantau."
            )
            return

        conn_warning = ""
        if amp_url == "CONN_ERROR":
            amp_url      = None
            conn_warning = "\nAMP belum bisa diambil saat ini, akan dicoba kembali saat monitoring berjalan."

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

        amp_display    = get_display_url(amp_url) if amp_url else "Belum terdeteksi"
        status_display = format_status_display(status)
        mention        = make_mention(user.id, user.username, user.first_name)

        await reply_then_delete(
            update, context, loading_msg,
            f"*DOMAIN DITAMBAHKAN*\n"
            f"--------------------\n"
            f"Domain   : `{get_display_url(request_url)}`\n"
            f"Status   : `{status_display}`\n"
            f"AMP Awal : `{amp_display}`\n"
            f"Pemilik  : {mention}\n"
            f"--------------------"
            f"{conn_warning}",
            disable_web_page_preview=True,
        )

    except Exception as e:
        write_log(f"[TAMBAH ERROR] {request_url} -> {e}")
        await reply_then_delete(
            update, context, loading_msg,
            f"Terjadi error saat memproses domain. Coba lagi.\nDetail: `{str(e)[:100]}`"
        )


# =====================
# COMMAND HAPUS
# =====================
async def hapus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, "Gunakan: `/hapus example.com`")
        return
    try:
        request_url, _ = normalize_domain(context.args[0])
        data = load_data()
        if request_url in data:
            del data[request_url]
            save_data(data)
            await safe_reply(
                update,
                f"*Domain Dihapus*\n--------------------\n`{get_display_url(request_url)}`",
                disable_web_page_preview=True,
            )
        else:
            await safe_reply(update, "Domain tidak ditemukan dalam daftar monitoring.")
    except Exception as e:
        write_log(f"[HAPUS ERROR] {e}")
        await safe_reply(update, "Gagal menghapus domain. Coba lagi.")


# =====================
# COMMAND LIST
# =====================
async def list_domains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        data    = load_data()
        domains = [d for d, info in data.items() if info.get("chat_id") == chat_id]

        if not domains:
            await safe_reply(update, "Belum ada domain tersimpan.")
            return

        msg = ["*DAFTAR DOMAIN MONITORING*\n"]
        for d in domains:
            info        = data[d]
            amp_now     = info.get("current_amp")
            amp_display = (
                get_display_url(amp_now)
                if amp_now and amp_now not in ("CONN_ERROR", "HTTP_ERROR")
                else "Tidak terdeteksi"
            )
            page_status = info.get("last_page_status", "")
            http_status = info.get("last_http_status", "-")
            # Batasi panjang page_status
            if page_status and len(page_status) > 30:
                page_status = page_status[:30] + "..."
            status_line = f"{http_status} - {page_status}" if page_status else str(http_status)

            owner_uid = info.get("owner_user_id")
            owner_un  = info.get("owner_username")
            owner_fn  = info.get("owner_first_name", "Pemilik")
            mention   = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else "-"

            msg.append(
                "--------------------\n"
                f"`{get_display_url(d)}`\n"
                f"AMP Awal     : `{get_display_url(info.get('initial_amp'))}`\n"
                f"AMP Sekarang : `{amp_display}`\n"
                f"Status       : `{status_line}`\n"
                f"Pemilik      : {mention}\n"
                f"Last Check   : {info.get('last_checked', '-')}"
            )

        await safe_reply(
            update,
            "\n".join(msg),
            disable_web_page_preview=True,
        )
    except Exception as e:
        write_log(f"[LIST ERROR] {e}")
        await safe_reply(update, "Gagal menampilkan daftar domain. Coba lagi.")


# =====================
# COMMAND CEK
# =====================
async def cek(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, "Gunakan: `/cek example.com`")
        return

    request_url, _ = normalize_domain(context.args[0])

    loading_msg = await safe_reply(update, f"Mengecek `{get_display_url(request_url)}`...")
    if not loading_msg:
        return

    try:
        amp, status = await asyncio.gather(
            get_amp_url(request_url),
            check_domain_status(request_url)
        )

        if amp == "CONN_ERROR":
            amp_text = "Tidak bisa konek ke domain"
        elif amp == "HTTP_ERROR":
            amp_text = "HTTP Error (4xx)"
        elif amp is None:
            amp_text = "Tidak ditemukan (tidak ada amphtml)"
        else:
            amp_text = get_display_url(amp)

        status_display = format_status_display(status)
        redirect_line  = ""
        if status.get("redirect_url"):
            redirect_line = f"Redirect ke : `{get_display_url(status['redirect_url'])}`\n"

        await reply_then_delete(
            update, context, loading_msg,
            f"*HASIL PENGECEKAN AMP*\n"
            f"--------------------\n"
            f"Domain      : `{get_display_url(request_url)}`\n"
            f"Status      : `{status_display}`\n"
            f"{redirect_line}"
            f"AMP URL     : `{amp_text}`\n"
            f"--------------------",
            disable_web_page_preview=True,
        )

    except Exception as e:
        write_log(f"[CEK ERROR] {request_url} -> {e}")
        await reply_then_delete(
            update, context, loading_msg,
            f"Terjadi error saat mengecek domain.\nDetail: `{str(e)[:100]}`"
        )


# =====================
# COMMAND STATUS
# =====================
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, "Gunakan: `/status example.com`")
        return

    request_url, _ = normalize_domain(context.args[0])

    loading_msg = await safe_reply(update, f"Mengecek status `{get_display_url(request_url)}`...")
    if not loading_msg:
        return

    try:
        status = await check_domain_status(request_url)

        kondisi       = "Online / Normal" if status["ok"] else (status["error"] or "Bermasalah")
        redirect_line = f"Redirect ke  : `{get_display_url(status['redirect_url'])}`\n" if status.get("redirect_url") else ""
        page_text     = (status.get("page_status_text") or "")[:40]
        page_line     = f"Info Halaman : `{page_text}`\n" if page_text else ""

        await reply_then_delete(
            update, context, loading_msg,
            f"*STATUS DOMAIN*\n"
            f"--------------------\n"
            f"Domain      : `{get_display_url(request_url)}`\n"
            f"HTTP Status : `{status['status_code'] or '-'}`\n"
            f"{page_line}"
            f"{redirect_line}"
            f"Kondisi     : `{kondisi}`\n"
            f"--------------------",
            disable_web_page_preview=True,
        )

    except Exception as e:
        write_log(f"[STATUS ERROR] {request_url} -> {e}")
        await reply_then_delete(
            update, context, loading_msg,
            f"Terjadi error saat mengecek status.\nDetail: `{str(e)[:100]}`"
        )


# =====================
# COMMAND UPDATE
# =====================
async def update_amp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update, "Gunakan: `/update example.com`")
        return

    request_url, _ = normalize_domain(context.args[0])
    user = update.effective_user
    data = load_data()

    if request_url not in data:
        await safe_reply(update, f"Domain `{get_display_url(request_url)}` tidak ditemukan.")
        return

    info = data[request_url]
    if info.get("owner_user_id") and info["owner_user_id"] != user.id:
        await safe_reply(update, "Kamu bukan pemilik domain ini.")
        return

    loading_msg = await safe_reply(
        update, f"Mengambil AMP terbaru dari `{get_display_url(request_url)}`..."
    )
    if not loading_msg:
        return

    try:
        new_amp = await get_amp_url(request_url)

        if new_amp == "HTTP_ERROR":
            await reply_then_delete(update, context, loading_msg, "Server menolak request saat update AMP.")
            return
        if new_amp == "CONN_ERROR":
            await reply_then_delete(update, context, loading_msg, "Gagal konek ke domain. Coba lagi nanti.")
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

        mention     = make_mention(user.id, user.username, user.first_name)
        amp_old_txt = get_display_url(old_amp) if old_amp else "Tidak ada"
        amp_new_txt = get_display_url(new_amp) if new_amp else "Tidak ada AMP"

        await reply_then_delete(
            update, context, loading_msg,
            f"*AMP REFERENSI DIPERBARUI*\n"
            f"--------------------\n"
            f"Domain   : `{get_display_url(request_url)}`\n"
            f"AMP Lama : `{amp_old_txt}`\n"
            f"AMP Baru : `{amp_new_txt}`\n"
            f"Oleh     : {mention}\n"
            f"--------------------",
            disable_web_page_preview=True,
        )

    except Exception as e:
        write_log(f"[UPDATE ERROR] {request_url} -> {e}")
        await reply_then_delete(
            update, context, loading_msg,
            f"Terjadi error saat update AMP.\nDetail: `{str(e)[:100]}`"
        )


# =====================
# PERIODIC CHECK
# =====================
async def periodic_check(app):
    await asyncio.sleep(10)

    while True:
        try:
            data    = load_data()
            updated = False

            for domain, info in data.items():
                try:
                    initial_amp        = info.get("initial_amp")
                    current_amp        = info.get("current_amp")
                    notified_count     = info.get("change_notified_count", 0)
                    consecutive_no_amp = info.get("consecutive_no_amp", 0)

                    owner_uid    = info.get("owner_user_id")
                    owner_un     = info.get("owner_username")
                    owner_fn     = info.get("owner_first_name", "Pemilik")
                    mention      = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else ""
                    mention_line = f"Pemilik : {mention}\n" if mention else ""

                    domain_status = await check_domain_status(domain)
                    data[domain]["last_http_status"] = domain_status["status_code"]
                    if domain_status.get("page_status_text"):
                        data[domain]["last_page_status"] = domain_status["page_status_text"]

                    if not domain_status["ok"]:
                        err_msg = format_status_display(domain_status)
                        if not info.get("domain_down_notified", False):
                            await safe_send(
                                app.bot, info["chat_id"],
                                f"*DOMAIN TIDAK BISA DIAKSES*\n"
                                f"--------------------\n"
                                f"Domain  : `{get_display_url(domain)}`\n"
                                f"Status  : `{err_msg}`\n"
                                f"{mention_line}"
                                f"--------------------",
                                disable_web_page_preview=True,
                            )
                            data[domain]["domain_down_notified"] = True
                            updated = True
                        data[domain]["last_checked"] = str(datetime.now())
                        updated = True
                        continue

                    if info.get("domain_down_notified", False):
                        data[domain]["domain_down_notified"] = False
                        updated = True

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
                            await safe_send(
                                app.bot, info["chat_id"],
                                f"*AMP TIDAK TERDETEKSI*\n"
                                f"--------------------\n"
                                f"Domain   : `{get_display_url(domain)}`\n"
                                f"AMP Awal : `{get_display_url(initial_amp)}`\n"
                                f"Status   : Hilang 3x berturut-turut\n"
                                f"Notif    : {notified_count+1}/3\n"
                                f"{mention_line}"
                                f"--------------------",
                                disable_web_page_preview=True,
                            )
                            data[domain]["change_notified_count"] = notified_count + 1
                            updated = True
                    else:
                        data[domain]["consecutive_no_amp"] = 0

                        if new_amp != initial_amp and new_amp is not None and current_amp != new_amp:
                            data[domain]["current_amp"] = new_amp
                            if notified_count < 3:
                                await safe_send(
                                    app.bot, info["chat_id"],
                                    f"*AMP URL BERUBAH*\n"
                                    f"--------------------\n"
                                    f"Domain   : `{get_display_url(domain)}`\n"
                                    f"AMP Lama : `{get_display_url(initial_amp)}`\n"
                                    f"AMP Baru : `{get_display_url(new_amp)}`\n"
                                    f"Notif    : {notified_count+1}/3\n"
                                    f"{mention_line}"
                                    f"Jika disengaja gunakan /update {get_display_url(domain)}\n"
                                    f"--------------------",
                                    disable_web_page_preview=True,
                                )
                                data[domain]["change_notified_count"] = notified_count + 1
                            updated = True

                        elif new_amp == initial_amp and current_amp != initial_amp:
                            data[domain]["current_amp"] = new_amp
                            data[domain]["change_notified_count"] = 0
                            await safe_send(
                                app.bot, info["chat_id"],
                                f"*AMP KEMBALI NORMAL*\n"
                                f"--------------------\n"
                                f"Domain    : `{get_display_url(domain)}`\n"
                                f"AMP Aktif : `{get_display_url(initial_amp)}`\n"
                                f"{mention_line}"
                                f"--------------------",
                                disable_web_page_preview=True,
                            )
                            updated = True

                    data[domain]["last_checked"] = str(datetime.now())

                except Exception as e:
                    write_log(f"[PERIODIC DOMAIN ERROR] {domain} -> {e}")
                    continue  # Lanjut ke domain berikutnya, jangan crash semua

            if updated:
                save_data(data)

        except Exception as e:
            write_log(f"[PERIODIC LOOP ERROR] {e}")

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
