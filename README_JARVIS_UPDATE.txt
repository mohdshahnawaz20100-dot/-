# SHAHNAWAZGRAM — JARVIS

Existing multi-user Telegram bot project with JARVIS AI, group tools, quiz,
scheduler, live precision watch, YouTube utilities, PDF tools and owner panel.

## Voice features
- `/Shahnawaz <text>` sends a male AI voice note.
- Reply to a text message with `/Shahnawaz` to speak that text.
- Devanagari Hindi uses male `hi-IN-MadhurNeural`; Roman Hindi / English uses
  male Indian English `en-IN-PrabhatNeural`.
- Incoming member Telegram voice notes are transcribed with Groq Whisper and
  passed to the existing JARVIS AI.
- In groups, voice understanding follows the existing per-group auto-reply
  setting. In DMs, it follows the existing DM/global settings.
- `/panel` has a separate **/Shahnawaz — Voice** button.

## Render deployment
Keep `bot.py`, `bot3.py`, `bot4.py`, `bot_ai.py`, `requirements.txt`,
`Procfile` and `render.yaml` in the repository root.
Build: `pip install -r requirements.txt`
Start: `python3 bot.py`

Required environment variables: `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`,
`GROQ_API_KEY`. Optional: `GROQ_MODEL`, `GROQ_STT_MODEL`,
`OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `WEATHER_API_KEY`, `NEWS_API_KEY`.

Enable a persistent disk and set `DATA_DIR` to its mount path if scheduled
messages, settings and SQLite data must survive deploys/restarts. `/tmp` is
temporary on Render.

## Scheduler and watch retained
- `.jarsend in 10m @username | Hello!` means ten minutes.
- `.jarsend in 250ms here | Test` accepts milliseconds (best effort).
- `.jarsend at 22:30:15.250 here | Good night` uses Asia/Kolkata.
- `/jarsends` and `.jarcancel <id>` manage quick schedules.
- `/watch` opens the separate live IST clock page. Millisecond display uses the
  viewing device clock; Telegram delivery cannot be guaranteed to a millisecond.

Do not upload `.env`, API keys, Telegram session files or database files.
Voice generation uses `edge-tts`; voice transcription uses Groq Whisper.
