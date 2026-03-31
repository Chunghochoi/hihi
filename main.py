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
import re
import socket
import time
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

# TikTok API endpoint gửi view
TIKTOK_VIEW_API = (
    "https://api16-normal-c-useast1a.tiktokv.com/aweme/v1/playtime/"
)

REPORT_INTERVAL    = 30   # giây giữa 2 lần báo cáo
PROXY_CHECK_TIMEOUT = 8   # giây timeout khi test proxy
PROXY_TEST_URL     = "https://www.tiktok.com/"
DEFAULT_WORKERS    = 300

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


def normalize_proxy(raw: str) -> str:
    raw = raw.strip()
    if raw and "://" not in raw:
        raw = "http://" + raw
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# TIKTOK VIDEO INFO
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_short_url(url: str) -> str:
    if "vt.tiktok.com" in url or "vm.tiktok.com" in url:
        try:
            sess = cfreqs.AsyncSession(impersonate="chrome110")
            resp = await sess.head(url, allow_redirects=True, timeout=10)
            return str(resp.url)
        except Exception:
            pass
    return url


async def fetch_video_info(url: str) -> Optional[Dict]:
    """
    Scrape HTML trang video TikTok để lấy:
      video_id, title, views, likes, saves, shares
    """
    url = await resolve_short_url(url)
    video_id = extract_video_id(url)

    try:
        sess = cfreqs.AsyncSession(impersonate="chrome110")
        headers = {
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/110.0.0.0 Safari/537.36",
            "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8",
        }
        resp = await sess.get(url, headers=headers, timeout=15)
        html = resp.text

        def _extract(pattern: str, default: int = 0) -> int:
            m = re.search(pattern, html, re.IGNORECASE)
            if not m:
                return default
            raw = m.group(1).replace(",", "").strip().upper()
            for suffix, mult in {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.items():
                if raw.endswith(suffix):
                    try:
                        return int(float(raw[:-1]) * mult)
                    except ValueError:
                        return default
            try:
                return int(raw)
            except ValueError:
                return default

        views  = _extract(r'"playCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')
        likes  = _extract(r'"diggCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')
        saves  = _extract(r'"collectCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')
        shares = _extract(r'"shareCount"\s*:\s*"?(\d[\d.,KMBkmb]*)"?')

        title_m = re.search(r'<title>([^<]+)</title>', html)
        title   = title_m.group(1).strip()[:60] if title_m else "N/A"

        if not video_id:
            video_id = extract_video_id(str(getattr(resp, "url", url))) or "unknown"

        return {
            "video_id": video_id,
            "url":      url,
            "title":    title,
            "views":    views,
            "likes":    likes,
            "saves":    saves,
            "shares":   shares,
        }

    except Exception as exc:
        log.error(f"[VideoInfo] fetch lỗi: {exc}")
        return None


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
    "chrome110", "chrome107", "chrome104",
    "safari_ios16_0", "safari16_0",
]

APP_INFO = {
    "aid": "1233", "app_name": "musical_ly",
    "version_code": "300904", "version_name": "30.9.4",
    "channel": "googleplay", "device_brand": "samsung",
    "os": "android", "os_version": "13", "os_api": "33",
}


async def send_single_view(
    video_id:   str,
    proxy:      Optional[str],
    worker_idx: int,
) -> bool:
    """
    Gửi 1 view theo 2 pha:
      Pha 1 — play_delta=0  (bắt đầu xem)
      Delay — 8–20s         (mô phỏng xem thực)
      Pha 2 — play_delta=N  (kết thúc xem)
    """
    tls  = TLS_PROFILES[worker_idx % len(TLS_PROFILES)]
    sess = cfreqs.AsyncSession(impersonate=tls)
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}

    device_id    = str(int(time.time() * 1000) + worker_idx)
    t_start      = int(time.time())
    payload_base = {**APP_INFO, "device_id": device_id, "aweme_id": video_id}

    try:
        await sess.post(
            TIKTOK_VIEW_API,
            data={**payload_base, "action_time": t_start, "play_delta": 0},
            timeout=10,
        )
        # Delay ngẫu nhiên 8–20 giây
        delay = 8 + abs(hash(device_id)) % 13
        await asyncio.sleep(delay)

        t_end = int(time.time())
        await sess.post(
            TIKTOK_VIEW_API,
            data={**payload_base, "action_time": t_end, "play_delta": t_end - t_start},
            timeout=10,
        )
        return True
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
    if not info or not info.get("video_id"):
        await msg.edit_text("❌ Không lấy được thông tin video\\. Kiểm tra lại URL\\.",
                            parse_mode=ParseMode.MARKDOWN_V2)
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
    if not context.args:
        await update.message.reply_text(
            "❌ Cú pháp: `/proxy_add proxy1 proxy2 \\.\\.\\.`\n"
            "Hoặc gửi file \\.txt chứa danh sách proxy\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    normalized = [normalize_proxy(p) for p in context.args if p.strip()]
    added      = STATE.add_proxies(normalized)
    await update.message.reply_text(
        f"✅ Đã thêm `{added}` proxy mới\n"
        f"📊 Tổng: `{len(STATE.proxies)}` proxy",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_proxy_add_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".txt"):
        return

    await update.message.reply_text("📂 Đang xử lý file proxy\\.\\.\\.",
                                    parse_mode=ParseMode.MARKDOWN_V2)

    tg_file = await context.bot.get_file(doc.file_id)
    content = await tg_file.download_as_bytearray()
    lines   = content.decode("utf-8", errors="ignore").splitlines()

    normalized = [normalize_proxy(l) for l in lines if l.strip()]
    added      = STATE.add_proxies(normalized)
    await update.message.reply_text(
        f"✅ Đã thêm `{added}` proxy từ file\n"
        f"📊 Tổng: `{len(STATE.proxies)}` proxy",
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

    display = STATE.proxies[:20]
    lines   = "\n".join(f"`{escape_md(p)}`" for p in display)
    extra   = (f"\n_\\.\\.\\.và {len(STATE.proxies)-20} proxy khác_"
               if len(STATE.proxies) > 20 else "")

    await update.message.reply_text(
        f"📋 *Danh sách proxy* \\({len(STATE.proxies)} tổng\\)\n"
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
