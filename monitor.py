import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
STATE_FILE = DATA_DIR / "state.json"
SNAPSHOT_FILE = DATA_DIR / "last_snapshot.txt"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BOOTSTRAP_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 SETAM-html-monitor/1.0 (+https://example.local; contact: admin@example.local)",
)
TG_POLL_TIMEOUT = int(os.getenv("TG_POLL_TIMEOUT", "20"))

DEFAULT_CONFIG: Dict[str, Any] = {
    "monitor_url": os.getenv("MONITOR_URL", "").strip().strip('"').strip("'"),
    "notify_chat_id": BOOTSTRAP_CHAT_ID,
    "admin_chat_ids": [BOOTSTRAP_CHAT_ID] if BOOTSTRAP_CHAT_ID else [],
    "check_interval": int(os.getenv("CHECK_INTERVAL", "900")),
    "request_timeout": int(os.getenv("REQUEST_TIMEOUT", "30")),
    "notify_on_first_run": os.getenv("NOTIFY_ON_FIRST_RUN", "false").lower() in {"1", "true", "yes", "y", "on"},
    "send_screenshot_on_change": os.getenv("SEND_SCREENSHOT_ON_CHANGE", "true").lower() in {"1", "true", "yes", "y", "on"},
    "send_screenshot_on_first_run": os.getenv("SEND_SCREENSHOT_ON_FIRST_RUN", "false").lower() in {"1", "true", "yes", "y", "on"},
    "screenshot_full_page": os.getenv("SCREENSHOT_FULL_PAGE", "true").lower() in {"1", "true", "yes", "y", "on"},
    "screenshot_width": int(os.getenv("SCREENSHOT_WIDTH", "1365")),
    "screenshot_height": int(os.getenv("SCREENSHOT_HEIGHT", "1800")),
    "screenshot_wait_ms": int(os.getenv("SCREENSHOT_WAIT_MS", "3000")),
    "ignore_patterns": [p.strip() for p in os.getenv("IGNORE_PATTERNS", "Час серверу:.*").split(",") if p.strip()],
    "allow_non_setam_urls": os.getenv("ALLOW_NON_SETAM_URLS", "false").lower() in {"1", "true", "yes", "y", "on"},
}

state_lock = threading.RLock()
force_check_event = threading.Event()
stop_event = threading.Event()
last_update_id: Optional[int] = None


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def safe_int(value: Any, fallback: int, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        return fallback
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def parse_bool(value: str) -> Optional[bool]:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on", "да", "вкл", "enable", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", "нет", "выкл", "disable", "disabled"}:
        return False
    return None


