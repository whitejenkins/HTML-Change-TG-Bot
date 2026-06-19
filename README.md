# HTML Monitor Telegram Bot

Bot for tracking one or more web pages and sending notifications to Telegram.

## Telegram commands

```text
/start — help
/help — help
/url [N] <URL> — set monitor URL #N and reset its baseline
/delurl [N] [URL] — delete monitor URL #N; optional URL must match
/show_url — show monitored URLs
/check_now — check all URLs now
/screenshot [N] — send screenshot of page #N
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

Examples:
```text
/url 1 https://example.com
/url 2 https://example2.com
/delurl 1 https://example.com
/delurl 2 https://example2.com
```

## .env
```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```
