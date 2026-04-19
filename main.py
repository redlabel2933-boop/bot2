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
# HELPER: SAFE REPLY (anti-crash Markdown)
# Jika Markdown gagal (karakter khusus di URL), fallback ke plain text
# =====================
async def safe_reply(message, text, **kwargs):
    try:
        return await message.reply_text(text, parse_mode="Markdown", **kwargs)
    except Exception:
        # Markdown gagal -> kirim tanpa formatting
        try:
            clean = text.replace('`', '').replace('*', '').replace('_', '')
            return await message.reply_text(clean, **kwargs)
        except Exception as e2:
            return await message.reply_text(f"Error menampilkan hasil: {str(e2)[:100]}")


# =====================
# HELPER: ESCAPE MARKDOWN
# =====================
def escape_md(text):
    if not text:
        return "-"
    text = str(text)
    for ch in ('\\', '`', '*', '_', '[', ']', '(', ')'):
        text = text.replace(ch, '\\' + ch)
    return text


# =====================
# HELPER: MENTION USER
# =====================
def make_mention(user_id, username=None, first_name="Pemilik"):
    if username:
        return f"@{username}"
    return f"[{first_name}](tg://user?id={user_id})"


# =====================
# HELPER: BUAT SSL CONTEXT
# =====================
def make_ssl_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx


# =====================
# HELPER: BUAT SESSION
# =====================
def make_session(ua_index=0, cookie_jar=None):
    """
    Buat aiohttp ClientSession dengan:
    - SSL toleran
    - Header lengkap mirip browser asli
    - Timeout 30 detik
    - Cookie jar opsional untuk share cookies antar request
    """
    headers = {
        "User-Agent"      : USER_AGENTS[ua_index % len(USER_AGENTS)],
        "Accept"          : "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language" : "en-US,en;q=0.9,id;q=0.8",
        "Accept-Encoding" : "gzip, deflate",
        "Connection"      : "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control"   : "max-age=0",
        "DNT"             : "1",
        "Sec-Fetch-Dest"  : "document",
        "Sec-Fetch-Mode"  : "navigate",
        "Sec-Fetch-Site"  : "none",
        "Sec-Fetch-User"  : "?1",
    }
    connector = aiohttp.TCPConnector(ssl=make_ssl_context())
    timeout   = aiohttp.ClientTimeout(total=30, connect=15)
    return aiohttp.ClientSession(
        headers=headers,
        connector=connector,
        timeout=timeout,
        cookie_jar=cookie_jar or aiohttp.CookieJar(unsafe=True),
    )


# =====================
# HELPER: BUAT GOOGLEBOT SESSION (untuk detect cloaking)
# =====================
def make_googlebot_session(cookie_jar=None):
    """Session yang meniru Googlebot asli (UA + From header)."""
    headers = {
        "User-Agent"      : "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "From"            : "googlebot(at)googlebot.com",
        "Accept"          : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language" : "en-US,en;q=0.5",
        "Accept-Encoding" : "gzip, deflate",
        "Connection"      : "keep-alive",
    }
    connector = aiohttp.TCPConnector(ssl=make_ssl_context())
    timeout   = aiohttp.ClientTimeout(total=30, connect=15)
    return aiohttp.ClientSession(
        headers=headers, connector=connector, timeout=timeout,
        cookie_jar=cookie_jar or aiohttp.CookieJar(unsafe=True),
    )


def make_googlebot_mobile_session(cookie_jar=None):
    """Session yang meniru Googlebot Mobile (smartphone)."""
    headers = {
        "User-Agent"      : "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.71 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        "From"            : "googlebot(at)googlebot.com",
        "Accept"          : "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language" : "en-US,en;q=0.5",
        "Accept-Encoding" : "gzip, deflate",
        "Connection"      : "keep-alive",
    }
    connector = aiohttp.TCPConnector(ssl=make_ssl_context())
    timeout   = aiohttp.ClientTimeout(total=30, connect=15)
    return aiohttp.ClientSession(
        headers=headers, connector=connector, timeout=timeout,
        cookie_jar=cookie_jar or aiohttp.CookieJar(unsafe=True),
    )


