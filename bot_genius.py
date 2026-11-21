"""
I_amgenius — owner-only, privacy-conscious JARVIS add-on.

Features:
- Owner-only command namespace.
- Per-group exclude/allow controls for any future giveaway outreach.
- Giveaway discovery alerts from chats where this Telegram bot can see messages.
- Manual giveaway/vote tracking and supporter-message drafts.
- Group member first/last-seen tracking from messages observed after installation.
- Abuse-word alerts in groups only when THIS bot is an admin.
- No fake voting, vote manipulation, unsolicited mass-DMs, or access bypass.
"""
from __future__ import annotations

import os
import re
import sqlite3
import time
import logging
from pathlib import Path
from datetime import datetime, timezone

from telethon import events

log = logging.getLogger("jarvis.igenius")
DATA_DIR = Path(os.getenv("DATA_DIR", "./clonex_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "i_amgenius.sqlite3"

# Keep this list intentionally small and configurable. Add local variants through
# I_AMGENIUS_ABUSE_WORDS (comma-separated) in the hosting environment.
_DEFAULT_ABUSE_WORDS = {
    "fuck", "fucking", "motherfucker", "bitch", "bastard", "asshole",
    "shit", "bullshit", "idiot", "gaand", "gandu", "chutiya", "madarchod",
    "behenchod", "bhosdi", "randi", "harami"
}
_ABUSE_WORDS = _DEFAULT_ABUSE_WORDS | {
    x.strip().lower() for x in os.getenv("I_AMGENIUS_ABUSE_WORDS", "").split(",") if x.strip()
}
_GIVEAWAY_RE = re.compile(r"\b(give[\s-]?away|contest|win\s+(?:a|the)|winner|voting\s+contest)\b", re.I)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _connect():
    c = sqlite3.connect(DB_PATH, timeout=20)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        owner_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS excluded_chats(
        chat_id INTEGER PRIMARY KEY, title TEXT, added_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS member_activity(
        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT,
        display_name TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
        first_message TEXT, last_message TEXT, message_count INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(chat_id,user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS giveaways(
        id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL,
        title TEXT NOT NULL, url TEXT, current_votes INTEGER NOT NULL DEFAULT 0,
        target_votes INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'tracking',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS seen_alerts(
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
        kind TEXT NOT NULL, PRIMARY KEY(chat_id,message_id,kind))""")
    c.commit()
    return c


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_excluded(chat_id):
    with _connect() as c:
        return c.execute("SELECT 1 FROM excluded_chats WHERE chat_id=?", (int(chat_id),)).fetchone() is not None


def _set_excluded(chat_id, title, excluded):
    with _connect() as c:
        if excluded:
            c.execute("INSERT OR REPLACE INTO excluded_chats(chat_id,title,added_at) VALUES(?,?,?)",
                      (int(chat_id), str(title or chat_id)[:200], _now()))
        else:
            c.execute("DELETE FROM excluded_chats WHERE chat_id=?", (int(chat_id),))


def _enabled(owner_id):
    with _connect() as c:
        row = c.execute("SELECT enabled FROM settings WHERE owner_id=?", (int(owner_id),)).fetchone()
        if row is None:
            c.execute("INSERT OR IGNORE INTO settings(owner_id,enabled) VALUES(?,1)", (int(owner_id),))
            c.commit()
            return True
        return bool(row[0])


def _set_enabled(owner_id, value):
    with _connect() as c:
        c.execute("INSERT INTO settings(owner_id,enabled) VALUES(?,?) "
                  "ON CONFLICT(owner_id) DO UPDATE SET enabled=excluded.enabled",
                  (int(owner_id), int(bool(value))))


def _clean(s, limit=500):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


def _abusive(text):
    words = {w.lower() for w in _WORD_RE.findall(text or "")}
    return sorted(words & _ABUSE_WORDS)


def _record_message(chat_id, sender, text):
    uid = getattr(sender, "id", None)
    if not uid:
        return
    username = getattr(sender, "username", None)
    name = " ".join(x for x in (getattr(sender, "first_name", None),
                                 getattr(sender, "last_name", None)) if x).strip()
    now = _now()
    with _connect() as c:
        c.execute("""INSERT INTO member_activity(
            chat_id,user_id,username,display_name,first_seen,last_seen,
            first_message,last_message,message_count)
            VALUES(?,?,?,?,?,?,?,?,1)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET
              username=excluded.username, display_name=excluded.display_name,
              last_seen=excluded.last_seen, last_message=excluded.last_message,
              message_count=member_activity.message_count+1""",
            (int(chat_id), int(uid), username, name or str(uid), now, now,
             _clean(text, 300), _clean(text, 300)))


def _claim_alert(chat_id, message_id, kind):
    with _connect() as c:
        cur = c.execute("INSERT OR IGNORE INTO seen_alerts(chat_id,message_id,kind) VALUES(?,?,?)",
                        (int(chat_id), int(message_id), kind))
        return cur.rowcount == 1


def _member_row(chat_id, user_id):
    with _connect() as c:
        return c.execute("""SELECT username,display_name,first_seen,last_seen,
                            first_message,last_message,message_count
                            FROM member_activity WHERE chat_id=? AND user_id=?""",
                         (int(chat_id), int(user_id))).fetchone()


def _giveaway_add(owner_id, title, url):
    now = _now()
    with _connect() as c:
        cur = c.execute("""INSERT INTO giveaways(owner_id,title,url,created_at,updated_at)
                           VALUES(?,?,?,?,?)""",
                        (int(owner_id), title[:180], url[:500], now, now))
        return cur.lastrowid


def _giveaways(owner_id):
    with _connect() as c:
        return c.execute("""SELECT id,title,url,current_votes,target_votes,status
                            FROM giveaways WHERE owner_id=? ORDER BY id DESC LIMIT 30""",
                         (int(owner_id),)).fetchall()


def _vote_update(owner_id, gid, current, target):
    with _connect() as c:
        cur = c.execute("""UPDATE giveaways SET current_votes=?,target_votes=?,updated_at=?
                           WHERE id=? AND owner_id=? AND status='tracking'""",
                        (int(current), int(target), _now(), int(gid), int(owner_id)))
        return cur.rowcount == 1


def _giveaway_done(owner_id, gid):
    with _connect() as c:
        cur = c.execute("UPDATE giveaways SET status='closed',updated_at=? WHERE id=? AND owner_id=?",
                        (_now(), int(gid), int(owner_id)))
        return cur.rowcount == 1


def register_genius(client, owner_id):
    """Register I_amgenius commands and passive alerts on the supplied bot client."""
    owner_id = int(owner_id or 0)
    if not owner_id:
        log.warning("I_amgenius disabled: OWNER_ID is not configured")
        return False

    async def owner_reply(event, text):
        # All command output is private to the configured owner or returned in the
        # current chat only after sender verification.
        await event.reply(str(text)[:3900], link_preview=False)

    @client.on(events.NewMessage(incoming=True, pattern=r"(?i)^/?I_amgenius(?:\s+(.*))?$"))
    async def genius_commands(event):
        if event.sender_id != owner_id:
            return
        raw = (event.pattern_match.group(1) or "").strip()
        parts = raw.split(maxsplit=2)
        action = (parts[0].lower() if parts else "help")

        if action in ("help", "?"):
            return await owner_reply(event,
                "🧠 **I_amgenius — Owner Commands**\n\n"
                "`I_amgenius on|off|status` — alert/monitor controls\n"
                "`I_amgenius exclude` — is GC ko exclude karo\n"
                "`I_amgenius allow` — GC ko dobara allow karo\n"
                "`I_amgenius excluded` — excluded GC list\n"
                "`I_amgenius member <user_id>` — current GC mein observed activity\n"
                "`I_amgenius giveaway add <title> | <url>` — tracker add\n"
                "`I_amgenius giveaways` — tracked giveaways\n"
                "`I_amgenius votes <id> <current>/<target>` — vote progress update\n"
                "`I_amgenius won <id>` — result mark karo\n\n"
                "Giveaway participation/votes ko manipulate nahi karta. Vote progress manually track hota hai; genuine supporter request ka draft banaya ja sakta hai.")
        if action == "on":
            _set_enabled(owner_id, True)
            return await owner_reply(event, "✅ I_amgenius monitoring ON.")
        if action == "off":
            _set_enabled(owner_id, False)
            return await owner_reply(event, "🔕 I_amgenius monitoring OFF.")
        if action == "status":
            return await owner_reply(event,
                f"🧠 I_amgenius: {'ON' if _enabled(owner_id) else 'OFF'}\n"
                f"Current chat excluded: {'YES' if event.is_group and _is_excluded(event.chat_id) else 'NO'}\n"
                "Abuse alerts: sirf un groups mein jahan ye bot admin ho aur messages visible hon.")
        if action in ("exclude", "allow"):
            if not event.is_group:
                return await owner_reply(event, "Ye command us GC mein bhejo jise exclude/allow karna hai.")
            chat = await event.get_chat()
            title = getattr(chat, "title", None) or str(event.chat_id)
            excluded = action == "exclude"
            _set_excluded(event.chat_id, title, excluded)
            return await owner_reply(event,
                ("🚫 Is GC ko future giveaway supporter-request workflow se exclude kar diya." if excluded
                 else "✅ Is GC ko exclusion list se hata diya."))
        if action == "excluded":
            with _connect() as c:
                rows = c.execute("SELECT chat_id,title FROM excluded_chats ORDER BY title").fetchall()
            return await owner_reply(event, "🚫 **Excluded chats**\n" +
                ("\n".join(f"• {title} (`{cid}`)" for cid, title in rows[:50]) if rows else "Koi excluded GC nahi."))
        if action == "member":
            if not event.is_group:
                return await owner_reply(event, "Member activity command ko usi GC mein use karo.")
            if len(parts) < 2 or not parts[1].isdigit():
                return await owner_reply(event, "Usage: `I_amgenius member <numeric_user_id>`")
            row = _member_row(event.chat_id, int(parts[1]))
            if not row:
                return await owner_reply(event, "Is member ka message record install ke baad observe nahi hua.")
            username, name, first, last, first_msg, last_msg, count = row
            return await owner_reply(event,
                f"👤 **Observed member activity**\nName: {name}\nUsername: @{username or 'N/A'}\n"
                f"User ID: `{parts[1]}`\nFirst observed: {first}\nLast observed: {last}\n"
                f"Messages observed: {count}\nFirst observed message: {first_msg or 'N/A'}\n"
                f"Last observed message: {last_msg or 'N/A'}\n\n"
                "Note: ye bot ke install/visibility ke baad ka record hai, complete historical join/DM data nahi.")
        if action == "giveaway":
            if len(parts) < 2 or parts[1].lower() != "add" or len(parts) < 3:
                return await owner_reply(event, "Usage: `I_amgenius giveaway add Title | https://link`")
            bits = parts[2].split("|", 1)
            title = bits[0].strip()
            url = bits[1].strip() if len(bits) > 1 else ""
            if not title:
                return await owner_reply(event, "Giveaway title missing hai.")
            gid = _giveaway_add(owner_id, title, url)
            return await owner_reply(event, f"✅ Giveaway tracker #{gid} save ho gaya.")
        if action == "giveaways":
            rows = _giveaways(owner_id)
            if not rows:
                return await owner_reply(event, "Abhi koi giveaway tracked nahi hai.")
            lines = ["🏆 **Tracked Giveaways**"]
            for gid, title, url, current, target, status in rows:
                lines.append(f"#{gid} {title}\nVotes: {current}/{target or 'not set'} | {status}\n{url}")
            return await owner_reply(event, "\n\n".join(lines))
        if action == "votes":
            if len(parts) < 3:
                return await owner_reply(event, "Usage: `I_amgenius votes <id> <current>/<target>`")
            m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", raw[len(parts[0]):].strip())
            if not m:
                return await owner_reply(event, "Format: `I_amgenius votes 1 25/100`")
            gid, current, target = map(int, m.groups())
            if not _vote_update(owner_id, gid, current, target):
                return await owner_reply(event, "Tracked giveaway ID nahi mila ya close ho chuka hai.")
            remaining = max(0, target-current)
            return await owner_reply(event, f"📊 Giveaway #{gid}: {current}/{target}\nRemaining: {remaining}")
        if action == "won":
            if len(parts) < 2 or not parts[1].isdigit():
                return await owner_reply(event, "Usage: `I_amgenius won <giveaway_id>`")
            ok = _giveaway_done(owner_id, int(parts[1]))
            return await owner_reply(event, "🏆 Result marked closed." if ok else "Giveaway ID nahi mila.")
        return await owner_reply(event, "Unknown command. `I_amgenius help` bhejo.")

    @client.on(events.NewMessage(incoming=True))
    async def genius_group_monitor(event):
        if not _enabled(owner_id):
            return
        if not (event.is_group or event.is_channel):
            return
        if not event.sender_id or event.sender_id == owner_id:
            return
        text = (event.raw_text or "").strip()
        sender = await event.get_sender()
        if event.is_group:
            try:
                _record_message(event.chat_id, sender, text)
            except Exception:
                log.exception("I_amgenius activity log failed")

        # Giveaway discovery is notification-only; no automatic joining or voting.
        if text and _GIVEAWAY_RE.search(text) and _claim_alert(event.chat_id, event.message.id, "giveaway"):
            try:
                chat = await event.get_chat()
                title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(event.chat_id)
                who = getattr(sender, "username", None) or getattr(sender, "first_name", None) or str(event.sender_id)
                await client.send_message(owner_id,
                    f"🎁 **Possible giveaway spotted**\nChat: {title}\nFrom: {who}\n"
                    f"Message: {text[:900]}\n\nCheck the rules and eligibility manually.")
            except Exception as exc:
                log.info("Giveaway notification unavailable: %s", type(exc).__name__)

        if not event.is_group:
            return
        # Only alert in groups where this bot account is actually an admin.
        try:
            perms = await client.get_permissions(event.chat_id, "me")
            if not bool(getattr(perms, "is_admin", False) or getattr(perms, "is_creator", False)):
                return
        except Exception:
            return

        bad_words = _abusive(text)
        kind = "abuse" if bad_words else None
        if not kind and getattr(event.message, "sticker", None):
            # Telegram doesn't provide semantic sticker meaning. Alert only when
            # sticker alt text itself contains a configured abuse term.
            alt = ""
            for attr in getattr(getattr(event.message, "document", None), "attributes", []) or []:
                alt += " " + str(getattr(attr, "alt", "") or "")
            bad_words = _abusive(alt)
            if bad_words:
                kind = "abusive_sticker_alt"
        if not kind or not _claim_alert(event.chat_id, event.message.id, kind):
            return
        try:
            chat = await event.get_chat()
            chat_title = getattr(chat, "title", None) or str(event.chat_id)
            sender_name = " ".join(x for x in (getattr(sender, "first_name", None),
                                               getattr(sender, "last_name", None)) if x)
            sender_name = sender_name or getattr(sender, "username", None) or str(event.sender_id)
            await client.send_message(owner_id,
                f"⚠️ **JARVIS ABUSE ALERT**\nGroup: {chat_title}\n"
                f"Member: {sender_name} (@{getattr(sender, 'username', None) or 'N/A'})\n"
                f"Type: {'abusive sticker alt-text' if kind.endswith('sticker_alt') else 'abusive text'}\n"
                f"Matched terms: {', '.join(bad_words)}\n"
                f"Message ID: `{event.message.id}`\n"
                f"Text: {text[:700] or '[sticker/media]'}")
        except Exception as exc:
            log.info("Abuse alert could not be delivered: %s", type(exc).__name__)

    log.info("I_amgenius registered for owner %s", owner_id)
    return True
