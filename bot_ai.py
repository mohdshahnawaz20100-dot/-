"""Safe, owner-only CLONIX command-center utilities for the existing client."""
import os, sqlite3, shutil, time
from pathlib import Path
from datetime import datetime, timezone
from telethon import events

_STARTED = time.monotonic()
_DATA = Path(os.getenv("DATA_DIR", "./clonix_data")).resolve()
_DATA.mkdir(parents=True, exist_ok=True)
_DB = _DATA / "clonix_ai.sqlite3"

def _db():
    c = sqlite3.connect(_DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS missions (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, task TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open', created_at TEXT NOT NULL, completed_at TEXT)")
    c.commit()
    return c

def register_ai_commands(client, owner_id):
    owner_id = int(owner_id)
    def owner_only(fn):
        async def wrapped(event):
            if event.sender_id != owner_id:
                return
            try:
                await fn(event)
            except Exception as exc:
                await event.reply(f"❌ Error: {str(exc)[:160]}")
        return wrapped

    @client.on(events.NewMessage(pattern=r"^\.aihelp$"))
    @owner_only
    async def aihelp(e):
        await e.reply("⚡ **CLONIX COMMAND CENTER**\n\n`.selfcheck` — system check\n`.uptime` — running time\n`.resource` — disk/load snapshot\n`.mission <task>` — save a task\n`.missions` — pending tasks\n`.missiondone <id>` — complete task\n`.dailyreport` — task report\n`.autobackup` — back up task database\n\nOwner-only. No arbitrary code execution.")

    @client.on(events.NewMessage(pattern=r"^\.selfcheck$"))
    @owner_only
    async def selfcheck(e):
        with _db() as c: c.execute("SELECT 1").fetchone()
        await e.reply("⚙️ **SELF-CHECK**\n✅ Telegram client: " + ("connected" if client.is_connected() else "disconnected") + "\n✅ Mission database accessible\n📁 Data: `" + str(_DATA) + "`")

    @client.on(events.NewMessage(pattern=r"^\.uptime$"))
    @owner_only
    async def uptime(e):
        s=int(time.monotonic()-_STARTED); d,s=divmod(s,86400); h,s=divmod(s,3600); m,s=divmod(s,60)
        await e.reply(f"⏱️ **Command module uptime:** {d}d {h}h {m}m {s}s")

    @client.on(events.NewMessage(pattern=r"^\.resource$"))
    @owner_only
    async def resource(e):
        u=shutil.disk_usage(_DATA)
        try: load=", ".join(f"{x:.2f}" for x in os.getloadavg())
        except (AttributeError,OSError): load="not available"
        await e.reply(f"📊 **HOST SNAPSHOT**\n💾 Disk total: `{u.total/1024**3:.2f} GB`\n🟢 Disk free: `{u.free/1024**3:.2f} GB`\n⚙️ Load: `{load}`")

    @client.on(events.NewMessage(pattern=r"^\.mission(?:\s+(.+))?$"))
    @owner_only
    async def mission(e):
        task=(e.pattern_match.group(1) or "").strip()
        if not task: return await e.reply("Usage: `.mission <task>`")
        if len(task)>500: return await e.reply("❌ Task 500 characters se chhota rakho.")
        with _db() as c:
            cur=c.execute("INSERT INTO missions(owner_id,task,created_at) VALUES(?,?,?)",(owner_id,task,datetime.now(timezone.utc).isoformat()))
            mid=cur.lastrowid
        await e.reply(f"✅ Mission saved. ID: `{mid}`\n📝 {task}")

    @client.on(events.NewMessage(pattern=r"^\.missions$"))
    @owner_only
    async def missions(e):
        with _db() as c: rows=c.execute("SELECT id,task FROM missions WHERE owner_id=? AND status='open' ORDER BY id DESC LIMIT 20",(owner_id,)).fetchall()
        await e.reply("📌 **PENDING MISSIONS**\n"+("\n".join(f"`{i}` • {t[:300]}" for i,t in rows) if rows else "No pending missions."))

    @client.on(events.NewMessage(pattern=r"^\.missiondone\s+(\d+)$"))
    @owner_only
    async def missiondone(e):
        mid=int(e.pattern_match.group(1))
        with _db() as c: cur=c.execute("UPDATE missions SET status='done',completed_at=? WHERE id=? AND owner_id=? AND status='open'",(datetime.now(timezone.utc).isoformat(),mid,owner_id))
        await e.reply(f"✅ Mission `{mid}` completed." if cur.rowcount else "❌ Pending mission ID nahi mila.")

    @client.on(events.NewMessage(pattern=r"^\.dailyreport$"))
    @owner_only
    async def dailyreport(e):
        with _db() as c:
            op=c.execute("SELECT COUNT(*) FROM missions WHERE owner_id=? AND status='open'",(owner_id,)).fetchone()[0]
            done=c.execute("SELECT COUNT(*) FROM missions WHERE owner_id=? AND status='done'",(owner_id,)).fetchone()[0]
        await e.reply(f"📈 **DAILY REPORT**\n🕒 `{datetime.now().astimezone():%d %b %Y, %I:%M %p %Z}`\n📌 Pending: `{op}`\n✅ Completed: `{done}`")

    @client.on(events.NewMessage(pattern=r"^\.autobackup$"))
    @owner_only
    async def autobackup(e):
        folder=_DATA/"backups"; folder.mkdir(parents=True,exist_ok=True)
        target=folder/f"clonix_ai_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.sqlite3"
        try:
            with _db() as src, sqlite3.connect(target) as dst: src.backup(dst)
            await e.reply(f"✅ Mission DB backup created: `{target.name}`")
        except Exception as exc: await e.reply(f"❌ Backup failed: {str(exc)[:140]}")
    return True
