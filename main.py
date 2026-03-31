"""
TikTok View Buff — Telegram Bot
================================
Python 3.10+

Commands:
  /start                 — Khởi động bot, hiển thị IP server
  /view <url>            — Bắt đầu buff view cho video TikTok
  /view_stop             — Dừng buff view đang chạy
  /proxy_add <p1> <p2>   — Thêm proxy (inline hoặc gửi file .txt)
  /proxy_check           — Kiểm tra proxy còn sống
  /proxy_list            — Xem danh sách proxy
  /worker <số>           — Đặt số luồng (mặc định 300)

Environment variables (Railway):
  TELEGRAM_BOT_TOKEN     — Token từ @BotFather (bắt buộc)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import socket
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import curl_cffi.requests as cfreqs
from telegram import Update, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# TikTok API servers (xoay vòng để tránh rate-limit)
TIKTOK_API_SERVERS = [
    "https://api16-normal-c-useast1a.tiktokv.com",
    "https://api19-core-c-useast1a.tiktokv.com",
    "https://api16-normal-useast5.tiktokv.com",
    "https://api21-normal-c-useast1a.tiktokv.com",
    "https://api22-normal-c-useast1a.tiktokv.com",
]
TIKTOK_PLAY_PATH   = "/aweme/v1/playtime/"

REPORT_INTERVAL     = 30   # giây giữa 2 lần báo cáo
PROXY_CHECK_TIMEOUT = 8    # giây timeout khi test proxy
PROXY_TEST_URL      = "https://www.tiktok.com/"
DEFAULT_WORKERS     = 300

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tt_bot")


# ─────────────────────────────────────────────────────────────────────────────
# STATE
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BuffSession:
    """Trạng thái buff view của 1 chat."""
    chat_id:        int
    video_url:      str
    video_id:       str
    initial_views:  int
    initial_likes:  int
    initial_saves:  int
    initial_shares: int
    start_time:     float = field(default_factory=time.time)
    sent_views:     int   = 0
    is_running:     bool  = True
    stop_event:     asyncio.Event = field(default_factory=asyncio.Event)
    task:           Optional[asyncio.Task] = None


class BotState:
    """Trạng thái toàn cục."""

    def __init__(self) -> None:
        self.proxies:  List[str]             = []
        self.proxy_set: Set[str]             = set()
        self.workers:  int                   = DEFAULT_WORKERS
        self.sessions: Dict[int, BuffSession] = {}

    def add_proxies(self, new_proxies: List[str]) -> int:
        added = 0
        for p in new_proxies:
            p = p.strip()
            if p and p not in self.proxy_set:
                self.proxies.append(p)
                self.proxy_set.add(p)
                added += 1
        return added

    def get_proxy(self, index: int) -> Optional[str]:
        if not self.proxies:
            return None
        return self.proxies[index % len(self.proxies)]

    def remove_dead_proxies(self, dead: Set[str]) -> None:
        self.proxies   = [p for p in self.proxies if p not in dead]
        self.proxy_set -= dead


STATE = BotState()

# ─────────────────────────────────────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────────────────────────────────────

def get_server_ip() -> str:
    """Lấy IP PUBLIC thật của server (không phải IP nội bộ)."""
    import urllib.request
    try:
        # Gọi API bên ngoài để lấy IP public thật
        ip = urllib.request.urlopen(
            "https://api.ipify.org", timeout=5
        ).read().decode().strip()
        return ip
    except Exception:
        try:
            # Fallback: ipinfo.io
            import json
            data = urllib.request.urlopen(
                "https://ipinfo.io/json", timeout=5
            ).read()
            return json.loads(data).get("ip", "Không xác định")
        except Exception:
            return "Không xác định"


def fmt_number(n: int) -> str:
    return f"{n:,}"


def fmt_duration(secs: float) -> str:
    secs = int(secs)
    h, rem = divmod(secs, 3600)
    m, s   = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def escape_md(text: str) -> str:
    """Escape ký tự đặc biệt MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def extract_video_id(url: str) -> Optional[str]:
    match = re.search(r"/video/(\d{15,20})", url)
    return match.group(1) if match else None


