# SETAM HTML Monitor Telegram Bot

Dockerized monitor for a SETAM/OpenMarket page. It watches a configured URL, normalizes HTML, calculates a SHA-256 hash, and sends Telegram alerts when the page changes. It can also send a screenshot of the page without keeping the screenshot locally.

## What changed in this version

Most settings are now controlled from the Telegram chat. `.env` is only needed for the bot token and the first admin chat id.

`TELEGRAM_CHAT_ID` is used as:

1. the initial admin allowed to control the bot;
2. the default notification chat.

So for a private bot you do **not** need a second chat id variable.

## Files

```text
setam-html-monitor/
├── Dockerfile
├── docker-compose.yml
├── monitor.py
├── requirements.txt
├── .env.example
└── README.md
```

Persistent state is stored in:

```text
./data/state.json
./data/last_snapshot.txt
```

Screenshots are created as temporary `.png` files, sent to Telegram, and immediately deleted.

## Start

```bash
cp .env.example .env
nano .env

docker compose up -d --build
```

Logs:

```bash
docker logs -f setam-monitor
```

Stop:

```bash
docker compose down
```

## Minimal `.env`

```env
TELEGRAM_BOT_TOKEN=123456:PASTE_TOKEN_HERE
TELEGRAM_CHAT_ID=561183451
```

Optional bootstrap URL:

```env
MONITOR_URL="https://setam.net.ua/auctions/search/%D0%9C%D0%B8%D1%80%D0%B3%D0%BE%D1%80%D0%BE%D0%B4/filters/region=16;"
```

You can change URL later from Telegram with `/url`.

## Telegram commands

```text
/start — help
/help — help
/url <URL> — set monitor URL and reset baseline
/show_url — show current URL
/check_now — check now
/screenshot — send screenshot of current page
/status — show current config and state
/config — same as /status
/interval <seconds> — set check interval, min 60 seconds
/timeout <seconds> — set request timeout, 5-180 seconds
/screenshot_size <width> <height> — set screenshot viewport, example 1365 1800
/screenshot_full_page on|off — enable/disable full-page screenshot
/screenshot_wait <ms> — wait before screenshot, example 3000
/screenshot_on_change on|off — send screenshot on page change
/screenshot_on_first_run on|off — send screenshot when baseline is created
/notify_on_first_run on|off — send text alert when baseline is created
/notify_here — send future alerts to this chat
/ignore_add <regex> — ignore matching volatile text in normalized HTML
/ignore_list — show ignore regex list
/ignore_clear — clear ignore regex list and reset baseline
/reset — reset hash/snapshot baseline
/allow_non_setam on|off — allow URLs outside setam.net.ua
```

## Recommended flow

1. Start container.
2. Open the bot in Telegram.
3. Send:

```text
/status
```

4. Set the URL:

```text
/url https://setam.net.ua/auctions/search/Миргород/filters/region=16;
```

5. Create baseline:

```text
/check_now
```

6. Test screenshot:

```text
/screenshot
```

7. Configure interval and screenshot size:

```text
/interval 900
/screenshot_size 1365 1800
/screenshot_full_page on
/screenshot_wait 3000
/screenshot_on_change on
```

## Notes

- Do not set interval too low. The bot enforces a minimum of 60 seconds, but 10-30 minutes is safer.
- If SETAM contains dynamic text that changes every request, add ignore rules with `/ignore_add`.
- If the URL changes, the bot resets baseline to avoid false alerts from the previous page.
- If you regenerate the Telegram token in BotFather, update `.env` and restart the container.
