JARVIS UPDATE PACKAGE
=====================

Files:
- bot.py       Main Telegram bot / userbot entry point
- bot3.py      Jarvis AI, group controls, quiz and inline command menu
- bot4.py      Jarvis Ultra, YouTube tools, links and PDF utilities
- bot_ai.py    Owner-only command-center utilities
- requirements.txt

Changes in this package:
1. /panel now opens the existing inline command menu.
2. The COMMAND PANEL button in /help now uses the existing jc:home callback,
   instead of a callback identifier with no matching handler.
3. Added owner-only quick schedule commands:
   .jarsend in 10m @username | Hello!
   .jarsend in 2h @username | Kaise ho?
   .jarsend at 22:30:15 @username | Good Night
   .jarsend at 22:30:15 here | Quiz shuru!
   .jarsends
   .jarcancel <id>

Duration examples:
- 10m = ten minutes (not 10:00)
- 2h = two hours
- 1h30m = one hour thirty minutes
- 45s = forty-five seconds
- `at` uses India time (Asia/Kolkata); if that time has already passed today,
  it schedules for the next day.

Deployment:
- Keep all four Python files and requirements.txt in the same project root.
- Install dependencies from requirements.txt.
- Keep your existing Render environment variables (API_ID, API_HASH,
  BOT_TOKEN, OWNER_ID, and any API keys you use).
- Keep persistent storage enabled for the DATA_DIR if scheduled jobs must
  survive redeploys/restarts. The process must be online for delivery.

Important:
- Telegram/network/server latency means a scheduled message is not guaranteed
  to arrive at an exact millisecond.
- `here` means the current chat. A username target must be resolvable by the
  connected Telegram account and the account must be allowed to message it.
- This package was syntax-checked. Live Telegram login, API credentials,
  Render deployment, and real message delivery cannot be verified offline.


PRECISION SCHEDULER + LIVE WATCH UPDATE
- .jarsend supports fractional seconds and milliseconds: `in 250ms` or `at 22:30:15.250`.
- ETA now displays milliseconds. The scheduler uses a monotonic high-resolution final wait and starts the send coroutine directly at the trigger point.
- /watch sends a separate Render-hosted live IST clock page. The page shows milliseconds and has Pause/Resume controls.
- Render provides RENDER_EXTERNAL_URL automatically for a deployed web service. The /watch page uses the viewing device's clock; it is a display, not a precision time-sync service.
- Message delivery timing is best-effort; Telegram, network, server scheduling and rate limits prevent a sub-millisecond delivery guarantee.
- Replace bot.py and bot4.py together. Keep bot3.py, bot_ai.py and requirements.txt from this ZIP matched with them.
