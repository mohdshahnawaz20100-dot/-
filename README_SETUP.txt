# JARVIS + I_amgenius

## Included files
- `bot.py` — main bot, with `bot_genius` registration added.
- `bot3.py` — existing JARVIS AI module (unchanged).
- `bot4.py` — existing JARVIS ULTRA module (unchanged).
- `bot_ai.py` — existing owner command-center module (unchanged).
- `bot5.py` — existing separate NEET/JEE quiz bot (unchanged).
- `bot_genius.py` — new owner-only I_amgenius module.

## Required environment
Use your existing environment variables:
`API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_ID`.
For AI features already present, configure `GROQ_API_KEY` as needed.
Do not share `.env`, bot tokens, API hashes, Telegram session files, or session strings.

## I_amgenius commands
- `I_amgenius help`
- `I_amgenius on|off|status`
- In a group: `I_amgenius exclude` / `I_amgenius allow`
- In a group: `I_amgenius member <numeric_user_id>`
- `I_amgenius giveaway add <title> | <url>`
- `I_amgenius giveaways`
- `I_amgenius votes <id> <current>/<target>`
- `I_amgenius won <id>`

## Important capability limits
- Abuse alerts are sent only for groups where the **bot account itself** is an admin and Telegram exposes the messages to it. The owner must start the bot in DM so it can send alerts.
- Text abuse detection is keyword-based and can miss context or slang. Telegram stickers do not expose their visual meaning as text; the module only flags a sticker if its alt text contains a configured abuse word.
- Member first/last activity is recorded from messages observed after this module is installed. It cannot reconstruct full historical first/last messages, exact join times, invite-request approvers, or private DMs between other users.
- Giveaway discovery is a notification to the owner when a visible chat message looks like a giveaway. Joining giveaways, submitting votes, vote collection, and ranking changes are not automated. The module will not create fake votes, manipulate polls, use other accounts, or spam groups.
- `exclude` records groups to exclude from any future supporter-request workflow; this package does not automatically message groups asking for votes.
- Group moderation commands from existing modules still depend on the relevant Telegram admin permissions.
