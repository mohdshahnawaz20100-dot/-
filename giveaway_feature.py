import asyncio
import json
import logging
import os
import time
from pathlib import Path
from telethon import Button, events

log = logging.getLogger("JARVIS.giveaway")
MAX_TEMPLATES = 30
MAX_LENGTH = 3500


def register_giveaway(client, owner_id):
    """Register a template-based sender. Every send requires owner confirmation."""
    owner_id = int(owner_id)
    data_dir = Path(os.getenv("DATA_DIR", "./clonex_data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    store = data_dir / f"giveaway_templates_{owner_id}.json"
    try:
        templates = json.loads(store.read_text(encoding="utf-8")) if store.exists() else []
        if not isinstance(templates, list):
            templates = []
    except Exception:
        templates = []
    selected = {"index": 0}
    pending = {}
    scheduled = {"task": None}

    def save():
        tmp = store.with_suffix(".tmp")
        tmp.write_text(json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(store)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.giveaway(?:\s+([\s\S]*))?$"))
    async def giveaway_command(event):
        if event.sender_id != owner_id:
            return
        arg = (event.pattern_match.group(1) or "").strip()
        parts = arg.split(maxsplit=1)
        action = parts[0].lower() if parts else "help"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if action == "add":
            if not rest:
                await event.reply("Usage: `.giveaway add <message>`")
                return
            if len(rest) > MAX_LENGTH:
                await event.reply(f"Message {MAX_LENGTH} characters se chhota rakho.")
                return
            if len(templates) >= MAX_TEMPLATES:
                await event.reply(f"Maximum {MAX_TEMPLATES} templates save ho sakte hain.")
                return
            templates.append(rest)
            selected["index"] = len(templates) - 1
            save()
            await event.reply(f"✅ Template #{len(templates)} saved.")
        elif action == "list":
            if not templates:
                await event.reply("Koi template nahi. `.giveaway add <message>`")
                return
            lines = ["📋 **Saved templates**"]
            for i, msg in enumerate(templates, 1):
                short = msg.replace("\n", " ")
                lines.append(f"{i}. {short[:65]}{'...' if len(short) > 65 else ''}"
                             f"{' ← selected' if i - 1 == selected['index'] else ''}")
            await event.reply("\n".join(lines), parse_mode="markdown")
        elif action == "use":
            try:
                n = int(rest) - 1
                if n < 0 or n >= len(templates):
                    raise ValueError
                selected["index"] = n
                await event.reply(f"✅ Template #{n + 1} selected.")
            except ValueError:
                await event.reply("Usage: `.giveaway use <number>`")
        elif action == "preview":
            if not templates:
                await event.reply("Pehle template save karo.")
            else:
                await event.reply("📝 **Preview**\n\n" + templates[selected["index"]])
        elif action in ("send", "schedule"):
            if not templates:
                await event.reply("Pehle `.giveaway add <message>` se template save karo.")
                return
            delay = None
            if action == "schedule":
                try:
                    delay = int(rest)
                    if not 5 <= delay <= 86400:
                        raise ValueError
                except ValueError:
                    await event.reply("Usage: `.giveaway schedule <seconds>` (5–86400)")
                    return
            token = f"{owner_id}:{event.chat_id}:{time.time_ns()}"
            pending[token] = (event.chat_id, templates[selected["index"]], delay, time.time())
            await event.reply(
                "📝 **Confirm message**\n\n" + templates[selected["index"]] +
                ("\n\n⏰ Confirm ke baad " + str(delay) + " seconds baad send hoga." if delay else
                 "\n\nConfirm karne par isi chat mein ek message bheja jayega."),
                buttons=[[Button.inline("✅ Confirm", data=("gw:y:" + token).encode())],
                         [Button.inline("✖️ Cancel", data=("gw:n:" + token).encode())]]
            )
        elif action == "cancel":
            task = scheduled["task"]
            if task and not task.done():
                task.cancel()
                scheduled["task"] = None
                await event.reply("🛑 Scheduled message cancelled.")
            else:
                await event.reply("Koi scheduled message pending nahi hai.")
        else:
            await event.reply(
                "🎁 **JARVIS Giveaway Helper**\n\n"
                "`.giveaway add <message>`\n`.giveaway list`\n"
                "`.giveaway use <number>`\n`.giveaway preview`\n"
                "`.giveaway send`\n`.giveaway schedule <seconds>`\n`.giveaway cancel`",
                parse_mode="markdown"
            )

    @client.on(events.CallbackQuery(pattern=rb"^gw:([yn]):(.+)$"))
    async def giveaway_confirm(event):
        if event.sender_id != owner_id:
            await event.answer("Not authorized", alert=True)
            return
        token = event.pattern_match.group(2).decode(errors="ignore")
        item = pending.pop(token, None)
        if item is None:
            await event.answer("Confirmation expired or already used.", alert=True)
            return
        chat_id, message, delay, created = item
        if time.time() - created > 600:
            await event.edit("⌛ Confirmation expired. Command dobara bhejo.")
            return
        if event.pattern_match.group(1) == b"n":
            await event.edit("✖️ Cancelled. Message nahi bheja gaya.")
            return
        if delay is None:
            try:
                await client.send_message(chat_id, message)
                await event.edit("✅ Message sent.")
            except Exception as exc:
                await event.edit(f"❌ Send failed: {str(exc)[:180]}")
            return

        old = scheduled["task"]
        if old and not old.done():
            old.cancel()

        async def deliver():
            try:
                await asyncio.sleep(delay)
                await client.send_message(chat_id, message)
                log.info("Confirmed scheduled giveaway message sent to %s", chat_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduled giveaway message failed")
            finally:
                scheduled["task"] = None

        scheduled["task"] = asyncio.create_task(deliver())
        await event.edit(f"⏰ Confirmed. {delay} seconds baad isi chat mein bheja jayega.")

    return giveaway_command