def normalize_proxy(raw: str, scheme: str = "http") -> str:
    """
    Chuẩn hóa proxy về dạng scheme://[user:pass@]host:port
    Hỗ trợ tất cả định dạng phổ biến:
      host:port                    → scheme://host:port
      host:port:user:pass          → scheme://user:pass@host:port  (Webshare)
      user:pass@host:port          → scheme://user:pass@host:port
      scheme://host:port           → giữ nguyên scheme
      scheme://user:pass@host:port → giữ nguyên
    """
    raw = raw.strip()
    if not raw:
        return ""

    # Đã có scheme → giữ nguyên
    if "://" in raw:
        return raw

    # Webshare format: host:port:user:pass (4 phần phân tách bởi ":")
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        return f"{scheme}://{user}:{passwd}@{host}:{port}"

    # user:pass@host:port (có @)
    if "@" in raw:
        return f"{scheme}://{raw}"

    # host:port thuần
    return f"{scheme}://{raw}"


def detect_scheme_from_filename(filename: str) -> str:
    """Tự động nhận dạng loại proxy từ tên file."""
    name = filename.lower()
    if "socks5" in name:
        return "socks5"
    if "socks4" in name:
        return "socks4"
    return "http"


# ─────────────────────────────────────────────────────────────────────────────
# TIKTOK VIDEO INFO
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_short_url(url: str) -> str:
    """
    Resolve vt.tiktok.com / vm.tiktok.com sang URL đầy đủ có /video/ID.
    Dùng GET (không phải HEAD) vì TikTok không trả Location cho HEAD.
    """
    if not ("vt.tiktok.com" in url or "vm.tiktok.com" in url):
        return url
    try:
        sess = cfreqs.AsyncSession(impersonate="chrome110")
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        resp = await sess.get(url, headers=headers, allow_redirects=True, timeout=15)
        final_url = str(resp.url)
        log.info(f"[Resolve] {url} → {final_url}")
        return final_url
    except Exception as exc:
        log.warning(f"[Resolve] Lỗi resolve short URL: {exc}")
        return url


