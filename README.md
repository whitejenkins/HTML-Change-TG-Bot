# HTML Monitor Telegram Bot

Bot for tracking web pages and sending notifications to Telegram.

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

## .env
```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```