# =====================
# HELPER: BACA HTML DENGAN ENCODING AMAN
# =====================
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
# - 403 retry dengan UA berbeda
# - 403 dianggap "ok" (domain online, hanya block bot)
# - SSL error fallback ke http://
# =====================
async def check_domain_status(url):
    result = {
        "status_code"     : None,
        "page_status_code": None,
        "page_status_text": None,
        "page_title"      : None,
        "ok"              : False,
        "error"           : None,
        "redirect_url"    : None,
    }

    # Selalu pakai browser UA (index 1=Chrome, 3=Safari, 4=Firefox)
    # BUKAN Googlebot (index 0), agar status akurat seperti user asli
    browser_ua_indices = [1, 3, 4]

    for ua_idx in browser_ua_indices:
        try:
            async with make_session(ua_index=ua_idx) as session:
                async with session.get(url, allow_redirects=True, max_redirects=10) as resp:
                    result["status_code"] = resp.status
                    result["ok"] = resp.status < 400 or resp.status == 403

                    if str(resp.url) != url:
                        result["redirect_url"] = str(resp.url)

                    try:
                        html = await safe_read_html(resp)
                        # Ambil title halaman sebagai status indicator
                        soup = BeautifulSoup(html, "html.parser")
                        title_tag = soup.find("title")
                        if title_tag and title_tag.text:
                            result["page_title"] = title_tag.text.strip()[:80]

                        ps = extract_page_status(html)
                        result["page_status_code"] = ps["code"]
                        result["page_status_text"] = ps["text"]
                    except:
                        pass

                    if resp.status == 403 and ua_idx != browser_ua_indices[-1]:
                        continue

                    return result

        except aiohttp.ClientSSLError as e:
            if url.startswith("https://"):
                http_url = url.replace("https://", "http://", 1)
                try:
                    async with make_session(ua_index=1) as session:
                        async with session.get(http_url, allow_redirects=True, max_redirects=10) as resp2:
                            result["status_code"] = resp2.status
                            result["ok"] = resp2.status < 400 or resp2.status == 403
                            if str(resp2.url) != http_url:
                                result["redirect_url"] = str(resp2.url)
                            try:
                                html2 = await safe_read_html(resp2)
                                soup2 = BeautifulSoup(html2, "html.parser")
                                t2 = soup2.find("title")
                                if t2 and t2.text:
                                    result["page_title"] = t2.text.strip()[:80]
                            except:
                                pass
                            return result
                except:
                    pass
            result["error"] = f"SSL Error: {str(e)[:60]}"
        except aiohttp.ClientConnectorError as e:
            result["error"] = f"Koneksi gagal: {str(e)[:60]}"
        except asyncio.TimeoutError:
            result["error"] = "Timeout (server tidak merespons)"
        except Exception as e:
            result["error"] = f"Error: {str(e)[:80]}"

    return result


# =====================
# HELPER: EXTRACT AMP FROM HTML
# =====================
def find_amp_in_html(html):
    """Cari <link rel='amphtml'> di HTML, return href atau None."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    amp = soup.find("link", rel="amphtml")
    if amp and amp.get("href"):
        return amp["href"].strip()
    return None


def find_article_links(html, base_url):
    """
    Cari link artikel dari halaman (untuk cek AMP di halaman artikel).
    Banyak website cuma punya AMP di halaman artikel, bukan homepage.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    parsed_base = urlparse(base_url)
    base_domain = parsed_base.netloc.lower().replace("www.", "")
    links = []

    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"].strip()
        if href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        if href.startswith("/"):
            href = f"{parsed_base.scheme}://{parsed_base.netloc}{href}"
        elif not href.startswith("http"):
            continue
        link_parsed = urlparse(href)
        link_domain = link_parsed.netloc.lower().replace("www.", "")
        if link_domain != base_domain:
            continue
        path = link_parsed.path.rstrip("/")
        if path and path.count("/") >= 1 and len(path) > 5:
            skip_paths = ("/wp-admin", "/wp-login", "/wp-content", "/feed",
                          "/tag/", "/category/", "/author/", "/page/",
                          "/login", "/register", "/cart", "/checkout",
                          "/search", "/sitemap", ".xml", ".json", ".css", ".js")
            if not any(path.lower().startswith(s) or path.lower().endswith(s) for s in skip_paths):
                if href not in links:
                    links.append(href)
        if len(links) >= 5:
            break
    return links