def _parse_count(raw) -> int:
    """Chuyển đổi số dạng 1.2M / 3.5K / 1234567 → int."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).replace(",", "").strip().upper()
    for suf, mult in [("B", 1_000_000_000), ("M", 1_000_000), ("K", 1_000)]:
        if s.endswith(suf):
            try:
                return int(float(s[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


async def fetch_video_info(url: str, proxy: Optional[str] = None) -> Optional[Dict]:
    """
    Lấy thông tin video TikTok:
      1. Resolve short URL → URL đầy đủ (lấy video_id)
      2. Thử oEmbed API (không cần auth, lấy title)
      3. Scrape HTML → parse JSON __UNIVERSAL_DATA_FOR_REHYDRATION__ / SIGI_STATE
         để lấy playCount, diggCount, collectCount, shareCount
    Tham số proxy: nên dùng proxy nếu có để tránh bị Railway IP block.
    """
    import json as _json

    # Dùng proxy đầu tiên nếu không truyền vào
    if proxy is None and STATE.proxies:
        proxy = STATE.proxies[0]

    url      = await resolve_short_url(url)
    video_id = extract_video_id(url)
    log.info(f"[VideoInfo] URL sau resolve: {url} | video_id: {video_id}")

    headers_web = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer":         "https://www.tiktok.com/",
    }

    title  = "N/A"
    views  = 0
    likes  = 0
    saves  = 0
    shares = 0

    proxy_cfg = {"http": proxy, "https": proxy} if proxy else {}

    # ── Bước 1: oEmbed (lấy title nhanh) ─────────────────────────────────────
    try:
        sess = cfreqs.AsyncSession(impersonate="chrome110")
        if proxy_cfg:
            sess.proxies = proxy_cfg
        oe = await sess.get(
            f"https://www.tiktok.com/oembed?url={url}",
            headers=headers_web, timeout=10,
        )
        if oe.status_code == 200:
            oe_data = oe.json()
            title   = oe_data.get("title", "N/A")[:80]
            # oEmbed không trả view count nên tiếp tục scrape HTML
    except Exception as exc:
        log.warning(f"[oEmbed] {exc}")

    # ── Bước 2: Scrape trang chính lấy số liệu ───────────────────────────────
    try:
        sess2 = cfreqs.AsyncSession(impersonate="chrome110")
        if proxy_cfg:
            sess2.proxies = proxy_cfg
        resp  = await sess2.get(url, headers=headers_web, timeout=20)
        html  = resp.text

        # Cập nhật video_id nếu chưa có (lấy từ URL sau redirect)
        if not video_id:
            video_id = extract_video_id(str(getattr(resp, "url", url)))

        # ── Thử parse __UNIVERSAL_DATA_FOR_REHYDRATION__ ──────────────────
        m = re.search(
            r'<script[^>]+id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>([^<]+)</script>',
            html, re.DOTALL,
        )
        if m:
            try:
                data    = _json.loads(m.group(1))
                # Đi xuống cây JSON tìm stats
                def _dig(obj, *keys):
                    for k in keys:
                        if isinstance(obj, dict) and k in obj:
                            obj = obj[k]
                        else:
                            return None
                    return obj

                # Tìm itemInfo > itemStruct > stats
                item_struct = (
                    _dig(data, "__DEFAULT_SCOPE__", "webapp.video-detail", "itemInfo", "itemStruct")
                    or _dig(data, "ItemModule")
                )
                if isinstance(item_struct, dict):
                    # itemStruct trực tiếp
                    stats = item_struct.get("stats", {})
                elif isinstance(item_struct, dict):
                    # ItemModule là {video_id: itemStruct}
                    first = next(iter(item_struct.values()), {})
                    stats = first.get("stats", {})
                else:
                    stats = {}

                if stats:
                    views  = _parse_count(stats.get("playCount",   views))
                    likes  = _parse_count(stats.get("diggCount",   likes))
                    saves  = _parse_count(stats.get("collectCount", saves))
                    shares = _parse_count(stats.get("shareCount",  shares))
                    if not title or title == "N/A":
                        title = item_struct.get("desc", "N/A")[:80] if isinstance(item_struct, dict) else "N/A"
                    log.info(f"[VideoInfo] Đọc được từ UNIVERSAL_DATA: views={views}")
            except Exception as exc:
                log.warning(f"[VideoInfo] Parse UNIVERSAL_DATA lỗi: {exc}")

        # ── Fallback: regex trực tiếp trong HTML ──────────────────────────
        if views == 0:
            def _rex(pattern: str) -> int:
                fm = re.search(pattern, html, re.IGNORECASE)
                if not fm:
                    return 0
                return _parse_count(fm.group(1))

            views  = views  or _rex(r'"playCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')
            likes  = likes  or _rex(r'"diggCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')
            saves  = saves  or _rex(r'"collectCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')
            shares = shares or _rex(r'"shareCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')

            if title == "N/A":
                tm = re.search(r'<title>([^<]+)</title>', html)
                if tm:
                    title = tm.group(1).strip()[:80]

            log.info(f"[VideoInfo] Fallback regex: views={views}")

    except Exception as exc:
        log.error(f"[VideoInfo] Scrape lỗi: {exc}")

    if not video_id:
        video_id = "unknown"

    return {
        "video_id": video_id,
        "url":      url,
        "title":    title,
        "views":    views,
        "likes":    likes,
        "saves":    saves,
        "shares":   shares,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROXY CHECK
# ─────────────────────────────────────────────────────────────────────────────

async def check_proxy(proxy: str) -> bool:
    try:
        sess = cfreqs.AsyncSession(impersonate="chrome110")
        resp = await sess.get(
            PROXY_TEST_URL,
            proxies={"http": proxy, "https": proxy},
            timeout=PROXY_CHECK_TIMEOUT,
        )
        return resp.status_code < 400
    except Exception:
        return False


async def check_all_proxies(proxies: List[str]) -> Dict[str, bool]:
    tasks   = {p: asyncio.create_task(check_proxy(p)) for p in proxies}
    results = {}
    for p, task in tasks.items():
        try:
            results[p] = await task
        except Exception:
            results[p] = False
    return results


# ─────────────────────────────────────────────────────────────────────────────
# VIEW BUFF ENGINE
# ─────────────────────────────────────────────────────────────────────────────

TLS_PROFILES = [
    "chrome124", "chrome120", "chrome110",
    "chrome107", "chrome104", "safari17_0",
    "safari16_0", "safari15_5",
]

DEVICE_BRANDS = ["samsung", "xiaomi", "huawei", "oppo", "vivo", "realme"]
OS_VERSIONS   = ["12", "13", "14"]
OS_APIS       = {"12": "32", "13": "33", "14": "34"}


def _rand_digits(n: int) -> str:
    return "".join(random.choices(string.digits, k=n))


def _rand_hex(n: int) -> str:
    return "".join(random.choices("0123456789abcdef", k=n))


def make_device() -> dict:
    """Tạo thông số thiết bị ngẫu nhiên thực tế."""
    os_ver    = random.choice(OS_VERSIONS)
    brand     = random.choice(DEVICE_BRANDS)
    device_id = _rand_digits(19)
    iid       = _rand_digits(19)   # install_id
    openudid  = _rand_hex(16)
    cdid      = str(uuid.uuid4())  # client device id
    return {
        "device_id":      device_id,
        "iid":            iid,
        "openudid":       openudid,
        "cdid":           cdid,
        "device_brand":   brand,
        "device_type":    f"{brand.upper()}-SM-G991B",
        "os":             "android",
        "os_version":     os_ver,
        "os_api":         OS_APIS[os_ver],
        "resolution":     random.choice(["1080*2400", "1080*2340", "1440*3200"]),
        "dpi":            str(random.choice([420, 480, 560])),
    }


def build_view_payload(video_id: str, device: dict, t_start: int, play_delta: int) -> dict:
    """
    Xây dựng payload đầy đủ cho TikTok playtime API.
    Các trường này là bắt buộc để server nhận request.
    """
    ver_code = random.choice(["320403", "310303", "300904"])
    ver_name = {"320403": "32.4.3", "310303": "31.3.3", "300904": "30.9.4"}[ver_code]
    return {
        # App info
        "aid":                "1233",
        "app_name":           "musical_ly",
        "app_language":       "en",
        "version_code":       ver_code,
        "version_name":       ver_name,
        "channel":            random.choice(["googleplay", "xiaomi", "huawei_store"]),
        "ab_version":         ver_name,
        "ssmix":              "a",
        "manifest_version_code": ver_code,
        "update_version_code":   ver_code,
        # Device
        "device_id":          device["device_id"],
        "iid":                device["iid"],
        "openudid":           device["openudid"],
        "cdid":               device["cdid"],
        "device_brand":       device["device_brand"],
        "device_type":        device["device_type"],
        "device_platform":    "android",
        "os":                 "android",
        "os_version":         device["os_version"],
        "os_api":             device["os_api"],
        "resolution":         device["resolution"],
        "dpi":                device["dpi"],
        # Network
        "ac":                 random.choice(["wifi", "4g", "5g"]),
        "ac2":                "wifi5g",
        "carrier_region":     random.choice(["US", "GB", "CA", "AU", "SG"]),
        "sys_region":         "US",
        "region":             "US",
        "timezone_name":      "America/New_York",
        "timezone_offset":    "-14400",
        "language":           "en",
        "locale":             "en_US",
        # Video play info
        "aweme_id":           video_id,
        "action_time":        str(t_start),
        "play_delta":         str(play_delta),
        "ts":                 str(t_start),
        "is_play_phone_mode": "0",
        "play_exit_type":     "3" if play_delta > 0 else "0",
        # Anti-detection
        "cronet_version":     "TTNetVersion:6c7669b9 2023-12-18",
        "build_number":       ver_name,
    }


async def send_single_view(
    video_id:   str,
    proxy:      Optional[str],
    worker_idx: int,
) -> bool:
    """
    Gửi 1 view theo 2 pha với thiết bị giả lập thực tế:
      Pha 1 — play_delta=0  (bắt đầu xem)
      Delay — 10–25s        (mô phỏng xem thực)
      Pha 2 — play_delta=N  (kết thúc xem, N = thời gian đã xem)
    Xoay vòng giữa nhiều API server.
    """
    tls    = TLS_PROFILES[worker_idx % len(TLS_PROFILES)]
    server = TIKTOK_API_SERVERS[worker_idx % len(TIKTOK_API_SERVERS)]
    api_url = server + TIKTOK_PLAY_PATH

    sess = cfreqs.AsyncSession(impersonate=tls)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}

    device  = make_device()
    t_start = int(time.time())

    # User-Agent khớp với app version
    ver_name = "30.9.4"
    ua = (
        f"com.zhiliaoapp.musically/{device['iid']} "
        f"(Linux; U; Android {device['os_version']}; en_US; {device['device_type']}; "
        f"Build/TP1A.220624.014; Cronet/TTNetVersion:6c7669b9 2023-12-18 QuicVersion:0144d358 2023-12-14)"
    )

    request_headers = {
        "User-Agent":     ua,
        "Content-Type":   "application/x-www-form-urlencoded",
        "X-SS-REQ-TICKET": str(int(time.time() * 1000)),
        "sdk-version":    "2",
        "Accept-Encoding": "gzip",
    }

    try:
        # Pha 1: bắt đầu xem
        payload1 = build_view_payload(video_id, device, t_start, 0)
        await sess.post(api_url, data=payload1, headers=request_headers, timeout=12)

        # Mô phỏng thời gian xem: 10–25 giây
        watch_time = random.randint(10, 25)
        await asyncio.sleep(watch_time)

        # Pha 2: kết thúc xem
        t_end   = int(time.time())
        payload2 = build_view_payload(video_id, device, t_end, t_end - t_start)
        r2 = await sess.post(api_url, data=payload2, headers=request_headers, timeout=12)

        return r2.status_code < 400

    except Exception:
        return False


async def buff_worker(
    worker_idx: int,
    video_id:   str,
    stop_event: asyncio.Event,
    counter:    List[int],
) -> None:
    """1 worker chạy liên tục cho đến khi stop_event được set."""
    while not stop_event.is_set():
        proxy   = STATE.get_proxy(worker_idx + int(time.time()))
        success = await send_single_view(video_id, proxy, worker_idx)
        if success:
            counter[0] += 1
        await asyncio.sleep(1 + (worker_idx % 5) * 0.5)


async def run_buff_session(app: Application, session: BuffSession) -> None:
    """
    Vòng lặp chính:
      • Khởi chạy N worker coroutines
      • Mỗi REPORT_INTERVAL giây → fetch view → gửi báo cáo Telegram
      • Dừng khi stop_event set
    """
    chat_id  = session.chat_id
    video_id = session.video_id
    num_w    = STATE.workers
    counter: List[int] = [0]

    worker_tasks = [
        asyncio.create_task(buff_worker(i, video_id, session.stop_event, counter))
        for i in range(num_w)
    ]

    last_report = time.time()

    try:
        while not session.stop_event.is_set():
            await asyncio.sleep(5)
            now = time.time()

            if now - last_report >= REPORT_INTERVAL:
                last_report = now
                info          = await fetch_video_info(session.video_url)
                current_views = info["views"] if info else session.initial_views
                gained        = current_views - session.initial_views
                elapsed       = now - session.start_time

                text = (
                    f"📊 *Báo cáo tiến độ*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎬 Video ID: `{video_id}`\n"
                    f"⏱ Thời gian: `{fmt_duration(elapsed)}`\n"
                    f"👁 View hiện tại: `{fmt_number(current_views)}`\n"
                    f"📈 View đã tăng: `\\+{fmt_number(gained)}`\n"
                    f"🚀 Requests gửi: `{fmt_number(counter[0])}`\n"
                    f"⚡ Luồng: `{num_w}`\n"
                    f"🔌 Proxy: `{len(STATE.proxies)}`\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"_Dùng /view\\_stop để dừng_"
                )
                await app.bot.send_message(
                    chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2
                )

    finally:
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        elapsed       = time.time() - session.start_time
        info          = await fetch_video_info(session.video_url)
        current_views = info["views"] if info else session.initial_views
        gained        = current_views - session.initial_views

        end_text = (
            f"🛑 *Đã dừng buff*\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎬 Video ID: `{video_id}`\n"
            f"⏱ Tổng thời gian: `{fmt_duration(elapsed)}`\n"
            f"👁 View ban đầu: `{fmt_number(session.initial_views)}`\n"
            f"👁 View hiện tại: `{fmt_number(current_views)}`\n"
            f"📈 Tổng view tăng: `\\+{fmt_number(gained)}`\n"
            f"🚀 Tổng requests: `{fmt_number(counter[0])}`"
        )
        await app.bot.send_message(
            chat_id=chat_id, text=end_text, parse_mode=ParseMode.MARKDOWN_V2
        )
        STATE.sessions.pop(chat_id, None)


# ─────────────────────────────────────────────────────────────────────────────
# BOT COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ip = get_server_ip()
    text = (
        f"🤖 *TikTok View Buff Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 IP Server: `{escape_md(ip)}`\n"
        f"⚡ Workers mặc định: `{STATE.workers}`\n"
        f"🔌 Proxies loaded: `{len(STATE.proxies)}`\n\n"
        f"*Danh sách lệnh:*\n"
        f"`/view <url>` — Bắt đầu buff view\n"
        f"`/view_stop` — Dừng buff\n"
        f"`/proxy_add <p1> <p2>` — Thêm proxy\n"
        f"`/proxy_check` — Kiểm tra proxy sống\n"
        f"`/proxy_list` — Xem danh sách proxy\n"
        f"`/worker <số>` — Đặt số luồng"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id

    if chat_id in STATE.sessions:
        await update.message.reply_text(
            "⚠️ Đang buff video khác\\. Dùng /view\\_stop trước\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Thiếu URL\\.\nVí dụ: `/view https://www\\.tiktok\\.com/@user/video/\\.\\.\\.`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    url = context.args[0]
    msg: Message = await update.message.reply_text(
        f"🔍 Đang quét thông tin video\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    info = await fetch_video_info(url)
    if not info or info.get("video_id") == "unknown":
        await msg.edit_text(
            "❌ Không lấy được thông tin video\\.\n\n"
            "Nguyên nhân có thể:\n"
            "• TikTok chặn IP server \\(cần proxy\\)\n"
            "• URL không đúng định dạng\n"
            "• Video đã bị xóa hoặc private\n\n"
            "Thử thêm proxy rồi dùng lại\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    video_id = info["video_id"]
    scan_text = (
        f"✅ *Đã quét video*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎬 ID: `{video_id}`\n"
        f"📝 Title: {escape_md(info['title'])}\n"
        f"👁 View: `{fmt_number(info['views'])}`\n"
        f"❤️ Tym: `{fmt_number(info['likes'])}`\n"
        f"🔖 Đã lưu: `{fmt_number(info['saves'])}`\n"
        f"📤 Share: `{fmt_number(info['shares'])}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🚀 Bắt đầu buff với `{STATE.workers}` luồng\\.\\.\\."
    )
    await msg.edit_text(scan_text, parse_mode=ParseMode.MARKDOWN_V2)

    session = BuffSession(
        chat_id        = chat_id,
        video_url      = url,
        video_id       = video_id,
        initial_views  = info["views"],
        initial_likes  = info["likes"],
        initial_saves  = info["saves"],
        initial_shares = info["shares"],
    )
    STATE.sessions[chat_id] = session
    task = asyncio.create_task(run_buff_session(context.application, session))
    session.task = task


async def cmd_view_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    session = STATE.sessions.get(chat_id)

    if not session:
        await update.message.reply_text("⚠️ Không có phiên buff nào đang chạy\\.",
                                        parse_mode=ParseMode.MARKDOWN_V2)
        return

    await update.message.reply_text("⏹ Đang dừng\\.\\.\\. Chờ báo cáo cuối cùng\\.",
                                    parse_mode=ParseMode.MARKDOWN_V2)
    session.stop_event.set()


async def cmd_proxy_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /proxy_add [loại] proxy1 proxy2 ...
    Loại tùy chọn: http (mặc định), socks4, socks5
    Ví dụ:
      /proxy_add 1.2.3.4:8080
      /proxy_add socks5 1.2.3.4:1080
      /proxy_add 1.2.3.4:6754:user:pass   ← Webshare tự nhận dạng
    """
    if not context.args:
        await update.message.reply_text(
            "❌ Cú pháp:\n"
            "`/proxy_add proxy1 proxy2` — HTTP mặc định\n"
            "`/proxy_add socks4 proxy1 proxy2`\n"
            "`/proxy_add socks5 proxy1 proxy2`\n"
            "`/proxy_add host:port:user:pass` — Webshare tự nhận\n\n"
            "Hoặc gửi file \\.txt \\(tên file chứa socks4/socks5 sẽ tự nhận loại\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    args = list(context.args)

    # Phát hiện scheme từ arg đầu tiên nếu là keyword
    scheme = "http"
    if args[0].lower() in ("http", "socks4", "socks5"):
        scheme = args.pop(0).lower()

    if not args:
        await update.message.reply_text("❌ Chưa có proxy nào sau loại\\.",
                                        parse_mode=ParseMode.MARKDOWN_V2)
        return

    normalized = [normalize_proxy(p, scheme) for p in args if p.strip()]
    normalized = [p for p in normalized if p]
    added      = STATE.add_proxies(normalized)

    scheme_emoji = {"http": "🌐", "socks4": "🔷", "socks5": "🔶"}.get(scheme, "🌐")
    await update.message.reply_text(
        f"✅ Đã thêm `{added}` proxy `{scheme}` mới\n"
        f"📊 Tổng: `{len(STATE.proxies)}` proxy",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_proxy_add_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Nhận file .txt chứa proxy.
    Tự nhận loại từ tên file:
      socks5_*.txt  → socks5://
      socks4_*.txt  → socks4://
      *             → http://
    Hỗ trợ tất cả định dạng:
      host:port
      host:port:user:pass  (Webshare)
      user:pass@host:port
      scheme://host:port
    """
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".txt"):
        return

    filename = doc.file_name or ""
    scheme   = detect_scheme_from_filename(filename)
    emoji    = {"http": "🌐", "socks4": "🔷", "socks5": "🔶"}.get(scheme, "🌐")

    await update.message.reply_text(
        f"📂 Đang xử lý file `{escape_md(filename)}`\n"
        f"{emoji} Loại proxy nhận dạng: `{scheme}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    tg_file = await context.bot.get_file(doc.file_id)
    content = await tg_file.download_as_bytearray()
    lines   = content.decode("utf-8", errors="ignore").splitlines()

    # Lọc và chuẩn hóa, bỏ qua dòng trống và comment (#)
    normalized = []
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = normalize_proxy(line, scheme)
        if p:
            normalized.append(p)

    added = STATE.add_proxies(normalized)
    await update.message.reply_text(
        f"✅ *Đã thêm proxy từ file*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📄 File: `{escape_md(filename)}`\n"
        f"{emoji} Loại: `{scheme}`\n"
        f"➕ Thêm mới: `{added}`\n"
        f"📊 Tổng pool: `{len(STATE.proxies)}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_proxy_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not STATE.proxies:
        await update.message.reply_text("⚠️ Chưa có proxy nào\\.",
                                        parse_mode=ParseMode.MARKDOWN_V2)
        return

    total = len(STATE.proxies)
    msg   = await update.message.reply_text(
        f"🔍 Đang kiểm tra `{total}` proxy\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    results = await check_all_proxies(STATE.proxies)
    alive   = {p for p, ok in results.items() if ok}
    dead    = {p for p, ok in results.items() if not ok}
    STATE.remove_dead_proxies(dead)

    await msg.edit_text(
        f"✅ *Kết quả kiểm tra proxy*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 Sống: `{len(alive)}`\n"
        f"🔴 Chết \\(đã xóa\\): `{len(dead)}`\n"
        f"📊 Còn lại: `{len(STATE.proxies)}`",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_proxy_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not STATE.proxies:
        await update.message.reply_text("⚠️ Chưa có proxy nào\\.",
                                        parse_mode=ParseMode.MARKDOWN_V2)
        return

    # Đếm theo loại
    counts = {"http": 0, "socks4": 0, "socks5": 0, "other": 0}
    for p in STATE.proxies:
        if p.startswith("socks5"):
            counts["socks5"] += 1
        elif p.startswith("socks4"):
            counts["socks4"] += 1
        elif p.startswith("http"):
            counts["http"] += 1
        else:
            counts["other"] += 1

    # Hiển thị tối đa 15 proxy
    display = STATE.proxies[:15]
    def _fmt(p: str) -> str:
        if p.startswith("socks5"):
            return f"🔶 `{escape_md(p)}`"
        if p.startswith("socks4"):
            return f"🔷 `{escape_md(p)}`"
        return f"🌐 `{escape_md(p)}`"

    lines = "\n".join(_fmt(p) for p in display)
    extra = (f"\n_\\.\\.\\.và {len(STATE.proxies)-15} proxy khác_"
             if len(STATE.proxies) > 15 else "")

    stat = (
        f"🌐 HTTP: `{counts['http']}` \\| "
        f"🔷 SOCKS4: `{counts['socks4']}` \\| "
        f"🔶 SOCKS5: `{counts['socks5']}`"
    )

    await update.message.reply_text(
        f"📋 *Danh sách proxy* \\({len(STATE.proxies)} tổng\\)\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{stat}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"{lines}{extra}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_worker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(
            f"⚡ Số luồng hiện tại: `{STATE.workers}`\n"
            f"Cú pháp: `/worker 300`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    try:
        n = int(context.args[0])
        if n < 1:
            raise ValueError
        if n > 1000:
            await update.message.reply_text("⚠️ Tối đa 1000 luồng\\.",
                                            parse_mode=ParseMode.MARKDOWN_V2)
            return
        STATE.workers = n
        await update.message.reply_text(
            f"✅ Đặt số luồng: `{n}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Số luồng không hợp lệ\\. Ví dụ: `/worker 300`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Chưa đặt TELEGRAM_BOT_TOKEN.\n"
            "Railway: vào Variables → thêm TELEGRAM_BOT_TOKEN=<token>"
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("view",        cmd_view))
    app.add_handler(CommandHandler("view_stop",   cmd_view_stop))
    app.add_handler(CommandHandler("proxy_add",   cmd_proxy_add))
    app.add_handler(CommandHandler("proxy_check", cmd_proxy_check))
    app.add_handler(CommandHandler("proxy_list",  cmd_proxy_list))
    app.add_handler(CommandHandler("worker",      cmd_worker))

    # Nhận file .txt để add proxy
    app.add_handler(MessageHandler(
        filters.Document.MimeType("text/plain"),
        cmd_proxy_add_file,
    ))

    log.info(f"Bot khởi động — IP: {get_server_ip()}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