def load_state_unlocked() -> Dict[str, Any]:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state_unlocked(state: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def get_state() -> Dict[str, Any]:
    with state_lock:
        state = load_state_unlocked()
        config = state.get("config") or {}
        merged_config = {**DEFAULT_CONFIG, **config}

        # Keep backwards compatibility if older state stored url at top level.
        if not merged_config.get("monitor_url") and state.get("url"):
            merged_config["monitor_url"] = state.get("url")

        # Bootstrap chat can be changed later from bot, but first run needs it.
        if BOOTSTRAP_CHAT_ID:
            admins = set(str(x) for x in merged_config.get("admin_chat_ids", []) if str(x))
            admins.add(str(BOOTSTRAP_CHAT_ID))
            merged_config["admin_chat_ids"] = sorted(admins)
            if not merged_config.get("notify_chat_id"):
                merged_config["notify_chat_id"] = BOOTSTRAP_CHAT_ID

        state["config"] = merged_config
        save_state_unlocked(state)
        return state


def update_config(**updates: Any) -> Dict[str, Any]:
    with state_lock:
        state = get_state()
        config = state.get("config") or {}
        config.update(updates)
        state["config"] = config
        save_state_unlocked(state)
        return state


def reset_baseline() -> None:
    with state_lock:
        state = get_state()
        for key in ["hash", "lots", "last_checked_at", "last_changed_at"]:
            state.pop(key, None)
        save_state_unlocked(state)
        try:
            SNAPSHOT_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def save_snapshot(text: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(text, encoding="utf-8")


def load_snapshot() -> str:
    if not SNAPSHOT_FILE.exists():
        return ""
    return SNAPSHOT_FILE.read_text(encoding="utf-8", errors="replace")


def validate_startup() -> None:
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not BOOTSTRAP_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(f"Missing required env vars: {', '.join(missing)}")


def validate_url(url: str, allow_non_setam: bool = False) -> Tuple[bool, str]:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False, "URL должен начинаться с http:// или https://"
    if not parsed.netloc:
        return False, "URL без домена"
    if not allow_non_setam and parsed.netloc.lower() not in {"setam.net.ua", "www.setam.net.ua"}:
        return False, "По умолчанию разрешен только setam.net.ua. Включи /allow_non_setam on, если точно нужно другое."
    return True, "ok"


def fetch_html(url: str, timeout: int) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "uk-UA,uk;q=0.9,ru;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def extract_lot_numbers(text: str) -> List[str]:
    return sorted(set(re.findall(r"Номер\s+лоту:\s*(\d+)", text)))


def normalize_html(html: str, ignore_patterns: List[str]) -> Tuple[str, List[str]]:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    text = soup.get_text("\n", strip=True)

    volatile_patterns = [
        r"_csrf[^\n]*",
        r"csrf[^\n]*",
        r"session[^\n]*",
        r"Час\s+серверу:\s*[^\n]*",
    ]
    volatile_patterns.extend(ignore_patterns or [])

    for pattern in volatile_patterns:
        try:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            # Invalid user regex should not break monitoring.
            pass

    lines = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)

    normalized = "\n".join(lines)
    lots = extract_lot_numbers(normalized)
    return normalized, lots


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_diff(old: str, new: str, max_lines: int = 60) -> str:
    diff = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    if not diff:
        return ""
    trimmed = diff[:max_lines]
    suffix = "" if len(diff) <= max_lines else f"\n... trimmed {len(diff) - max_lines} diff lines"
    return "\n".join(trimmed) + suffix


def tg_api(method: str, payload: Optional[dict] = None, timeout: int = 30, files: Optional[dict] = None) -> dict:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}"
    if files:
        response = requests.post(url, data=payload or {}, files=files, timeout=timeout)
    else:
        response = requests.post(url, json=payload or {}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
    return data


def tg_send(chat_id: str, text: str) -> None:
    max_len = 3900
    chunks = [text[i : i + max_len] for i in range(0, len(text), max_len)] or [text]
    timeout = get_state()["config"].get("request_timeout", 30)
    for chunk in chunks:
        tg_api(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )


def notify(text: str) -> None:
    config = get_state()["config"]
    chat_id = str(config.get("notify_chat_id") or BOOTSTRAP_CHAT_ID)
    tg_send(chat_id, text)


def make_screenshot(url: str, config: Dict[str, Any]) -> Path:
    tmp = tempfile.NamedTemporaryFile(prefix="setam_", suffix=".png", delete=False)
    screenshot_path = Path(tmp.name)
    tmp.close()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            page = browser.new_page(
                viewport={
                    "width": safe_int(config.get("screenshot_width"), 1365, 320, 3840),
                    "height": safe_int(config.get("screenshot_height"), 1800, 320, 10000),
                },
                locale="uk-UA",
            )
            timeout_ms = safe_int(config.get("request_timeout"), 30, 5, 180) * 1000
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            wait_ms = safe_int(config.get("screenshot_wait_ms"), 3000, 0, 60000)
            if wait_ms > 0:
                page.wait_for_timeout(wait_ms)
            page.screenshot(path=str(screenshot_path), full_page=bool(config.get("screenshot_full_page", True)))
            browser.close()
        return screenshot_path
    except Exception:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def tg_send_photo(chat_id: str, photo_path: Path, caption: str = "") -> None:
    timeout = max(safe_int(get_state()["config"].get("request_timeout"), 30, 5, 180), 60)
    with photo_path.open("rb") as photo:
        tg_api(
            "sendPhoto",
            payload={"chat_id": chat_id, "caption": caption[:1000]},
            files={"photo": (photo_path.name, photo, "image/png")},
            timeout=timeout,
        )


def send_page_screenshot(chat_id: Optional[str] = None, reason: str = "manual") -> None:
    config = get_state()["config"]
    url = config.get("monitor_url")
    if not url:
        raise RuntimeError("MONITOR_URL не задан. Используй /url <URL>")
    target_chat = str(chat_id or config.get("notify_chat_id") or BOOTSTRAP_CHAT_ID)
    screenshot_path: Optional[Path] = None
    try:
        screenshot_path = make_screenshot(url, config)
        tg_send_photo(target_chat, screenshot_path, caption=f"📸 SETAM screenshot: {reason}\n{utc_now()}")
    finally:
        if screenshot_path:
            try:
                screenshot_path.unlink(missing_ok=True)
            except Exception as exc:
                print(f"Failed to remove temporary screenshot: {exc}", file=sys.stderr, flush=True)


def format_change_message(old_hash: str, new_hash: str, old_lots: List[str], new_lots: List[str], diff: str) -> str:
    config = get_state()["config"]
    added = sorted(set(new_lots) - set(old_lots))
    removed = sorted(set(old_lots) - set(new_lots))

    parts = [
        "🔔 SETAM page changed",
        f"Time: {utc_now()}",
        f"URL: {config.get('monitor_url')}",
        "",
        f"Old hash: {old_hash[:12] if old_hash else 'none'}",
        f"New hash: {new_hash[:12]}",
        f"Lots before/after: {len(old_lots)} → {len(new_lots)}",
    ]
    if added:
        parts.append(f"New lot numbers: {', '.join(added[:30])}")
    if removed:
        parts.append(f"Removed lot numbers: {', '.join(removed[:30])}")
    if diff:
        parts.extend(["", "Diff preview:", diff[:2500]])
    return "\n".join(parts)


def check_once(manual_chat_id: Optional[str] = None) -> str:
    with state_lock:
        state = get_state()
        config = state.get("config") or {}
        url = config.get("monitor_url")
        if not url:
            raise RuntimeError("URL не задан. Используй /url <URL>")
        old_hash = state.get("hash", "")
        old_lots = state.get("lots", [])
        old_snapshot = load_snapshot()

    timeout = safe_int(config.get("request_timeout"), 30, 5, 180)
    html = fetch_html(url, timeout=timeout)
    normalized, new_lots = normalize_html(html, config.get("ignore_patterns", []))
    new_hash = sha256_text(normalized)
    first_run = not old_hash

    if first_run:
        with state_lock:
            state = get_state()
            state.update({"hash": new_hash, "lots": new_lots, "last_checked_at": utc_now(), "url": url})
            save_snapshot(normalized)
            save_state_unlocked(state)
        msg = f"✅ SETAM monitor initialized\nTime: {utc_now()}\nURL: {url}\nHash: {new_hash[:12]}\nLots found: {len(new_lots)}"
        if config.get("notify_on_first_run") or manual_chat_id:
            tg_send(str(manual_chat_id or config.get("notify_chat_id")), msg)
            if config.get("send_screenshot_on_first_run"):
                send_page_screenshot(str(manual_chat_id or config.get("notify_chat_id")), "first run")
        print(f"[{utc_now()}] first snapshot saved: {new_hash[:12]}, lots={len(new_lots)}", flush=True)
        return msg

    if new_hash != old_hash:
        diff = build_diff(old_snapshot, normalized)
        message = format_change_message(old_hash, new_hash, old_lots, new_lots, diff)
        notify(message)
        if config.get("send_screenshot_on_change"):
            send_page_screenshot(reason="page changed")
        with state_lock:
            state = get_state()
            state.update({
                "hash": new_hash,
                "lots": new_lots,
                "last_checked_at": utc_now(),
                "last_changed_at": utc_now(),
                "url": url,
            })
            save_snapshot(normalized)
            save_state_unlocked(state)
        print(f"[{utc_now()}] changed: {old_hash[:12]} -> {new_hash[:12]}", flush=True)
        return message

    with state_lock:
        state = get_state()
        state["last_checked_at"] = utc_now()
        save_state_unlocked(state)
    msg = f"✅ No change\nTime: {utc_now()}\nHash: {new_hash[:12]}\nLots: {len(new_lots)}\nURL: {url}"
    if manual_chat_id:
        tg_send(str(manual_chat_id), msg)
    print(f"[{utc_now()}] no change: {new_hash[:12]}, lots={len(new_lots)}", flush=True)
    return msg


def is_admin(chat_id: str) -> bool:
    config = get_state()["config"]
    admins = {str(x) for x in config.get("admin_chat_ids", []) if str(x)}
    return str(chat_id) in admins


def require_admin(chat_id: str) -> bool:
    if is_admin(chat_id):
        return True
    try:
        tg_send(str(chat_id), "⛔️ Access denied. Этот chat id не в admin list.")
    except Exception:
        pass
    return False


def config_text() -> str:
    state = get_state()
    c = state["config"]
    return (
        "⚙️ Current config\n"
        f"URL: {c.get('monitor_url') or 'not set'}\n"
        f"Notify chat: {c.get('notify_chat_id')}\n"
        f"Admin chats: {', '.join(map(str, c.get('admin_chat_ids', [])))}\n"
        f"Interval: {c.get('check_interval')} sec\n"
        f"Request timeout: {c.get('request_timeout')} sec\n"
        f"Screenshot on change: {c.get('send_screenshot_on_change')}\n"
        f"Screenshot on first run: {c.get('send_screenshot_on_first_run')}\n"
        f"Notify on first run: {c.get('notify_on_first_run')}\n"
        f"Full-page screenshot: {c.get('screenshot_full_page')}\n"
        f"Screenshot size: {c.get('screenshot_width')}x{c.get('screenshot_height')}\n"
        f"Screenshot wait: {c.get('screenshot_wait_ms')} ms\n"
        f"Allow non-SETAM URLs: {c.get('allow_non_setam_urls')}\n"
        f"Ignore patterns: {', '.join(c.get('ignore_patterns', [])) or 'none'}\n"
        f"Last checked: {state.get('last_checked_at', 'never')}\n"
        f"Last changed: {state.get('last_changed_at', 'never')}\n"
        f"Hash: {(state.get('hash') or '')[:12] or 'none'}\n"
        f"Lots: {len(state.get('lots', []))}"
    )


def help_text() -> str:
    return (
        "SETAM monitor commands:\n\n"
        "/url <URL> — задать URL мониторинга и сбросить baseline\n"
        "/show_url — показать текущий URL\n"
        "/check_now — проверить сейчас\n"
        "/screenshot — отправить скрин текущей страницы\n"
        "/status или /config — показать конфиг и состояние\n"
        "/interval <seconds> — интервал проверки, минимум 60 сек\n"
        "/timeout <seconds> — request timeout, 5-180 сек\n"
        "/screenshot_size <width> <height> — размер viewport, например 1365 1800\n"
        "/screenshot_full_page on|off — full-page screenshot\n"
        "/screenshot_wait <ms> — задержка перед скрином\n"
        "/screenshot_on_change on|off — слать скрин при изменении\n"
        "/screenshot_on_first_run on|off — скрин при первом baseline\n"
        "/notify_on_first_run on|off — уведомление при первом baseline\n"
        "/ignore_add <regex> — добавить regex в ignore list\n"
        "/ignore_list — показать ignore patterns\n"
        "/ignore_clear — очистить ignore patterns\n"
        "/reset — сбросить hash/snapshot, следующий check создаст baseline\n"
        "/notify_here — слать уведомления в текущий чат\n"
        "/allow_non_setam on|off — разрешить/запретить URL не setam.net.ua"
    )


def handle_command(chat_id: str, text: str) -> None:
    if not text.startswith("/"):
        return
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    arg = arg.strip()

    if command in {"/start", "/help"}:
        if require_admin(chat_id):
            tg_send(chat_id, help_text())
        return

    if not require_admin(chat_id):
        return

    try:
        if command == "/url":
            if not arg:
                tg_send(chat_id, "Использование: /url https://setam.net.ua/...")
                return
            c = get_state()["config"]
            ok, reason = validate_url(arg, allow_non_setam=bool(c.get("allow_non_setam_urls")))
            if not ok:
                tg_send(chat_id, f"❌ URL rejected: {reason}")
                return
            update_config(monitor_url=arg)
            reset_baseline()
            tg_send(chat_id, f"✅ URL updated and baseline reset:\n{arg}\n\nЗапусти /check_now для создания нового baseline.")

        elif command == "/show_url":
            tg_send(chat_id, str(get_state()["config"].get("monitor_url") or "URL not set"))

        elif command == "/check_now":
            tg_send(chat_id, "🔎 Checking now...")
            threading.Thread(target=lambda: check_once(manual_chat_id=chat_id), daemon=True).start()

        elif command == "/screenshot":
            tg_send(chat_id, "📸 Making screenshot...")
            threading.Thread(target=lambda: send_page_screenshot(chat_id=chat_id, reason="manual"), daemon=True).start()

        elif command in {"/status", "/config"}:
            tg_send(chat_id, config_text())

        elif command == "/interval":
            value = safe_int(arg, 900, 60, 86400)
            update_config(check_interval=value)
            force_check_event.set()
            tg_send(chat_id, f"✅ Interval updated: {value} sec")

        elif command == "/timeout":
            value = safe_int(arg, 30, 5, 180)
            update_config(request_timeout=value)
            tg_send(chat_id, f"✅ Request timeout updated: {value} sec")

        elif command == "/screenshot_size":
            parts = arg.split()
            if len(parts) != 2:
                tg_send(chat_id, "Использование: /screenshot_size 1365 1800")
                return
            width = safe_int(parts[0], 1365, 320, 3840)
            height = safe_int(parts[1], 1800, 320, 10000)
            update_config(screenshot_width=width, screenshot_height=height)
            tg_send(chat_id, f"✅ Screenshot viewport updated: {width}x{height}")

        elif command == "/screenshot_full_page":
            value = parse_bool(arg)
            if value is None:
                tg_send(chat_id, "Использование: /screenshot_full_page on|off")
                return
            update_config(screenshot_full_page=value)
            tg_send(chat_id, f"✅ Full-page screenshot: {value}")

        elif command == "/screenshot_wait":
            value = safe_int(arg, 3000, 0, 60000)
            update_config(screenshot_wait_ms=value)
            tg_send(chat_id, f"✅ Screenshot wait updated: {value} ms")

        elif command == "/screenshot_on_change":
            value = parse_bool(arg)
            if value is None:
                tg_send(chat_id, "Использование: /screenshot_on_change on|off")
                return
            update_config(send_screenshot_on_change=value)
            tg_send(chat_id, f"✅ Screenshot on change: {value}")

        elif command == "/screenshot_on_first_run":
            value = parse_bool(arg)
            if value is None:
                tg_send(chat_id, "Использование: /screenshot_on_first_run on|off")
                return
            update_config(send_screenshot_on_first_run=value)
            tg_send(chat_id, f"✅ Screenshot on first run: {value}")

        elif command == "/notify_on_first_run":
            value = parse_bool(arg)
            if value is None:
                tg_send(chat_id, "Использование: /notify_on_first_run on|off")
                return
            update_config(notify_on_first_run=value)
            tg_send(chat_id, f"✅ Notify on first run: {value}")

        elif command == "/notify_here":
            update_config(notify_chat_id=str(chat_id))
            tg_send(chat_id, f"✅ Notifications will be sent to this chat: {chat_id}")

        elif command == "/allow_non_setam":
            value = parse_bool(arg)
            if value is None:
                tg_send(chat_id, "Использование: /allow_non_setam on|off")
                return
            update_config(allow_non_setam_urls=value)
            tg_send(chat_id, f"✅ Allow non-SETAM URLs: {value}")

        elif command == "/ignore_add":
            if not arg:
                tg_send(chat_id, "Использование: /ignore_add <regex>")
                return
            try:
                re.compile(arg)
            except re.error as exc:
                tg_send(chat_id, f"❌ Invalid regex: {exc}")
                return
            state = get_state()
            patterns = list(state["config"].get("ignore_patterns", []))
            patterns.append(arg)
            update_config(ignore_patterns=patterns)
            reset_baseline()
            tg_send(chat_id, "✅ Ignore pattern added. Baseline reset. Run /check_now.")

        elif command == "/ignore_list":
            patterns = get_state()["config"].get("ignore_patterns", [])
            tg_send(chat_id, "Ignore patterns:\n" + ("\n".join(patterns) if patterns else "none"))

        elif command == "/ignore_clear":
            update_config(ignore_patterns=[])
            reset_baseline()
            tg_send(chat_id, "✅ Ignore patterns cleared. Baseline reset. Run /check_now.")

        elif command == "/reset":
            reset_baseline()
            tg_send(chat_id, "✅ Baseline reset. Run /check_now to create a new one.")

        else:
            tg_send(chat_id, "Unknown command. Use /help")
    except Exception as exc:
        try:
            tg_send(chat_id, f"⚠️ Command error: {exc}")
        except Exception:
            print(f"Command error and Telegram send failed: {exc}", file=sys.stderr, flush=True)


def telegram_poll_loop() -> None:
    global last_update_id
    print(f"[{utc_now()}] Telegram command polling started", flush=True)
    while not stop_event.is_set():
        try:
            payload = {"timeout": TG_POLL_TIMEOUT}
            if last_update_id is not None:
                payload["offset"] = last_update_id + 1
            data = tg_api("getUpdates", payload, timeout=TG_POLL_TIMEOUT + 10)
            for update in data.get("result", []):
                last_update_id = update.get("update_id", last_update_id)
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id"))
                text = message.get("text") or ""
                if chat_id and text:
                    handle_command(chat_id, text)
        except Exception as exc:
            print(f"Telegram polling error: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)


def monitor_loop() -> None:
    print(f"[{utc_now()}] SETAM monitor loop started", flush=True)
    while not stop_event.is_set():
        state = get_state()
        config = state["config"]
        interval = safe_int(config.get("check_interval"), 900, 60, 86400)
        url = config.get("monitor_url")

        if url:
            try:
                check_once()
            except Exception as exc:
                error = f"⚠️ SETAM monitor error\nTime: {utc_now()}\nURL: {url}\nError: {exc}"
                print(error, file=sys.stderr, flush=True)
                try:
                    notify(error)
                except Exception as tg_exc:
                    print(f"Telegram send failed: {tg_exc}", file=sys.stderr, flush=True)
        else:
            print(f"[{utc_now()}] URL not set. Use /url <URL> in Telegram.", flush=True)

        force_check_event.wait(timeout=interval)
        force_check_event.clear()


def main() -> None:
    validate_startup()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    get_state()
    tg_thread = threading.Thread(target=telegram_poll_loop, daemon=True)
    tg_thread.start()
    monitor_loop()


if __name__ == "__main__":
    main()