# =====================
# AMP CHECKER V3 (CLOAKING-AWARE)
# Phase 1: Googlebot Desktop + Mobile (untuk cloaked sites)
# Phase 2: Browser biasa (untuk non-cloaked / bot-blocking sites)
# Phase 3: Cek halaman artikel dengan kedua mode
# =====================
async def get_amp_url(domain, retries=3, delay=2):
    """
    Return:
      str   -> AMP URL ditemukan
      None  -> Domain OK tapi tidak ada AMP
      "HTTP_ERROR"  -> Server balas 4xx
      "CONN_ERROR"  -> Tidak bisa konek sama sekali
    """
    last_exception = None
    last_status    = None
    page_html      = None
    final_url      = None

    # == PHASE 1: CEK SEBAGAI GOOGLEBOT (untuk cloaking) ==
    googlebot_makers = [
        ("Googlebot-Desktop", make_googlebot_session),
        ("Googlebot-Mobile", make_googlebot_mobile_session),
    ]

    for bot_name, session_factory in googlebot_makers:
        try:
            async with session_factory() as session:
                async with session.get(domain, allow_redirects=True, max_redirects=10) as resp:
                    last_status = resp.status
                    final_url = str(resp.url)
                    if resp.status < 400:
                        html = await safe_read_html(resp)
                        page_html = html
                        amp_href = find_amp_in_html(html)
                        if amp_href:
                            write_log(f"[AMP via {bot_name}] {domain} -> {amp_href}")
                            return amp_href
                        write_log(f"[NO AMP via {bot_name}] {domain}")
                    else:
                        write_log(f"[{bot_name} HTTP {resp.status}] {domain}")
        except asyncio.TimeoutError:
            last_exception = "Timeout"
            write_log(f"[TIMEOUT {bot_name}] {domain}")
        except aiohttp.ClientSSLError as e:
            last_exception = f"SSL: {e}"
            write_log(f"[SSL {bot_name}] {domain}")
        except aiohttp.ClientConnectorError as e:
            last_exception = f"ConnError: {e}"
            write_log(f"[CONN {bot_name}] {domain}")
        except Exception as e:
            last_exception = str(e)
            write_log(f"[ERR {bot_name}] {domain} -> {e}")

    # == PHASE 2: CEK SEBAGAI BROWSER BIASA ==
    cookie_jar = aiohttp.CookieJar(unsafe=True)
    for attempt in range(retries):
        try:
            async with make_session(ua_index=attempt + 1, cookie_jar=cookie_jar) as session:
                async with session.get(domain, allow_redirects=True, max_redirects=10) as resp:
                    last_status = resp.status
                    final_url = str(resp.url)
                    if resp.status >= 500:
                        await asyncio.sleep(delay)
                        continue
                    if resp.status == 403:
                        await asyncio.sleep(delay)
                        continue
                    if resp.status >= 400:
                        return "HTTP_ERROR"
                    html = await safe_read_html(resp)
                    page_html = html
                    amp_href = find_amp_in_html(html)
                    if amp_href:
                        write_log(f"[AMP via browser] {domain} -> {amp_href}")
                        return amp_href
                    write_log(f"[NO AMP browser {attempt+1}] {domain}")
                    break  # 200 tapi tidak ada AMP, tidak perlu retry
        except asyncio.TimeoutError:
            last_exception = "Timeout"
            await asyncio.sleep(delay)
        except aiohttp.ClientSSLError as e:
            last_exception = f"SSL: {e}"
            if domain.startswith("https://") and attempt == 0:
                try:
                    http_fb = domain.replace("https://", "http://", 1)
                    async with make_session(ua_index=1) as s:
                        async with s.get(http_fb, allow_redirects=True, max_redirects=10) as r2:
                            if r2.status < 400:
                                h2 = await safe_read_html(r2)
                                page_html = h2
                                final_url = str(r2.url)
                                a2 = find_amp_in_html(h2)
                                if a2:
                                    return a2
                                last_status = r2.status
                except:
                    pass
            await asyncio.sleep(delay)
        except aiohttp.ClientConnectorError as e:
            last_exception = f"ConnError: {e}"
            await asyncio.sleep(delay)
        except Exception as e:
            last_exception = str(e)
            await asyncio.sleep(delay)

    # == PHASE 3: CEK HALAMAN ARTIKEL ==
    if page_html and last_status is not None and last_status < 400:
        check_url = final_url or domain
        article_links = find_article_links(page_html, check_url)
        if article_links:
            write_log(f"[ARTICLE CHECK] {domain} -> {len(article_links)} links")
            for link in article_links[:3]:
                # Coba Googlebot dulu (cloaking)
                try:
                    async with make_googlebot_session() as session:
                        async with session.get(link, allow_redirects=True, max_redirects=5) as ar:
                            if ar.status == 200:
                                ah = await safe_read_html(ar)
                                amp_href = find_amp_in_html(ah)
                                if amp_href:
                                    write_log(f"[AMP IN ARTICLE via Googlebot] {domain} -> {amp_href}")
                                    return amp_href
                except:
                    pass
                # Fallback: browser biasa
                try:
                    async with make_session(ua_index=1) as session:
                        async with session.get(link, allow_redirects=True, max_redirects=5) as ar:
                            if ar.status == 200:
                                ah = await safe_read_html(ar)
                                amp_href = find_amp_in_html(ah)
                                if amp_href:
                                    write_log(f"[AMP IN ARTICLE via browser] {domain} -> {amp_href}")
                                    return amp_href
                except:
                    continue

    # Semua phase selesai
    if last_status is not None:
        if last_status == 403:
            write_log(f"[403 FINAL] {domain}")
            return "HTTP_ERROR"
        return None
    write_log(f"[CONN_ERROR FINAL] {domain} -> {last_exception}")
    return "CONN_ERROR"


# =====================
# FORMAT STATUS DISPLAY
# =====================
def format_status_display(status: dict) -> str:
    error = status.get("error")
    if error:
        return error
    http_code = status.get("status_code")
    return str(http_code) if http_code else "-"


# =====================
# COMMAND TAMBAH
# =====================
async def tambah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /tambah example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id
    user    = update.effective_user

    loading_msg = await update.message.reply_text(
        f"\u23f3 Mengecek domain `{get_display_url(request_url)}`..."
    )

    try:
        status = await check_domain_status(request_url)
    except Exception as e:
        await safe_delete(context, chat_id, loading_msg.message_id)
        await update.message.reply_text(f"Error saat cek domain: {str(e)[:100]}")
        return

    await safe_delete(context, chat_id, loading_msg.message_id)

    if not status["ok"]:
        err = status["error"] or format_status_display(status)
        await safe_reply(
            update.message,
            f"\u274c *Domain tidak bisa diakses!*\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f310 Domain : `{get_display_url(request_url)}`\n"
            f"\u26a0\ufe0f Status : `{err}`\n"
            f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"Pastikan domain aktif sebelum ditambahkan.",
        )
        return

    loading_msg2 = await update.message.reply_text("\u23f3 Mengambil AMP URL...")

    try:
        amp_url = await get_amp_url(request_url)
    except Exception as e:
        await safe_delete(context, chat_id, loading_msg2.message_id)
        await update.message.reply_text(f"Error saat cek AMP: {str(e)[:100]}")
        return

    await safe_delete(context, chat_id, loading_msg2.message_id)

    if amp_url == "HTTP_ERROR":
        await update.message.reply_text(
            "\u274c Server menolak request (HTTP 4xx). Domain tidak bisa dipantau."
        )
        return

    conn_warning = ""
    if amp_url == "CONN_ERROR":
        amp_url      = None
        conn_warning = "\n\u26a0\ufe0f _AMP belum bisa diambil saat ini, akan dicoba kembali saat monitoring._"

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

    await safe_reply(
        update.message,
        f"\u2705 *DOMAIN DITAMBAHKAN*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\U0001f310 Domain   : `{get_display_url(request_url)}`\n"
        f"\U0001f4e1 Status   : `{status_display}`\n"
        f"\u26a1 AMP Awal : `{amp_display}`\n"
        f"\U0001f464 Pemilik  : {mention}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500"
        f"{conn_warning}",
        disable_web_page_preview=True,
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
        await safe_reply(
            update.message,
            f"\U0001f5d1 *Domain Dihapus*\n\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n\U0001f310 `{get_display_url(request_url)}`",
            disable_web_page_preview=True,
        )
    else:
        await update.message.reply_text("\u26a0\ufe0f Domain tidak ditemukan.")


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

    msg = ["\U0001f4cb *DAFTAR DOMAIN MONITORING*\n"]
    for d in domains:
        info       = data[d]
        amp_now    = info.get("current_amp")
        amp_display = (
            get_display_url(amp_now)
            if amp_now and amp_now not in ("CONN_ERROR", "HTTP_ERROR")
            else "Tidak terdeteksi"
        )
        http_status = info.get("last_http_status", "-")

        owner_uid = info.get("owner_user_id")
        owner_un  = info.get("owner_username")
        owner_fn  = info.get("owner_first_name", "Pemilik")
        mention   = make_mention(owner_uid, owner_un, owner_fn) if owner_uid else "-"

        msg.append(
            "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
            f"\U0001f310 `{get_display_url(d)}`\n"
            f"\u26a1 AMP Awal     : `{get_display_url(info.get('initial_amp'))}`\n"
            f"\u26a1 AMP Sekarang : `{amp_display}`\n"
            f"\U0001f4e1 Status       : `{http_status}`\n"
            f"\U0001f464 Pemilik      : {mention}\n"
            f"\U0001f552 Last Check   : {info.get('last_checked', '-')}"
        )

    await safe_reply(
        update.message,
        "\n".join(msg),
        disable_web_page_preview=True,
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
        f"\u23f3 Mengecek `{get_display_url(request_url)}`..."
    )

    try:
        amp, status = await asyncio.gather(
            get_amp_url(request_url),
            check_domain_status(request_url)
        )
    except Exception as e:
        await safe_delete(context, chat_id, loading_msg.message_id)
        await update.message.reply_text(f"\u274c Error saat mengecek: {str(e)[:100]}")
        return

    await safe_delete(context, chat_id, loading_msg.message_id)

    if amp == "CONN_ERROR":
        amp_text = "\u26a0\ufe0f Tidak bisa konek"
    elif amp == "HTTP_ERROR":
        amp_text = "\u274c HTTP Error (4xx)"
    elif amp is None:
        amp_text = "\u2796 Tidak ditemukan"
    else:
        amp_text = f"`{get_display_url(amp)}`"

    status_display = format_status_display(status)
    redirect_line  = ""
    if status.get("redirect_url"):
        redir_display = get_display_url(status['redirect_url'])
        redirect_line = f"\U0001f500 Redirect   : `{redir_display}`\n"

    await safe_reply(
        update.message,
        f"\U0001f50d *HASIL PENGECEKAN AMP*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\U0001f310 Domain     : `{get_display_url(request_url)}`\n"
        f"\U0001f4e1 Status     : `{status_display}`\n"
        f"{redirect_line}"
        f"\u26a1 AMP URL    : {amp_text}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        disable_web_page_preview=True,
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
        f"\u23f3 Mengecek status `{get_display_url(request_url)}`..."
    )

    try:
        status = await check_domain_status(request_url)
    except Exception as e:
        await safe_delete(context, chat_id, loading_msg.message_id)
        await update.message.reply_text(f"\u274c Error: {str(e)[:100]}")
        return

    await safe_delete(context, chat_id, loading_msg.message_id)

    if status["ok"]:
        kondisi = "\u2705 Online"
    elif status["error"]:
        kondisi = f"\u274c {status['error']}"
    else:
        kondisi = "\u26a0\ufe0f Bermasalah"

    redirect_line = f"\U0001f500 Redirect   : `{get_display_url(status['redirect_url'])}`\n" if status.get("redirect_url") else ""

    await safe_reply(
        update.message,
        f"\U0001f4e1 *STATUS DOMAIN*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\U0001f310 Domain     : `{get_display_url(request_url)}`\n"
        f"\U0001f4c8 HTTP Code  : `{status['status_code'] or '-'}`\n"
        f"{redirect_line}"
        f"\U0001f7e2 Kondisi    : {kondisi}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        disable_web_page_preview=True,
    )


# =====================
# COMMAND UPDATE
# =====================
async def update_amp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Gunakan: /update example.com")
        return

    request_url, _ = normalize_domain(context.args[0])
    chat_id = update.effective_chat.id
    user    = update.effective_user
    data    = load_data()

    if request_url not in data:
        await update.message.reply_text(
            f"Domain {get_display_url(request_url)} tidak ditemukan."
        )
        return

    info = data[request_url]
    if info.get("owner_user_id") and info["owner_user_id"] != user.id:
        await update.message.reply_text("Kamu bukan pemilik domain ini.")
        return

    loading_msg = await update.message.reply_text(
        f"Mengambil AMP terbaru dari {get_display_url(request_url)}..."
    )

    try:
        new_amp = await get_amp_url(request_url)
    except Exception as e:
        await safe_delete(context, chat_id, loading_msg.message_id)
        await update.message.reply_text(f"Error: {str(e)[:100]}")
        return

    await safe_delete(context, chat_id, loading_msg.message_id)

    if new_amp == "HTTP_ERROR":
        await update.message.reply_text("\u274c Server menolak request saat update AMP.")
        return

    if new_amp == "CONN_ERROR":
        await update.message.reply_text("\u274c Gagal konek ke domain. Coba lagi nanti.")
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
    await safe_reply(
        update.message,
        f"\u2705 *AMP REFERENSI DIPERBARUI*\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
        f"\U0001f310 Domain   : `{get_display_url(request_url)}`\n"
        f"\u26a1 AMP Lama : `{get_display_url(old_amp)}`\n"
        f"\u26a1 AMP Baru : `{get_display_url(new_amp) if new_amp else 'Tidak ada AMP'}`\n"
        f"\U0001f464 Oleh     : {mention}\n"
        f"\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        disable_web_page_preview=True,
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

            # -- Cek status domain --
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
                                "*DOMAIN TIDAK BISA DIAKSES*\n"
                                "--------------------\n"
                                f"Domain  : {get_display_url(domain)}\n"
                                f"Status  : {err_msg}\n"
                                f"{mention_line}"
                                "--------------------"
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

            # -- Cek AMP --
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
                                "*AMP TIDAK TERDETEKSI*\n"
                                "--------------------\n"
                                f"Domain   : {get_display_url(domain)}\n"
                                f"AMP Awal : {get_display_url(initial_amp)}\n"
                                f"Status   : Hilang 3x berturut-turut\n"
                                f"Notif    : {notified_count+1}/3\n"
                                f"{mention_line}"
                                "--------------------"
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
                                    "*AMP URL BERUBAH*\n"
                                    "--------------------\n"
                                    f"Domain   : {get_display_url(domain)}\n"
                                    f"AMP Lama : {get_display_url(initial_amp)}\n"
                                    f"AMP Baru : {get_display_url(new_amp)}\n"
                                    f"Notif    : {notified_count+1}/3\n"
                                    f"{mention_line}"
                                    f"Jika disengaja gunakan: /update {get_display_url(domain)}\n"
                                    "--------------------"
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
                                "*AMP KEMBALI NORMAL*\n"
                                "--------------------\n"
                                f"Domain    : {get_display_url(domain)}\n"
                                f"AMP Aktif : {get_display_url(initial_amp)}\n"
                                f"{mention_line}"
                                "--------------------"
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
