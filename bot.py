#!/usr/bin/env python3
# 𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ - Multi-User Userbot

import os, sys, json, asyncio, random, re, time, logging, shutil, sqlite3, uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote as url_quote

from aiohttp import web
import aiohttp


from telethon import TelegramClient, events, functions, Button
from telethon.tl.types import (
    ChatBannedRights, User, ChannelParticipantsAdmins, ChatAdminRights,
    MessageMediaWebPage, MessageMediaUnsupported
)
from telethon.errors import (
    FloodWaitError, SessionPasswordNeededError, PhoneCodeInvalidError,
    PhoneCodeExpiredError, PasswordHashInvalidError
)
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest, DeletePhotosRequest
from telethon.tl.functions.contacts import BlockRequest, UnblockRequest
from telethon.tl.functions.messages import EditChatDefaultBannedRightsRequest
from telethon.tl.functions.channels import EditBannedRequest, GetParticipantRequest, EditAdminRequest, EditTitleRequest
from telethon.tl.functions.messages import EditChatTitleRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import ReactionEmoji
from telethon.sessions import StringSession

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

# ═══════════════ CONFIG ═══════════════
API_ID       = int(os.getenv("API_ID", "39662504"))
API_HASH     = os.getenv("API_HASH", "")
BOT_TOKEN    = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
OWNER_ID     = int(os.getenv("OWNER_ID", "0"))

WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
NEWS_API_KEY    = os.getenv("NEWS_API_KEY", "")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY", "")

DATA_DIR = Path(os.getenv("DATA_DIR", "./clonex_data"))
DATA_DIR.mkdir(exist_ok=True, parents=True)
TEMP_DIR = DATA_DIR / "temp"
TEMP_DIR.mkdir(exist_ok=True, parents=True)

GHOST_IMG_LOCAL = DATA_DIR / "ghost_cached.jpg"
GHOST_IMG_URL = "https://i.ibb.co/MDZVcWjh/IMG-20260921-203641-296.jpg"

CLEANUP_DAYS = 45
IST_TZ = ZoneInfo("Asia/Kolkata")

# ═══════════════ SMART TIMING ═══════════════
# 🎯 PERFECT OFFSET: 10 Millisecond (Singapore server ke liye perfect)
PING_OFFSET = 0.0

logging.basicConfig(format='%(asctime)s [%(levelname)s] %(message)s',
                    level=logging.INFO, datefmt='%H:%M:%S')
log = logging.getLogger("CN_USERBOT")


# ═══════════════ SQLITE DATABASE (MongoDB replacement) ═══════════════
DB_PATH = DATA_DIR / "clonex.sqlite3"

def _sqlite_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bot_documents (
            collection TEXT NOT NULL,
            doc_id TEXT NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY (collection, doc_id)
        )
    """)
    return conn

@contextmanager
def _db_session():
    conn = _sqlite_connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _get_path(data, dotted_key):
    value = data
    for part in dotted_key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value

def _set_path(data, dotted_key, value):
    parts = dotted_key.split(".")
    target = data
    for part in parts[:-1]:
        if not isinstance(target.get(part), dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value

def _matches_document(doc, query):
    for key, expected in (query or {}).items():
        actual = _get_path(doc, key)
        if isinstance(expected, dict):
            for op, value in expected.items():
                if op == "$gt":
                    if actual is None or not actual > value:
                        return False
                elif op == "$lt":
                    if actual is None or not actual < value:
                        return False
                elif op == "$regex":
                    if actual is None or re.search(value, str(actual)) is None:
                        return False
                else:
                    return False
        elif actual != expected:
            return False
    return True

class _SQLiteCursor:
    def __init__(self, documents):
        self.documents = documents

    def sort(self, key, direction=1):
        self.documents.sort(
            key=lambda item: (_get_path(item, key) is None, _get_path(item, key)),
            reverse=(direction < 0)
        )
        return self

    async def to_list(self, length=None):
        return self.documents[:length] if length is not None else list(self.documents)

    def __aiter__(self):
        async def iterator():
            for document in self.documents:
                yield dict(document)
        return iterator()

class SQLiteCollection:
    def __init__(self, name):
        self.name = name

    def _read_all(self):
        with _db_session() as conn:
            rows = conn.execute(
                "SELECT payload FROM bot_documents WHERE collection = ?",
                (self.name,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def find(self, query=None):
        return _SQLiteCursor([
            doc for doc in self._read_all()
            if _matches_document(doc, query or {})
        ])

    async def find_one(self, query=None):
        for doc in self._read_all():
            if _matches_document(doc, query or {}):
                return doc
        return None

    async def insert_one(self, document):
        doc = dict(document)
        if "_id" not in doc:
            doc["_id"] = uuid.uuid4().hex
        with _db_session() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_documents(collection, doc_id, payload) VALUES (?, ?, ?)",
                (self.name, str(doc["_id"]), json.dumps(doc, ensure_ascii=False, default=str))
            )
        return type("InsertResult", (), {"inserted_id": doc["_id"]})()

    async def update_one(self, query, update, upsert=False):
        docs = self._read_all()
        found = next((d for d in docs if _matches_document(d, query or {})), None)
        if found is None:
            if not upsert:
                return type("UpdateResult", (), {"matched_count": 0, "modified_count": 0})()
            found = {k: v for k, v in (query or {}).items() if not isinstance(v, dict)}
        original = json.dumps(found, sort_keys=True, default=str)
        for key, value in update.get("$set", {}).items():
            _set_path(found, key, value)
        for key, value in update.get("$inc", {}).items():
            current = _get_path(found, key) or 0
            _set_path(found, key, current + value)
        if "_id" not in found:
            found["_id"] = uuid.uuid4().hex
        with _db_session() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bot_documents(collection, doc_id, payload) VALUES (?, ?, ?)",
                (self.name, str(found["_id"]), json.dumps(found, ensure_ascii=False, default=str))
            )
        changed = int(original != json.dumps(found, sort_keys=True, default=str))
        return type("UpdateResult", (), {"matched_count": 1, "modified_count": changed})()

    async def delete_one(self, query):
        docs = self._read_all()
        found = next((d for d in docs if _matches_document(d, query or {})), None)
        if found is None:
            return type("DeleteResult", (), {"deleted_count": 0})()
        with _db_session() as conn:
            cur = conn.execute(
                "DELETE FROM bot_documents WHERE collection = ? AND doc_id = ?",
                (self.name, str(found.get("_id", "")))
            )
        return type("DeleteResult", (), {"deleted_count": cur.rowcount})()

    async def delete_many(self, query):
        docs = [d for d in self._read_all() if _matches_document(d, query or {})]
        with _db_session() as conn:
            for doc in docs:
                conn.execute(
                    "DELETE FROM bot_documents WHERE collection = ? AND doc_id = ?",
                    (self.name, str(doc.get("_id", "")))
                )
        return type("DeleteResult", (), {"deleted_count": len(docs)})()

users_col = SQLiteCollection("logged_users")
all_users_col = SQLiteCollection("all_users")
user_data_col = SQLiteCollection("user_data")
blocked_col = SQLiteCollection("blocked_users")
cmd_usage_col = SQLiteCollection("command_usage")
cmd_blocked_col = SQLiteCollection("command_blocked")
sched_col = SQLiteCollection("scheduled_messages")

async def init_database():
    try:
        with _db_session() as conn:
            conn.execute("SELECT 1")
        log.info(f"✅ SQLite connected: {DB_PATH}")
        return True
    except Exception as e:
        log.error(f"❌ SQLite database: {e}")
        return False

users_db = {}
all_users_db = {}
user_data_cache = {}
blocked_users = set()
command_usage_db = {}
command_blocked_db = {}

async def load_all():
    global users_db, all_users_db, user_data_cache, blocked_users, command_usage_db, command_blocked_db
    if users_col is None:
        return
    try:
        users_db = {}
        async for d in users_col.find({}):
            uid = d.get("_id"); d.pop("_id", None); users_db[str(uid)] = d
        all_users_db = {}
        async for d in all_users_col.find({}):
            uid = d.get("_id"); d.pop("_id", None); all_users_db[str(uid)] = d
        user_data_cache = {}
        async for d in user_data_col.find({}):
            uid = d.get("_id"); d.pop("_id", None); user_data_cache[str(uid)] = d
        blocked_users = set()
        if blocked_col is not None:
            async for d in blocked_col.find({}):
                blocked_users.add(str(d.get("_id")))
        command_usage_db = {}
        if cmd_usage_col is not None:
            async for d in cmd_usage_col.find({}):
                uid = str(d.get("_id"))
                d.pop("_id", None)
                if "commands" in d:
                    command_usage_db[uid] = {"commands": d.get("commands", {}), "last_update": d.get("last_update", time.time())}
                else:
                    last_update = d.pop("last_update", time.time())
                    command_usage_db[uid] = {"commands": d, "last_update": last_update}
        command_blocked_db = {}
        if cmd_blocked_col is not None:
            async for d in cmd_blocked_col.find({}):
                uid = str(d.get("_id"))
                command_blocked_db[uid] = d.get("commands", [])
        log.info(f"✅ Loaded {len(users_db)} logged, {len(all_users_db)} all, {len(user_data_cache)} user_data, {len(blocked_users)} blocked")
    except Exception as e:
        log.error(f"load_all: {e}")

async def save_user(uid, data):
    if users_col is None: return
    try:
        d = dict(data); d["_id"] = str(uid)
        await users_col.update_one({"_id": str(uid)}, {"$set": d}, upsert=True)
    except Exception as e:
        log.error(f"save_user: {e}")

async def save_all_user(uid, data):
    if all_users_col is None: return
    try:
        d = dict(data); d["_id"] = str(uid)
        await all_users_col.update_one({"_id": str(uid)}, {"$set": d}, upsert=True)
    except Exception as e:
        log.error(f"save_all_user: {e}")

async def save_user_data(uid, data):
    if user_data_col is None: return
    try:
        d = dict(data); d["_id"] = str(uid)
        await user_data_col.update_one({"_id": str(uid)}, {"$set": d}, upsert=True)
    except Exception as e:
        log.error(f"save_user_data: {e}")

async def block_user_db(uid):
    uid = str(uid)
    blocked_users.add(uid)
    if blocked_col is not None:
        try:
            await blocked_col.update_one({"_id": uid}, {"$set": {"blocked_at": datetime.now().strftime("%d %b %Y, %I:%M %p")}}, upsert=True)
        except Exception as e:
            log.error(f"block_user_db: {e}")

async def unblock_user_db(uid):
    uid = str(uid)
    blocked_users.discard(uid)
    if blocked_col is not None:
        try:
            await blocked_col.delete_one({"_id": uid})
        except Exception as e:
            log.error(f"unblock_user_db: {e}")

async def log_command_usage(uid, command):
    uid = str(uid)
    try:
        if uid not in command_usage_db:
            command_usage_db[uid] = {"commands": {}, "last_update": time.time()}
        if "commands" not in command_usage_db[uid] or not isinstance(command_usage_db[uid].get("commands"), dict):
            command_usage_db[uid] = {"commands": command_usage_db[uid] if isinstance(command_usage_db[uid], dict) else {}, "last_update": time.time()}
        command_usage_db[uid]["commands"][command] = command_usage_db[uid]["commands"].get(command, 0) + 1
        command_usage_db[uid]["last_update"] = time.time()
        if cmd_usage_col is not None:
            await cmd_usage_col.update_one({"_id": uid}, {"$inc": {f"commands.{command}": 1}, "$set": {"last_update": time.time()}}, upsert=True)
    except Exception as e:
        log.error(f"log_command_usage: {e}")

async def cleanup_old_command_logs():
    global command_usage_db
    if cmd_usage_col is None: return
    try:
        cutoff_time = time.time() - (CLEANUP_DAYS * 24 * 60 * 60)
        result = await cmd_usage_col.delete_many({"last_update": {"$lt": cutoff_time}})
        if result.deleted_count > 0:
            log.info(f"🧹 Cleaned {result.deleted_count} old command logs")
            to_remove = []
            for uid, data in command_usage_db.items():
                last_update = data.get("last_update", 0) if isinstance(data, dict) else 0
                if not last_update or last_update < cutoff_time:
                    to_remove.append(uid)
            for uid in to_remove:
                command_usage_db.pop(uid, None)
    except Exception as e:
        log.error(f"cleanup_old_command_logs: {e}")

async def auto_cleanup_loop():
    while True:
        try:
            await cleanup_old_command_logs()
        except Exception as e:
            log.error(f"auto_cleanup_loop: {e}")
        await asyncio.sleep(24 * 60 * 60)

async def cleanup_temp_loop():
    while True:
        try:
            now = time.time()
            for f in TEMP_DIR.glob("*"):
                if f.is_file() and now - f.stat().st_mtime > 3600:
                    try: f.unlink()
                    except: pass
        except Exception as e:
            log.error(f"Temp cleanup: {e}")
        await asyncio.sleep(3600)

def get_user(uid):
    uid = str(uid)
    if uid not in users_db:
        users_db[uid] = {"logged_in": False, "phone": None, "tg_id": None, "tg_name": None, "tg_username": None, "session_string": None, "step": None, "phone_code_hash": None, "login_time": None}
    return users_db[uid]

def get_user_data(uid):
    uid = str(uid)
    if uid not in user_data_cache:
        user_data_cache[uid] = {}
    return user_data_cache[uid]

async def track_start(uid, name, username):
    uid = str(uid)
    if uid not in all_users_db:
        all_users_db[uid] = {"name": name, "username": username, "tg_id": uid, "first_seen": datetime.now().strftime("%d %b %Y, %I:%M %p")}
        await save_all_user(uid, all_users_db[uid])


def is_bot_approved(uid):
    """Only the configured owner and explicitly approved Telegram users may use the bot."""
    uid = str(uid)
    return bool(OWNER_ID and uid == str(OWNER_ID)) or bool(all_users_db.get(uid, {}).get("approved", False))

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    log.error("❌ BOT_TOKEN missing"); exit(1)

SESSION_DIR = Path("/tmp/clonex_session")
SESSION_DIR.mkdir(exist_ok=True, parents=True)
bot = TelegramClient(
    str(SESSION_DIR / "clonex_bot"), API_ID, API_HASH,
    device_model="𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡",
    system_version="SHAHNAWAZ",
    app_version="1.0"
).start(bot_token=BOT_TOKEN)
from bot3 import register_jarvis
register_jarvis(bot, OWNER_ID, auto_reply=True)
from bot_ai import register_ai_commands
register_ai_commands(bot, OWNER_ID)
from bot4 import register_bot4
register_bot4(bot, OWNER_ID)
pending_clients = {}
user_bots = {}
broadcast_history = []
raid_state = {
    "reply": {},
    "rr": {},
    "flag": {},
    "hrr": {},
}

async def health_server():
    async def handle(request):
        return web.Response(text="✅ 𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ Userbot Running")

    async def watch_page(request):
        html_page = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>JARVIS Precision Watch</title><style>
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#090d18;color:#eaf1ff;font-family:system-ui,sans-serif}
main{width:min(94vw,650px);padding:32px 18px;text-align:center;border:1px solid #263653;border-radius:24px;background:linear-gradient(145deg,#121c31,#0b1220);box-shadow:0 20px 70px #0008}
small{letter-spacing:.18em;color:#8ca7d4}.clock{font-variant-numeric:tabular-nums;font-size:clamp(2.5rem,10vw,5.4rem);font-weight:750;letter-spacing:.02em;margin:24px 0 8px}.ms{color:#73d6ff}.date{color:#a8b7d1;margin:10px 0 24px}button{border:1px solid #46638d;border-radius:999px;padding:10px 22px;background:#1b2d49;color:white;font-size:1rem}#state{color:#8ca7d4;margin-top:18px;font-size:.85rem}
</style></head><body><main><small>JARVIS · PRECISION WATCH</small><div class="clock"><span id="time">--:--:--</span><span class="ms" id="ms">.---</span></div><div class="date" id="date">Loading IST…</div><button id="toggle">Pause</button><div id="state">Live · Device clock · Asia/Kolkata</div></main>
<script>
const timeEl=document.getElementById('time'),msEl=document.getElementById('ms'),dateEl=document.getElementById('date'),btn=document.getElementById('toggle'),stateEl=document.getElementById('state');let running=true,raf=0;
const tf=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:true});
const df=new Intl.DateTimeFormat('en-IN',{timeZone:'Asia/Kolkata',weekday:'long',day:'2-digit',month:'long',year:'numeric'});
function tick(){if(!running)return;const now=Date.now();timeEl.textContent=tf.format(now);msEl.textContent='.'+String(now%1000).padStart(3,'0');dateEl.textContent=df.format(now)+' · IST';raf=requestAnimationFrame(tick)}
btn.addEventListener('click',()=>{running=!running;btn.textContent=running?'Pause':'Resume';stateEl.textContent=running?'Live · Device clock · Asia/Kolkata':'Paused';if(running)tick();else cancelAnimationFrame(raf)});tick();
</script></body></html>'''
        return web.Response(text=html_page, content_type="text/html")

    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_get('/health', handle)
    app.router.add_get('/watch', watch_page)
    port = int(os.getenv('PORT', 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    log.info(f"✅ Health on port {port}")
    try:
        # Keep the health endpoint alive while the bot is running.
        await asyncio.Event().wait()
    finally:
        # Close aiohttp sockets cleanly when Render stops/restarts the service.
        await runner.cleanup()

async def get_ghost_image_path():
    if GHOST_IMG_LOCAL.exists() and GHOST_IMG_LOCAL.stat().st_size > 1000:
        return str(GHOST_IMG_LOCAL)
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(GHOST_IMG_URL) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 1000:
                        with open(GHOST_IMG_LOCAL, "wb") as f:
                            f.write(data)
                        return str(GHOST_IMG_LOCAL)
    except Exception as e:
        log.warning(f"Ghost image download failed: {e}")
    return None

# ═══════════════ BOT COMMANDS ═══════════════
@bot.on(events.NewMessage(pattern='/start'))
async def start_h(event):
    uid = event.sender_id
    if str(uid) in blocked_users:
        return
    u = get_user(uid)
    try:
        s = await event.get_sender()
        name = ((s.first_name or "") + (" " + s.last_name if getattr(s, 'last_name', None) else "")).strip() or "Unknown"
        await track_start(uid, name, s.username or "None")
    except Exception:
        pass
    if not is_bot_approved(uid):
        await event.reply(
            "🔒 **Approval Required**\n\n"
            "Aapki request owner ko bhej di gayi hai. Owner approve karega tabhi bot use kar paoge.\n"
            "🆔 Your ID: `{}".format(uid) + "`",
            parse_mode='markdown'
        )
        try:
            await bot.send_message(OWNER_ID, f"🆕 **Approval Request**\n👤 {name if 'name' in locals() else 'New user'}\n🆔 `{uid}`\nApprove: `/approve {uid}`")
        except Exception:
            pass
        return
    START_MEDIA = "https://files.catbox.moe/35bl05.mp4"
    if u["logged_in"]:
        caption = ("✅ **ᴀʟʀᴇᴀᴅʏ ʟᴏɢɢᴇᴅ ɪɴ!**\n"
                   "━━━━━━━━━━━━━━━━━━━━━\n\n"
                   "**🔹 ᴀᴠᴀɪʟᴀʙʟᴇ ᴄᴏᴍᴍᴀɴᴅs**\n"
                   "• `/help` — ʜᴇʟᴘ ᴍᴇɴᴜ\n"
                   "• `/status` — ʟᴏɢɪɴ sᴛᴀᴛᴜs\n"
                   "• `/logout` — ʟᴏɢᴏᴜᴛ\n\n"
                   "**💡 ɴᴏᴛᴇ:**\n"
                   "Apna userbot use karne ke liye kisi bhi chat me `.help` bhejo.\n\n"
                   "━━━━━━━━━━━━━━━━━━━━━\n"
                   "✨ **ᴘᴏᴡᴇʀᴇᴅ ʙʏ @shahnawazzz_1** 😎 ✨")
    else:
        caption = ("🎉 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ 𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ 🎉\n"
                   "━━━━━━━━━━━━━━━━━━━━━\n\n"
                   "🛑 ʟᴏɢɪɴ sᴛᴇᴘs:\n\n"
                   "🔷  sᴇɴᴅ /ʟᴏɢɪɴ\n\n"
                   "🔷  sᴇɴᴅ ᴘʜᴏɴᴇ ɴᴜᴍʙᴇʀ (ᴡɪᴛʜ ᴄᴏᴜɴᴛʀʏ ᴄᴏᴅᴇ, ᴇ.ɢ. +𝟿𝟷𝟿𝟾𝟽𝟼𝟻𝟺𝟹𝟸𝟷𝟶)\n\n"
                   "🔷  sᴇɴᴅ ᴏᴛᴘ (sᴘᴀᴄᴇ ᴋᴇ sᴀᴀᴛʜ) (ᴇ.ɢ. 𝟷 𝟸 𝟹 𝟺 𝟻)\n\n"
                   "🔷  sᴇɴᴅ 𝟸ғᴀ ᴘᴀssᴡᴏʀᴅ (ɪғ ᴇɴᴀʙʟᴇᴅ)\n\n"
                   "━━━━━━━━━━━━━━━━━━━━━\n"
                   "🔐 ʀᴇᴀᴅʏ? ᴛʏᴘᴇ: /ʟᴏɢɪɴ\n\n"
                   "✨ ᴘᴏᴡᴇʀᴇᴅ ʙʏ @shahnawazzz_1 😎 ✨")
    try:
        await bot.send_file(event.chat_id, START_MEDIA, caption=caption, parse_mode='markdown', supports_streaming=True)
    except Exception:
        try:
            await bot.send_file(event.chat_id, START_MEDIA, caption=caption)
        except Exception:
            await event.reply(caption, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/help'))
async def help_h(event):
    if not is_bot_approved(event.sender_id):
        await event.reply("🔒 Approval pending hai. Owner ke approve karne ke baad use kar paoge.")
        return
    uid = event.sender_id
    if str(uid) in blocked_users:
        return
    base = ("🛑 𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ Bot\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 **Login:**\n• `/start`\n• `/login`\n• `/logout`\n• `/status`\n\n"
            "🔹 **Userbot:**\nLogin ke baad kisi bhi chat me `.alive` bhejo\n\n")
    if uid == OWNER_ID:
        await event.reply(base + "👑 **Owner:**\n• `/pending` — Approval requests\n• `/approve <id>` — Approve user\n• `/unapprove <id>` — Revoke access\n• `/users`\n• `/live`\n• `/remove <tg_id>`\n• `/broadcast <msg>`\n• `/delbroadcast`\n• `/stats`\n• `/block <id>`\n• `/unblock <id>`\n• `/blocked`\n• `/cmdstats`\n• `/userstats <id>`\n• `/cmdusage <cmd>`\n• `/blockcmd <id> <cmd>`\n• `/unblockcmd <id> <cmd>`\n• `/blockedcmds <id>`\n• `/cmdcleanup`\n• `/clearsessions`\n• `/resetusers`\n• `/resetall`\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n✨ **ᴘᴏᴡᴇʀᴇᴅ ʙʏ @shahnawazzz_1** 😎", parse_mode='markdown', buttons=[Button.inline("📋 COMMAND PANEL", b"jc:home")])
    else:
        await event.reply(base + "━━━━━━━━━━━━━━━━━━━━━\n✨ **ᴘᴏᴡᴇʀᴇᴅ ʙʏ @shahnawazzz_1** 😎", parse_mode='markdown', buttons=[Button.inline("📋 COMMAND PANEL", b"jc:home")])

@bot.on(events.NewMessage(pattern='/status'))
async def status_h(event):
    if not is_bot_approved(event.sender_id):
        await event.reply("🔒 Approval pending hai. Owner ke approve karne ke baad use kar paoge.")
        return
    if str(event.sender_id) in blocked_users:
        return
    u = get_user(event.sender_id)
    if u["logged_in"]:
        await event.reply(f"✅ **Logged In**\n👤 `{u.get('tg_name','N/A')}`\n🆔 `{u.get('tg_id','N/A')}`\n🕐 `{u.get('login_time','N/A')}`", parse_mode='markdown')
    else:
        await event.reply("❌ Not logged in. `/login`")

@bot.on(events.NewMessage(pattern='/login'))
async def login_h(event):
    if not is_bot_approved(event.sender_id):
        await event.reply("🔒 Approval pending hai. Owner ke approve karne ke baad use kar paoge.")
        return
    log.info(f"🔥 /login from {event.sender_id}")
    try:
        uid = str(event.sender_id)
        if uid in blocked_users:
            await event.reply("🚫 **Aap is bot ko use nahi kar sakte.**")
            return
        u = get_user(event.sender_id)
        if u["logged_in"]:
            await event.reply(f"✅ Already logged in as `{u.get('tg_name', 'N/A')}`\n\nPehle `/logout` karo.", parse_mode='markdown')
            return
        await event.reply("📱 **Phone number bhejo** (country code ke saath)\nExample: `+919876543210`\n\n⚠️ Sirf apne Telegram account ka login karo. Login session is bot ke server/database par save hoga; kisi aur ka OTP ya password share mat karo.", parse_mode='markdown')
        u["step"] = "await_phone"
        asyncio.create_task(save_user(event.sender_id, u))
    except Exception as e:
        log.error(f"❌ /login error: {e}", exc_info=True)

@bot.on(events.NewMessage(pattern='/logout'))
async def logout_h(event):
    if not is_bot_approved(event.sender_id):
        await event.reply("🔒 Approval pending hai. Owner ke approve karne ke baad use kar paoge.")
        return
    log.info(f"🔥 /logout from {event.sender_id}")
    try:
        uid = str(event.sender_id)
        if uid in blocked_users:
            if uid in user_bots:
                try: await user_bots[uid].disconnect()
                except Exception: pass
                del user_bots[uid]
            return
        u = get_user(uid)
        if not u["logged_in"]:
            await event.reply("❌ Aap login nahi ho. `/login`")
            return
        if uid in user_bots:
            try: await user_bots[uid].disconnect()
            except Exception: pass
            del user_bots[uid]
        u["logged_in"] = False
        u["session_string"] = None
        u["step"] = None
        await event.reply("✅ **Logged Out!**")
        asyncio.create_task(save_user(uid, u))
    except Exception as e:
        log.error(f"❌ /logout error: {e}", exc_info=True)

@bot.on(events.NewMessage(pattern=r'^/approve(?:\s+(\d+))?$'))
async def approve_access_h(event):
    if event.sender_id != OWNER_ID:
        return
    target = event.pattern_match.group(1)
    if not target:
        await event.reply("Usage: `/approve <telegram_user_id>`", parse_mode='markdown')
        return
    info = all_users_db.get(str(target), {})
    info.update({"tg_id": str(target), "approved": True})
    info.setdefault("name", "Unknown")
    info.setdefault("username", "None")
    info.setdefault("first_seen", datetime.now().strftime("%d %b %Y, %I:%M %p"))
    all_users_db[str(target)] = info
    await save_all_user(target, info)
    await event.reply(f"✅ Approved! User `{target}` ab /login karke bot use kar sakta hai.", parse_mode='markdown')
    try:
        await bot.send_message(int(target), "✅ Owner ne aapki request approve kar di hai. Ab /start aur /login use kar sakte ho.")
    except Exception:
        pass

@bot.on(events.NewMessage(pattern=r'^/unapprove(?:\s+(\d+))?$'))
async def unapprove_access_h(event):
    if event.sender_id != OWNER_ID:
        return
    target = event.pattern_match.group(1)
    if not target:
        await event.reply("Usage: `/unapprove <telegram_user_id>`", parse_mode='markdown')
        return
    info = all_users_db.setdefault(str(target), {"tg_id": str(target), "name": "Unknown", "username": "None"})
    info["approved"] = False
    await save_all_user(target, info)
    u = users_db.get(str(target), {})
    if u:
        u.update({"logged_in": False, "session_string": None, "step": None})
        await save_user(target, u)
    cli = user_bots.pop(str(target), None)
    if cli:
        try: await cli.disconnect()
        except Exception: pass
    pending = pending_clients.pop(str(target), None)
    if pending:
        try: await pending.disconnect()
        except Exception: pass
    await event.reply(f"🚫 Access revoked for `{target}`. Existing userbot session disconnected.", parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/pending$'))
async def pending_access_h(event):
    if event.sender_id != OWNER_ID:
        return
    pending = [(uid, data) for uid, data in all_users_db.items() if not data.get("approved", False)]
    if not pending:
        await event.reply("✅ Koi pending approval nahi hai.")
        return
    out = "🔒 **Pending Approval Requests**\n\n"
    for uid, data in pending[:50]:
        out += f"• {data.get('name', 'Unknown')} — `{uid}`\n  Approve: `/approve {uid}`\n\n"
    await event.reply(out, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/users'))
async def users_h(event):
    if event.sender_id != OWNER_ID:
        await event.reply("❌ Sirf owner.")
        return
    if not all_users_db:
        await event.reply("📭 Koi user nahi.")
        return
    logged = sum(1 for u in users_db.values() if u.get("logged_in"))
    t = f"👥 **Total: {len(all_users_db)}**\n✅ **Logged In: {logged}**\n🚫 **Blocked: {len(blocked_users)}**\n" + "═" * 25 + "\n\n"
    for i, (uid, info) in enumerate(list(all_users_db.items())[:50], 1):
        is_logged = uid in users_db and users_db[uid].get("logged_in")
        st = "🟢" if is_logged else "🔴"
        if uid in blocked_users:
            st = "🚫"
        t += f"{i}. {st} `{info.get('name','Unknown')}`\n"
        t += f"   🆔 `{uid}`\n"
        t += f"   📛 @{info.get('username','None')}\n"
        t += f"   📅 `{info.get('first_seen','N/A')}`\n\n"
    if len(all_users_db) > 50:
        t += f"\n_...aur {len(all_users_db) - 50}_"
    await event.reply(t, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/live'))
async def live_h(event):
    if event.sender_id != OWNER_ID:
        await event.reply("❌ Sirf owner.")
        return
    if not all_users_db and not users_db:
        await event.reply("📭 Koi user nahi.")
        return

    all_ids = set(all_users_db.keys()) | set(users_db.keys())
    active, logged_off, started_only, offline = [], [], [], []

    for uid in all_ids:
        u = users_db.get(uid, {})
        info = all_users_db.get(uid, {})
        name = u.get("tg_name") or info.get("name") or "Unknown"
        tg_id = u.get("tg_id") or info.get("tg_id") or uid
        username = u.get("tg_username") or info.get("username") or "None"
        first_seen = info.get("first_seen", "N/A")

        is_logged = u.get("logged_in", False)
        is_running = uid in user_bots
        has_start = uid in all_users_db

        entry = {"name": name, "tg_id": tg_id, "username": username, "first_seen": first_seen}

        if is_running:
            active.append(entry)
        elif is_logged:
            logged_off.append(entry)
        elif has_start and not is_logged:
            started_only.append(entry)
        else:
            offline.append(entry)

    def render_list(title, items, icon):
        if not items:
            return ""
        out = f"{icon} **{title} ({len(items)})**\n\n"
        for i, info in enumerate(items, 1):
            out += f"{i}. 👤 `{info['name']}`\n"
            out += f"   🆔 `{info['tg_id']}`\n"
            out += f"   📛 @{info['username']}\n"
            out += f"   📅 `{info['first_seen']}`\n\n"
        return out + "─" * 25 + "\n\n"

    text = f"📡 **LIVE STATUS**\n" + "═" * 25 + "\n\n"
    text += render_list("ACTIVE (Userbot Running)", active, "🟢")
    text += render_list("LOGGED IN (Userbot Off)", logged_off, "🟡")
    text += render_list("STARTED ONLY (Not Logged In)", started_only, "🔵")
    text += render_list("OFFLINE / SESSION REMOVED", offline, "🔴")
    text += (f"━━━━━━━━━━━━━━━━━━\n"
             f"📊 **Summary**\n"
             f"🟢 Active: `{len(active)}`\n"
             f"🟡 Logged (off): `{len(logged_off)}`\n"
             f"🔵 Started only: `{len(started_only)}`\n"
             f"🔴 Offline: `{len(offline)}`\n"
             f"🚫 Blocked: `{len(blocked_users)}`")
    await event.reply(text, parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/remove(\s+.*)?$'))
async def remove_h(event):
    if event.sender_id != OWNER_ID:
        await event.reply("❌ Sirf owner.")
        return
    args = (event.text or "").replace("/remove", "").strip()
    if not args:
        await event.reply("❌ `/remove <tg_id>`")
        return
    t = args.split()[0]
    f = None
    for uid, u in users_db.items():
        if str(u.get("tg_id")) == str(t) or uid == str(t):
            f = uid; break
    if not f:
        await event.reply(f"❌ `{t}` not found.")
        return
    n = users_db[f].get("tg_name", "Unknown")
    if f in user_bots:
        try: await user_bots[f].disconnect()
        except Exception: pass
        del user_bots[f]
    try:
        if users_col is not None: await users_col.delete_one({"_id": str(f)})
    except Exception: pass
    del users_db[f]
    await event.reply(f"✅ **Removed!**\n👤 `{n}`\n🆔 `{t}`", parse_mode='markdown')
    try:
        await bot.send_message(int(f), "⚠️ Access removed. `/login` dobara.")
    except Exception: pass

@bot.on(events.NewMessage(pattern=r'^/broadcast(\s+.*)?$'))
async def bcast_h(event):
    if event.sender_id != OWNER_ID: return
    has_reply = event.is_reply
    reply_msg = await event.get_reply_message() if has_reply else None
    text = (event.text or "").replace("/broadcast", "", 1).strip()
    if not text and not has_reply and not event.media:
        await event.reply("❌ **Usage:**\n`/broadcast <message>`"); return
    targets = set(all_users_db.keys())
    for uid, u in users_db.items():
        if u.get("logged_in"):
            targets.add(str(uid))
            if u.get("tg_id"): targets.add(str(u["tg_id"]))
    sent, failed = 0, 0
    sent_msgs = []
    for user_id in targets:
        try:
            sent_msg = None
            if has_reply and reply_msg:
                if reply_msg.media:
                    sent_msg = await bot.send_file(int(user_id), reply_msg.media, caption=reply_msg.text or "")
                else:
                    sent_msg = await bot.send_message(int(user_id), reply_msg.text or "")
            elif event.media:
                sent_msg = await bot.send_file(int(user_id), event.media, caption=text)
            else:
                sent_msg = await bot.send_message(int(user_id), text)
            if sent_msg: sent_msgs.append({"user_id": int(user_id), "message_id": sent_msg.id, "timestamp": time.time()})
            sent += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            log.error(f"Broadcast to {user_id} failed: {e}")
            failed += 1
    broadcast_history.clear()
    broadcast_history.extend(sent_msgs)
    await event.reply(f"✅ **Broadcast Done!**\n📤 Sent: `{sent}`\n❌ Failed: `{failed}`\n👥 Total: `{len(targets)}`\n\n💡 Delete: `/delbroadcast`", parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/delbroadcast'))
async def delbroadcast_h(event):
    if event.sender_id != OWNER_ID:
        await event.reply("❌ Sirf owner."); return
    if not broadcast_history:
        await event.reply("📭 Koi recent broadcast nahi hai."); return
    msg = await event.reply(f"🗑️ Deleting broadcast from `{len(broadcast_history)}` users...")
    deleted, failed, too_old = 0, 0, 0
    for entry in list(broadcast_history):
        try:
            age = time.time() - entry["timestamp"]
            if age > 48 * 3600: too_old += 1; continue
            await bot.delete_messages(entry["user_id"], [entry["message_id"]])
            deleted += 1
            await asyncio.sleep(0.3)
        except Exception: failed += 1
    broadcast_history.clear()
    await msg.edit(f"✅ **Broadcast Deleted!**\n🗑️ Deleted: `{deleted}`\n❌ Failed: `{failed}`\n⏰ Too old: `{too_old}`")

@bot.on(events.NewMessage(pattern='/stats'))
async def stats_h(event):
    if event.sender_id != OWNER_ID: return
    await event.reply(f"📊 **Bot Stats**\n👥 Total: `{len(all_users_db)}`\n✅ Logged: `{len(users_db)}`\n🟢 Active: `{len(user_bots)}`\n🚫 Blocked: `{len(blocked_users)}`\n📊 Cmd Tracked: `{len(command_usage_db)}`", parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/cmdstats'))
async def cmdstats_h(event):
    if event.sender_id != OWNER_ID: return
    cmd_totals = {}
    user_totals = {}
    for uid, data in command_usage_db.items():
        cmds = data.get("commands", {}) if isinstance(data, dict) else data
        for cmd_name, count in cmds.items():
            cmd_totals[cmd_name] = cmd_totals.get(cmd_name, 0) + count
            user_totals[uid] = user_totals.get(uid, 0) + count
    if not cmd_totals:
        await event.reply("📭 Abhi tak koi command use nahi hui.")
        return
    top_cmds = sorted(cmd_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    top_users = sorted(user_totals.items(), key=lambda x: x[1], reverse=True)[:10]
    text = f"📊 **COMMAND STATS**\n" + "═" * 25 + "\n\n"
    text += "🔥 **Top 10 Commands:**\n\n"
    for i, (cmd_name, count) in enumerate(top_cmds, 1):
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"**{i}.**"
        text += f"{medal} `.{cmd_name}` — **{count}** times\n"
    text += "\n" + "─" * 25 + "\n\n"
    text += "👥 **Top 10 Users:**\n\n"
    for i, (uid, count) in enumerate(top_users, 1):
        name = "Unknown"
        try:
            u = await bot.get_entity(int(uid))
            name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
        except Exception:
            pass
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"**{i}.**"
        text += f"{medal} `{name}` (`{uid}`) — **{count}** commands\n"
    text += f"\n📌 **Total Commands Used:** `{sum(cmd_totals.values())}`\n"
    text += f"👤 **Total Users Tracked:** `{len(command_usage_db)}`"
    await event.reply(text, parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/userstats(\s+.*)?$'))
async def userstats_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/userstats", "").strip()
    if not args:
        await event.reply("❌ `/userstats <user_id>`"); return
    uid = args.split()[0]
    data = command_usage_db.get(uid, {})
    cmds = data.get("commands", {}) if isinstance(data, dict) else data
    if not cmds:
        await event.reply(f"📭 `{uid}` ne koi command use nahi ki."); return
    name = "Unknown"
    try:
        u = await bot.get_entity(int(uid))
        name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
    except Exception:
        pass
    sorted_cmds = sorted(cmds.items(), key=lambda x: x[1], reverse=True)
    total = sum(cmds.values())
    text = f"👤 **User:** `{name}`\n🆔 **ID:** `{uid}`\n📊 **Total:** `{total}` commands\n" + "═" * 25 + "\n\n"
    for i, (cmd_name, count) in enumerate(sorted_cmds, 1):
        text += f"**{i}.** `.{cmd_name}` — **{count}** times\n"
    blocked = command_blocked_db.get(uid, [])
    if blocked:
        text += "\n🚫 **Blocked Commands:**\n"
        for c in blocked:
            text += f"• `.{c}`\n"
    await event.reply(text, parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/cmdusage(\s+.*)?$'))
async def cmdusage_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/cmdusage", "").strip()
    if not args:
        await event.reply("❌ `/cmdusage <command>`\nExample: `/cmdusage clone`"); return
    cmd_name = args.split()[0].lstrip('.')
    users_used = []
    for uid, data in command_usage_db.items():
        cmds = data.get("commands", {}) if isinstance(data, dict) else data
        if cmd_name in cmds: users_used.append((uid, cmds[cmd_name]))
    if not users_used:
        await event.reply(f"📭 `.{cmd_name}` koi bhi use nahi kiya."); return
    users_used.sort(key=lambda x: x[1], reverse=True)
    total = sum(c for _, c in users_used)
    text = f"📌 **Command:** `.{cmd_name}`\n📊 **Total Uses:** `{total}`\n👥 **Users:** `{len(users_used)}`\n" + "═" * 25 + "\n\n"
    for i, (uid, count) in enumerate(users_used[:20], 1):
        name = "Unknown"
        try:
            u = await bot.get_entity(int(uid))
            name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
        except Exception:
            pass
        text += f"**{i}.** `{name}` (`{uid}`) — **{count}** times\n"
    await event.reply(text, parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/blockcmd(\s+.*)?$'))
async def blockcmd_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/blockcmd", "").strip()
    if not args or len(args.split()) < 2:
        await event.reply("❌ **Usage:**\n`/blockcmd <user_id> <command>`"); return
    parts = args.split()
    uid, c = parts[0], parts[1].lstrip('.')
    if uid not in command_blocked_db: command_blocked_db[uid] = []
    if c not in command_blocked_db[uid]: command_blocked_db[uid].append(c)
    if cmd_blocked_col is not None:
        await cmd_blocked_col.update_one({"_id": uid}, {"$set": {"commands": command_blocked_db[uid]}}, upsert=True)
    await event.reply(f"🚫 **Command Blocked!**\n🆔 `{uid}`\n📌 `.{c}`", parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/unblockcmd(\s+.*)?$'))
async def unblockcmd_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/unblockcmd", "").strip()
    if not args or len(args.split()) < 2:
        await event.reply("❌ **Usage:**\n`/unblockcmd <user_id> <command>`"); return
    parts = args.split()
    uid, c = parts[0], parts[1].lstrip('.')
    if uid in command_blocked_db and c in command_blocked_db[uid]:
        command_blocked_db[uid].remove(c)
        if cmd_blocked_col is not None:
            await cmd_blocked_col.update_one({"_id": uid}, {"$set": {"commands": command_blocked_db[uid]}}, upsert=True)
    await event.reply(f"✅ **Command Unblocked!**\n🆔 `{uid}`\n📌 `.{c}`", parse_mode='markdown')

@bot.on(events.NewMessage(pattern=r'^/blockedcmds(\s+.*)?$'))
async def blockedcmds_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/blockedcmds", "").strip()
    if not args:
        await event.reply("❌ `/blockedcmds <user_id>`"); return
    uid = args.split()[0]
    cmds = command_blocked_db.get(uid, [])
    if not cmds:
        await event.reply(f"📭 `{uid}` ke liye koi command blocked nahi hai."); return
    text = f"🚫 **Blocked Commands for `{uid}`**\n" + "═" * 25 + "\n\n"
    for i, c in enumerate(cmds, 1):
        text += f"**{i}.** `.{c}`\n"
    await event.reply(text, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/cmdcleanup'))
async def cmdcleanup_h(event):
    global command_usage_db
    if event.sender_id != OWNER_ID: return
    if cmd_usage_col is None:
        await event.reply("❌ SQLite database available nahi hai."); return
    msg = await event.reply("🧹 Cleaning up old command logs...")
    try:
        cutoff_time = time.time() - (CLEANUP_DAYS * 24 * 60 * 60)
        result = await cmd_usage_col.delete_many({"last_update": {"$lt": cutoff_time}})
        await msg.edit(f"✅ **Cleanup Done!**\n🗑️ Deleted: `{result.deleted_count}` records")
    except Exception as e:
        await msg.edit(f"❌ Error: `{str(e)[:200]}`")

@bot.on(events.NewMessage(pattern=r'^/block(\s+.*)?$'))
async def block_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/block", "").strip()
    if not args and not event.is_reply:
        await event.reply("❌ **Usage:**\n`/block <user_id>`\n`/block @username`\n`/block` (reply to a user)"); return
    target_id = None
    if event.is_reply:
        msg = await event.get_reply_message()
        target_id = msg.sender_id
    elif args.startswith("@"):
        try: u = await bot.get_entity(args); target_id = u.id
        except Exception: await event.reply(f"❌ `{args}` not found."); return
    else:
        try: target_id = int(args.split()[0])
        except Exception: await event.reply("❌ Invalid user ID."); return
    if target_id == OWNER_ID:
        await event.reply("❌ Aap khud ko block nahi kar sakte."); return
    target_id = str(target_id)
    if target_id in blocked_users:
        await event.reply(f"⚠️ Ye user already blocked hai.\n🆔 `{target_id}`", parse_mode='markdown'); return
    if target_id in user_bots:
        try: await user_bots[target_id].disconnect()
        except Exception: pass
        del user_bots[target_id]
    if target_id in users_db:
        users_db[target_id]["logged_in"] = False
        users_db[target_id]["session_string"] = None
        await save_user(target_id, users_db[target_id])
    await block_user_db(target_id)
    await event.reply(f"🚫 **User Blocked!**\n🆔 `{target_id}`", parse_mode='markdown')
    try:
        await bot.send_message(int(target_id), "🚫 Aapko block kar diya gaya hai.")
    except Exception: pass

@bot.on(events.NewMessage(pattern=r'^/unblock(\s+.*)?$'))
async def unblock_h(event):
    if event.sender_id != OWNER_ID: return
    args = (event.text or "").replace("/unblock", "").strip()
    if not args:
        await event.reply("❌ `/unblock <user_id/@username>`"); return
    target_id = None
    if args.startswith("@"):
        try: u = await bot.get_entity(args); target_id = u.id
        except Exception: await event.reply(f"❌ `{args}` not found."); return
    else:
        try: target_id = int(args.split()[0])
        except Exception: await event.reply("❌ Invalid user ID."); return
    target_id = str(target_id)
    if target_id not in blocked_users:
        await event.reply(f"⚠️ Ye user blocked nahi hai.\n🆔 `{target_id}`", parse_mode='markdown'); return
    await unblock_user_db(target_id)
    await event.reply(f"✅ **User Unblocked!**\n🆔 `{target_id}`", parse_mode='markdown')
    try:
        await bot.send_message(int(target_id), "✅ Aapko unblock kar diya gaya hai.")
    except Exception: pass

@bot.on(events.NewMessage(pattern='/blocked'))
async def blocked_h(event):
    if event.sender_id != OWNER_ID: return
    if not blocked_users:
        await event.reply("📭 Koi blocked user nahi hai."); return
    text = f"🚫 **Blocked ({len(blocked_users)})**\n\n"
    for i, uid in enumerate(list(blocked_users)[:50], 1): text += f"{i}. `{uid}`\n"
    await event.reply(text, parse_mode='markdown')

@bot.on(events.NewMessage(pattern='/clearsessions'))
async def clear_sessions_h(event):
    if event.sender_id != OWNER_ID: return
    if users_col is None:
        await event.reply("❌ No DB"); return
    try:
        for uid in list(user_bots.keys()):
            try: await user_bots[uid].disconnect()
            except: pass
            del user_bots[uid]
        await users_col.delete_many({})
        users_db.clear()
        await event.reply("✅ All sessions cleared!")
    except Exception as e:
        await event.reply(f"❌ {str(e)[:200]}")

@bot.on(events.NewMessage(pattern='/resetusers'))
async def resetusers_h(event):
    if event.sender_id != OWNER_ID: return
    if all_users_col is None:
        await event.reply("❌ No DB"); return
    try:
        await all_users_col.delete_many({})
        all_users_db.clear()
        await event.reply("✅ All users reset!")
    except Exception as e:
        await event.reply(f"❌ {str(e)[:200]}")

@bot.on(events.NewMessage(pattern='/resetall'))
async def resetall_h(event):
    if event.sender_id != OWNER_ID: return
    try:
        for uid in list(user_bots.keys()):
            try: await user_bots[uid].disconnect()
            except: pass
            del user_bots[uid]
        if users_col is not None: await users_col.delete_many({})
        if all_users_col is not None: await all_users_col.delete_many({})
        users_db.clear()
        all_users_db.clear()
        user_data_cache.clear()
        broadcast_history.clear()
        await event.reply("✅ **FULL RESET DONE!**")
    except Exception as e:
        await event.reply(f"❌ {str(e)[:200]}")

# ═══════════════ LOGIN FLOW ═══════════════
@bot.on(events.NewMessage(func=lambda e: e.is_private and not e.out and not (e.text or "").startswith('/')))
async def login_flow(event):
    uid = event.sender_id
    if str(uid) in blocked_users: return
    if not is_bot_approved(uid):
        await event.reply("🔒 Approval pending hai. Owner ke approve karne ke baad hi login kar sakte ho.")
        return
    u = get_user(uid)
    st = u.get("step")
    if not st: return
    text = (event.text or "").strip()

    if st == "await_phone":
        if not text.startswith("+") or len(re.sub(r'\D', '', text)) < 8:
            await event.reply("❌ Galat. Example: `+919876543210`"); return
        try:
            await event.reply("⏳ Client ban raha hai...")
            cli = TelegramClient(
                StringSession(), API_ID, API_HASH,
                device_model="𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡",
                system_version="SHAHNAWAZ",
                app_version="1.0"
            )
            await cli.connect()
            sent = await cli.send_code_request(text)
            pending_clients[uid] = cli
            u["phone"] = text
            u["phone_code_hash"] = sent.phone_code_hash
            u["step"] = "await_otp"
            asyncio.create_task(save_user(uid, u))
            await event.reply(
                "📨 **OTP BHEJA GAYA!**\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "OTP space ke saath bhejo\n\n"
                "**Example:**\n"
                "`1 2 3 4 5`\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ **Bina space wala OTP accept nahi hoga**",
                parse_mode='markdown'
            )
        except Exception as e:
            u["step"] = None
            asyncio.create_task(save_user(uid, u))
            await event.reply(f"❌ `{str(e)[:200]}`")
        return

    if st == "await_otp":
        raw = text.strip()
        if " " not in raw:
            await event.reply(
                "❌ **Space ke saath bhejo**\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "**Example:**\n"
                "`1 2 3 4 5`\n\n"
                "━━━━━━━━━━━━━━━━━━━━━\n"
                "⚠️ **Bina space wala OTP accept nahi hoga**",
                parse_mode='markdown'
            )
            return
        otp = raw.replace(" ", "")
        if not otp.isdigit():
            await event.reply("❌ Sirf digits (space ke saath)"); return
        cli = pending_clients.get(uid)
        if not cli:
            u["step"] = None
            asyncio.create_task(save_user(uid, u))
            await event.reply("❌ Session khatam."); return
        try:
            await cli.sign_in(phone=u["phone"], code=otp, phone_code_hash=u["phone_code_hash"])
            await finalize(uid, cli, event)
        except SessionPasswordNeededError:
            u["step"] = "await_password"
            asyncio.create_task(save_user(uid, u))
            await event.reply("🔐 **Two-Step Verification Password Required!**")
        except PhoneCodeInvalidError:
            await event.reply("❌ Galat OTP.")
        except PhoneCodeExpiredError:
            u["step"] = None
            asyncio.create_task(save_user(uid, u))
            await event.reply("❌ Expire. `/login`")
        except Exception as e:
            u["step"] = None
            asyncio.create_task(save_user(uid, u))
            await event.reply(f"❌ `{str(e)[:200]}`")
        return

    if st == "await_password":
        cli = pending_clients.get(uid)
        if not cli:
            u["step"] = None
            asyncio.create_task(save_user(uid, u))
            await event.reply("❌ Session khatam."); return
        try:
            await cli.sign_in(password=text)
            try: await event.delete()
            except: pass
            await finalize(uid, cli, event)
        except PasswordHashInvalidError:
            await event.reply("❌ Galat 2FA Password!")
        except Exception as e:
            u["step"] = None
            asyncio.create_task(save_user(uid, u))
            await event.reply(f"❌ `{str(e)[:200]}`")
        return

async def finalize(uid, cli, event):
    uid = str(uid)
    if not is_bot_approved(uid):
        try: await cli.disconnect()
        except: pass
        await event.reply("🔒 Approval revoke/pending hai. Login cancel kar diya gaya.")
        return
    if uid in blocked_users:
        try: await cli.disconnect()
        except: pass
        return
    u = users_db[uid]
    try:
        me = await cli.get_me()
        u["session_string"] = cli.session.save()
        u["logged_in"] = True
        u["step"] = None
        u["tg_id"] = me.id
        u["tg_name"] = ((me.first_name or "") + (" " + me.last_name if me.last_name else "")).strip()
        u["tg_username"] = me.username or "None"
        u["login_time"] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        await save_user(uid, u)
        if uid not in all_users_db:
            await track_start(uid, u["tg_name"], u["tg_username"])
        pending_clients.pop(uid, None)
        await start_ub(uid, u["session_string"])
        await event.reply(
            f"🎉 **USERBOT ACTIVE!**\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"👤 **{u['tg_name']}**\n"
            f"🆔 `{u['tg_id']}`\n"
            f"📛 @{u['tg_username']}\n\n"
            f"**Bot On hai** ye check karne ke liye\n"
            f"👉 `.alive` Bhejo\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"**Logout:** `/logout`",
            parse_mode='markdown'
        )
        try:
            await bot.send_message(OWNER_ID,
                f"🔔 **New Login**\n👤 `{u['tg_name']}`\n🆔 `{u['tg_id']}`\n📛 @{u['tg_username']}\n🕐 `{u['login_time']}`", parse_mode='markdown')
        except Exception: pass
    except Exception as e:
        log.error(f"finalize: {e}")
        await event.reply(f"❌ `{str(e)[:200]}`")

async def start_ub(uid, ss):
    uid = str(uid)
    if uid in blocked_users or not is_bot_approved(uid): return
    if uid in user_bots: return
    try:
        ub = TelegramClient(
            StringSession(ss), API_ID, API_HASH,
            connection_retries=None,
            retry_delay=1,
            auto_reconnect=True,
            request_retries=5,
            timeout=20,
            flood_sleep_threshold=120,
            device_model="𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡",
            system_version="SHAHNAWAZ",
            app_version="1.0",
            lang_code="en",
            system_lang_code="en-US",
            use_ipv6=False
        )
        await ub.start()
        log.info(f"✅ Userbot started: {uid}")
        user_bots[uid] = ub
        reg_commands(ub, int(uid))
        # Enable cross-chat JARVIS auto-reply only on the owner's personal session.
        if int(uid) == OWNER_ID:
            from bot3 import register_jarvis
            register_jarvis(ub, int(uid), auto_reply=True)
        asyncio.create_task(_restore_schedules_on_startup(ub, int(uid)))
        asyncio.create_task(keepalive_loop(uid, ub))
    except Exception as e:
        log.error(f"❌ start_ub {uid} FAILED: {type(e).__name__}: {e}")
        try:
            if uid in users_db:
                users_db[uid]["logged_in"] = False
                users_db[uid]["session_string"] = None
                await save_user(uid, users_db[uid])
        except: pass

async def keepalive_loop(uid, ub):
    """Har 20 sec me ping bhejke connection zinda rakhta hai."""
    from telethon.tl.functions import PingRequest
    while True:
        await asyncio.sleep(20)
        # Agar userbot band ho gaya ya replace ho gaya to loop khatam
        if uid not in user_bots or user_bots[uid] is not ub:
            return
        try:
            if not ub.is_connected():
                log.warning(f"🔄 Reconnecting userbot {uid}")
                await ub.connect()
            await asyncio.wait_for(
                ub(PingRequest(ping_id=random.randint(1, 10**9))),
                timeout=10
            )
        except asyncio.TimeoutError:
            log.warning(f"⏱️ Ping timeout for {uid}, forcing reconnect...")
            try:
                await ub.disconnect()
                await asyncio.sleep(1)
                await ub.connect()
            except Exception as e:
                log.error(f"Reconnect {uid} failed: {e}")
        except Exception as e:
            log.warning(f"Keepalive {uid}: {e}")
            try:
                if not ub.is_connected():
                    await ub.connect()
            except: pass

# ═══════════════ SCHEDULER HELPERS ═══════════════
def _parse_schedule_time(date_str: str, time_str: str):
    """
    Accepts:
      - Date: YYYY-MM-DD  or  DD-MM-YYYY  or  DD/MM/YYYY
      - Time: HH:MM:SS or HH:MM  (24h)
               hh:MM:SS AM/PM or hh:MM AM/PM  (12h)
    Returns (datetime in IST) or raises ValueError with clean message.
    """
    date_str = date_str.strip()
    dt_date = None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            dt_date = datetime.strptime(date_str, fmt)
            break
        except ValueError:
            continue
    if dt_date is None:
        raise ValueError("Date `YYYY-MM-DD` ya `DD-MM-YYYY` format me do")

    time_str = time_str.strip().upper().replace(".", "")
    dt_time = None

    if "AM" in time_str or "PM" in time_str:
        for fmt in ("%I:%M:%S %p", "%I:%M %p", "%I %p"):
            try:
                dt_time = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                continue
        if dt_time is None:
            raise ValueError("12-hour format: `hh:MM:SS AM/PM` ya `hh:MM AM/PM`")
    else:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                dt_time = datetime.strptime(time_str, fmt)
                break
            except ValueError:
                continue
        if dt_time is None:
            raise ValueError("24-hour format: `HH:MM:SS` ya `HH:MM`")

    combined = dt_date.replace(
        hour=dt_time.hour,
        minute=dt_time.minute,
        second=dt_time.second,
        tzinfo=IST_TZ,
    )
    return combined


def _fmt_ist_12h(ts: float) -> str:
    """Format IST time and include milliseconds only when non-zero."""
    dt = datetime.fromtimestamp(ts, IST_TZ)
    base = dt.strftime("%I:%M:%S")
    if dt.microsecond:
        base += f".{dt.microsecond // 1000:03d}"
    return base + dt.strftime(" %p IST")


def _fmt_ist_full(ts: float) -> str:
    """Format a timestamp as `21 Oct 2026, 06:19:00 PM IST`."""
    return datetime.fromtimestamp(ts, IST_TZ).strftime("%d %b %Y, %I:%M:%S %p IST")


# ═══════════════ SCHEDULER CORE (PERFECT TIMING) ═══════════════
async def _scheduler_runner(client, doc):
    """Best-effort high-resolution scheduler; delivery still depends on Telegram/network."""
    try:
        target_ts = float(doc["scheduled_ts"])
        fire_ts = target_ts - max(0.0, float(PING_OFFSET))

        # Coarse sleep while the target is far away; avoid needless wakeups.
        while True:
            diff = fire_ts - time.time()
            if diff <= 2.0:
                break
            if diff > 120:
                await asyncio.sleep(min(60.0, diff - 10.0))
            elif diff > 10:
                await asyncio.sleep(min(2.0, diff - 2.0))
            else:
                await asyncio.sleep(min(0.2, diff - 1.0))

        if sched_col is None:
            return
        fresh = await sched_col.find_one({"_id": doc["_id"]})
        if not fresh or fresh.get("status") != "pending":
            return

        send_text = fresh["text"]
        target = fresh["target_id"]
        comment_to = fresh.get("comment_to")
        reply_to = fresh.get("reply_to_msg_id")

        # Warm the already-connected Telegram connection before the target time.
        try:
            from telethon.tl.functions import PingRequest
            await asyncio.wait_for(client(PingRequest(ping_id=random.randint(1, 10**9))), timeout=1.5)
        except Exception:
            pass

        async def _real_send():
            try:
                if comment_to:
                    try:
                        await client.send_message(target, send_text, comment_to=int(comment_to))
                    except Exception:
                        await client.send_message(target, send_text)
                elif reply_to:
                    try:
                        await client.send_message(target, send_text, reply_to=reply_to)
                    except Exception:
                        await client.send_message(target, send_text)
                else:
                    await client.send_message(target, send_text)

                await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "sent", "sent_at": time.time()}})
                drift_ms = (time.time() - target_ts) * 1000.0
                log.info(f"⚡ Scheduled msg {doc['_id'][:8]} SENT (drift: {drift_ms:+.1f}ms)")
                cf_chat = fresh.get("confirm_chat_id")
                cf_msg = fresh.get("confirm_msg_id")
                if cf_chat and cf_msg:
                    try:
                        await client.delete_messages(int(cf_chat), [int(cf_msg)])
                    except Exception:
                        pass
            except FloodWaitError as e:
                log.warning(f"⏰ FloodWait {e.seconds}s; Telegram delayed the scheduled send")
                await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": f"FloodWait {e.seconds}s"}})
            except Exception as e:
                log.error(f"⏰ Send failed: {e}")
                await sched_col.update_one({"_id": doc["_id"]}, {"$set": {"status": "failed", "error": str(e)[:200]}})

        # Convert remaining wall-clock time to monotonic time once near the deadline.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, fire_ts - time.time())
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            if remaining > 0.050:
                await asyncio.sleep(remaining - 0.020)
            elif remaining > 0.003:
                await asyncio.sleep(remaining / 2.0)
            else:
                # Yield to the event loop while waiting for the final few ms.
                await asyncio.sleep(0)

        # Await directly instead of creating another task, removing one scheduling hop.
        await _real_send()

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error(f"_scheduler_runner error: {e}", exc_info=True)


async def _restore_schedules_on_startup(client, owner_id):
    if sched_col is None:
        return
    try:
        cursor = sched_col.find({
            "owner_id": owner_id,
            "status": "pending",
            "scheduled_ts": {"$gt": time.time() - 300}
        })
        docs = await cursor.to_list(length=200)
        for d in docs:
            asyncio.create_task(_scheduler_runner(client, d))
        if docs:
            log.info(f"⏰ Restored {len(docs)} scheduled message(s) for {owner_id}")
    except Exception as e:
        log.warning(f"_restore_schedules_on_startup: {e}")

# ═══════════════ USERBOT COMMANDS ═══════════════
def reg_commands(client, owner_id):
    db = get_user_data(owner_id)
    defaults = {
        "owner_id": owner_id, "admins": [], "ghost_data": {}, "ghost_on": False,
        "ghostall_data": {}, "ghostall_on": False,
        "auto_react_dm_enabled": False, "auto_react_group_enabled": False,
        "auto_react_emojis": ['🥰','😭','😂','🤗','🆒','😇','🤩','😎','🫡','🤝','👍','❤️','❤️‍🔥','🔥','💯','👀','😁','🤨','🕊','🎉','👏','⚡️','🤷','👌','🧑‍💻','🏆','✍️','😴','🤔'],
        "auto_reply": {"enabled": False, "mode": "always",
            "text": "The owner is busy with some personal work at the moment😌Please wait until they are back online🥀",
            "replied_users": [], "approved_users": []},
        "ai_reply": {"enabled": False, "approved_users": []},
        "pmguard": {"enabled": False, "warn_limit": 5, "custom_enabled": False, "custom_limit": None},
        "pm_warnings": {}, "approved": [], "blacklist": [],
        "muted": {}, "gmuted": {}, "blocklist": [], "locked_groups": [],
        "clone_data": {}, "cloneall_data": {}, "saved_profile": {},
        "reply_raid": {}, "rr_raid": {}, "flag_raid": {},
        "heart_raid": {}, "spam_running": False, "fast_gc_data": {},
        "start_time": datetime.now().isoformat()
    }
    for k, v in defaults.items():
        if k not in db: db[k] = v
    # 🆕 Force update auto_react_emojis on every start
    db["auto_react_emojis"] = defaults["auto_react_emojis"]
    if "warn_limit" not in db.get("pmguard", {}): db["pmguard"]["warn_limit"] = 5
    if "custom_enabled" not in db.get("pmguard", {}): db["pmguard"]["custom_enabled"] = False
    if "custom_limit" not in db.get("pmguard", {}): db["pmguard"]["custom_limit"] = None
    if "approved_users" not in db.get("auto_reply", {}): db["auto_reply"]["approved_users"] = []
    if "ai_reply" not in db:
        db["ai_reply"] = {"enabled": False, "approved_users": []}
    # 🆕 Save forced update to SQLite
    asyncio.create_task(save_user_data(owner_id, db))

    async def save_db():
        try:
            data = dict(db)
            for key in ["reply_raid", "rr_raid", "flag_raid", "heart_raid"]:
                if key in data and isinstance(data[key], dict):
                    new_dict = {}
                    for k, v in data[key].items():
                        new_dict[str(k)] = list(v) if isinstance(v, set) else v
                    data[key] = new_dict
            await save_user_data(owner_id, data)
            user_data_cache[str(owner_id)] = data
        except Exception as e:
            log.error(f"save_db: {e}")

    async def refresh_db():
        try:
            fresh = await user_data_col.find_one({"_id": str(owner_id)})
            if fresh:
                fresh.pop("_id", None)
                for k, v in fresh.items(): db[k] = v
                # 🆕 Force update emoji list after refresh too
                db["auto_react_emojis"] = defaults["auto_react_emojis"]
                user_data_cache[str(owner_id)] = dict(db)
        except Exception as e:
            log.warning(f"refresh_db: {e}")

    async def get_fresh_data(key):
        await refresh_db()
        return db.get(key)

    def get_args(msg):
        t = msg.text or ''
        for m in re.finditer(r'\.\w+\s*(.*)', t): return m.group(1).strip()
        return ''

    def is_owner(uid): return uid == db.get("owner_id", owner_id)

    async def resolve_user(client, msg, text):
        if msg.is_reply: return (await msg.get_reply_message()).sender_id
        if text:
            m = re.search(r'@(\w+)', text)
            if m:
                try: u = await client.get_entity(m.group(1)); return u.id
                except: return None
            try: u = await client.get_entity(int(text)); return u.id
            except: return None
        return None

    def user_link(uid): return f"[User](tg://user?id={uid})"

    async def reply_to_target(event, text):
        if event.reply_to_msg_id:
            try: await client.send_message(event.chat_id, text, reply_to=event.reply_to_msg_id); return
            except: pass
        await event.reply(text)

    async def send_anim(event, text, parse_mode=None):
        if event.reply_to_msg_id:
            return await client.send_message(event.chat_id, text, reply_to=event.reply_to_msg_id, parse_mode=parse_mode)
        return await event.reply(text, parse_mode=parse_mode)

    async def raid_loop(event, target_id, raid_key, message_func, delay=1):
        chat_id = str(event.chat_id)
        raid_dict = raid_state[raid_key]
        if chat_id not in raid_dict:
            raid_dict[chat_id] = set()
        raid_dict[chat_id].add(target_id)
        while target_id in raid_dict.get(chat_id, set()):
            try:
                await message_func()
                await asyncio.sleep(delay)
            except Exception:
                break

    RAID_MESSAGES = [
        "🚀 Raid #{n}!\n🔥 Attack mode on, brace yourself!",
        "💥 Spam #{n}!\n⚡ We can't be stopped now!",
        "🔥 Attack #{n}!\n💫 Unstoppable raid force incoming!",
        "⚡ Raid #{n}\n🚀 Here we go again, hold tight!",
        "🌀 Flood #{n}\n💥 Breaking through everything!",
    ]
    REPLY_MESSAGES = [
        "💬 Spam reply!\n🔥 Raid mode is active!",
        "🤖 Bot attack!\n⚡ Beware the incoming storm!",
        "📢 Hey there!\n🌀 Message wave incoming!",
        "⚠️ Raid mode!\n💥 Brace yourself, it's raining!",
        "🌀 Flooding the chat!\n🚀 Can't be stopped!",
    ]
    SPAM_EMOJIS = ['🌟', '✨', '🔥', '💥', '⚡', '🌀', '🚀', '🎯', '💫', '⭐']
    DEFAULT_SPAM_TEXTS = ["🔥 Spam attack!", "🚀 Flooding the chat!", "💥 Here comes the spam!", "🌀 Spam mode activated!", "⚡ Lightning spam!", "🎯 Target acquired!", "💫 Spam spam spam!", "🌟 Spam with style!", "✨ Spam is life!", "🔥 Spam till you drop!"]

    def cmd(pattern, owner_only=False, delete_cmd=True):
        def wrapper(func):
            @client.on(events.NewMessage(pattern=rf'^\.{pattern}(\s.*)?$'))
            async def handler(event):
                if event.sender_id != db.get("owner_id", owner_id): return
                if owner_only and not is_owner(event.sender_id): return
                asyncio.create_task(log_command_usage(event.sender_id, pattern))
                if delete_cmd:
                    try: await event.delete()
                    except: pass
                try:
                    await refresh_db()
                    await func(event)
                    await save_db()
                except FloodWaitError as e:
                    log.warning(f"Flood wait {e.seconds}s")
                    await event.reply(f"⏳ Flood wait {e.seconds} sec...")
                    await asyncio.sleep(e.seconds)
                except Exception as e:
                    log.error(f"Cmd .{pattern} error: {e}")
                    await event.reply(f"❌ Error: {str(e)[:200]}")
            return handler
        return wrapper

    # ═══════════════ HELP MENU (2 PARTS) ═══════════════
    @cmd('help', delete_cmd=False)
    async def help_cmd(event):
        part1 = """╭━━━━━━━━━━━━━━━━━━━━╮
⚙️ 𝐔𝐒𝐄𝐑𝐁𝐎𝐓 𝐌𝐄𝐍𝐔 (𝟏/𝟐) ⚙️
╰━━━━━━━━━━━━━━━━━━━━╯

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🔹 𝐁𝐀𝐒𝐈𝐂 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .help — Show menu
▸ .alive — Check status
▸ .profile — Your profile
▸ .id — Get ID
▸ .info — Get user info

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🤖 𝐀𝐈 𝐀𝐒𝐒𝐈𝐒𝐓𝐀𝐍𝐓
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .ai <question> — Ask anything to AI

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 📥 𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃𝐄𝐑
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .save — Save media silently (reply to media)
▸ .saveaudio — Extract audio from video (reply)
▸ .savemsg — Save text silently (reply to message)
▸ .savechannel <link> — Save channel post (no admin needed)
▸ .play <song> — YouTube search

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 👤 𝐏𝐑𝐎𝐅𝐈𝐋𝐄 𝐒𝐘𝐒𝐓𝐄𝐌
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .clone @user — Add 1 pic, others safe
▸ .reclone — Remove 1 pic, others safe
▸ .cloneall @user — Full clone (hide all, add 1)
▸ .recloneall — Full restore (all pics back)
▸ .ghost — Add 1 ghost pic, others safe
▸ .unghost — Remove 1 ghost pic, others safe
▸ .ghostall — Hide all pics, add 1
▸ .unghostall — Restore all pics
▸ .saveprofile — Save original profile (backup)
▸ .restoreprofile — Restore saved original

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🛡️ 𝐀𝐃𝐌𝐈𝐍 𝐂𝐎𝐌𝐌𝐀𝐍𝐃𝐒
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .admins — Group admin list
▸ .addadmin reply/@id — Add admin (full rights)
▸ .deladmin reply/@id — Remove admin

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🔇 𝐌𝐔𝐓𝐄 & 𝐑𝐄𝐒𝐓𝐑𝐈𝐂𝐓
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .mute reply/@id — Local mute
▸ .unmute reply/@id — Unmute
▸ .gmute reply/@id — Global mute
▸ .gunmute reply/@id — Global unmute
▸ .lock — Lock group
▸ .unlock — Unlock group
▸ .ban reply/@id — Ban user
▸ .unban reply/@id — Unban user

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 👥 𝐆𝐑𝐎𝐔𝐏 𝐌𝐎𝐃
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .tagall <text> — Tag all members
▸ .tagall2 <text> — Tag members one by one
▸ .stoptag — Stop tagall/tagall2
▸ .raid <count> — Raid (with emojis)
▸ .purge — Delete all messages below replied
▸ .throw reply/@id — Kick user
▸ .stop — Stop all raids, spam & fastgc

✨ ᴘᴏᴡᴇʀᴇᴅ ʙʏ @shahnawazzz_1 ✨"""

        part2 = """╭━━━━━━━━━━━━━━━━━━━━╮
⚙️ 𝐔𝐒𝐄𝐑𝐁𝐎𝐓 𝐌𝐄𝐍𝐔 (𝟐/𝟐) ⚙️
╰━━━━━━━━━━━━━━━━━━━━╯

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🤖 𝐀𝐈 𝐑𝐄𝐏𝐋𝐘
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .aireply on/off — AI auto-reply ON/OFF
▸ .aireply approve <reply/@id> — AI reply se exclude
▸ .aireply remove <reply/@id> — Exclude hatado
▸ .aireply list — AI approved users
▸ .aireply status — Status check

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ ⚙️ 𝐀𝐔𝐓𝐎 𝐒𝐘𝐒𝐓𝐄𝐌
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .autoreact on/off — Auto-reaction in DMs
▸ .autoreactg on/off — Auto-reaction in groups
▸ .autoreply on/off — Auto reply
▸ .autoreply set <text> — Set custom reply
▸ .autoreply reset — Reset first-message list
▸ .autoreply approve reply/@id — Approve user
▸ .autoreply remove reply/@id — Remove approved user
▸ .autoreply list — List approved users
▸ .pmguard on/off — PM Guard (Default)
▸ .pmguard set <2/3/4/6> — Custom PM Guard
▸ .pmguard set off — Custom PM Guard OFF
▸ .pmguard status — Show current settings
▸ .approve reply/@user/id — Approve user (PM Guard)
▸ .disapprove reply/@user/id — Disapprove user
▸ .approvelist — List approved users
▸ .blacklist reply/@id — Blacklist user
▸ .removeblacklist reply/@id — Remove from blacklist

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🎯 𝐑𝐀𝐈𝐃 𝐄𝐍𝐆𝐈𝐍𝐄
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .reply reply/@id — Reply raid
▸ .sreply — Stop reply raid
▸ .rr reply/@id — 🤣 Raid
▸ .srr — Stop RR
▸ .flag reply/@id — Flag raid
▸ .sflag — Stop flag
▸ .hrr reply/@id — Heart raid
▸ .shrr — Stop heart

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ ⚡ 𝐒𝐏𝐀𝐌 & 𝐅𝐀𝐒𝐓
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .spray <count> <text> — Silent spam (only text)
▸ .dspray — Stop spam
▸ .fastgc set <text> — Start fast group title spam
▸ .fastgc stop — Stop fastgc

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🎲 𝐅𝐔𝐍
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .ping — Check latency
▸ .flip — Coin flip
▸ .dice — Roll dice

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🕵️ 𝐒𝐓𝐀𝐋𝐊 𝐓𝐎𝐎𝐋𝐒
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .stalk — Deep profile info (reply)
▸ .allpfp — All profile pics
▸ .lastmsg <n> — Last N messages from user

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🎵 𝐌𝐔𝐒𝐈𝐂
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .lyrics <song> — Song lyrics
▸ .songinfo <song> — Detailed song info
▸ .spotify <query> — Search tracks
▸ .trending — Global trending songs
▸ .ringtone <song> — 30s previews
▸ .playlist <mood> — Mood playlist

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🎨 𝐀𝐈 𝐈𝐌𝐀𝐆𝐄
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .t2i <prompt> — Text to image

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ ✨ 𝐀𝐍𝐈𝐌𝐀𝐓𝐈𝐎𝐍𝐒
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .animate <text> — Typing animation
▸ .spinner [sec] — Multi-stage loader
▸ .loveu — I LOVE YOU box
▸ .loves — I LOVE YOU TOO box
▸ .matrix [sec] — Matrix rain
▸ .hearts <text> — Hearts bubble
▸ .countdown [n] — Countdown (default 5)
▸ .wave <text> — Wave effect

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ ✨ 𝐅𝐀𝐍𝐂𝐘 𝐓𝐄𝐗𝐓
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .hi .hey .bye .welcome
▸ .ok .yes .wow .cool .omg .lol
▸ .love .hate .sad
▸ .good .bad .best
▸ .go .fire .boss
▸ .text <anything> — Custom banner
▸ .banner <word> [font] — Custom font

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🎨 𝐓𝐄𝐗𝐓 𝐀𝐑𝐓
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .gm .gn .hbd .wlc .hello2 .love2
▸ .heyy .gg .flower .gum .howdy .cool2
▸ .hug .hug2 .thnx .purpose
▸ .hii .om .sm .il

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🌍 𝐏𝐔𝐁𝐋𝐈𝐂 𝐅𝐄𝐀𝐓𝐔𝐑𝐄𝐒
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .weather <city> — Weather report
▸ .news [query] — Top India news
▸ .azan <city> — Prayer times

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🖼️ 𝐈𝐌𝐀𝐆𝐄 𝐓𝐎𝐎𝐋𝐒
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .sticker — Image to sticker
▸ .toimg — Sticker to image
▸ .blur — Blur an image
▸ .img2pdf — Image to PDF

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ 🌤️ 𝐅𝐎𝐑𝐄𝐂𝐀𝐒𝐓 & 𝐀𝐐𝐈
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .forecast <city> — 5-day weather forecast
▸ .aqi <city> — Air quality index

╭━━━━━━━━━━━━━━━━━━━━━━╮
┃ ⏰ 𝐒𝐂𝐇𝐄𝐃𝐔𝐋𝐄𝐑
╰━━━━━━━━━━━━━━━━━━━━━━╯
▸ .schedule <date> <time> <target> <msg>
▸ .schedulech <date> <time> <post_link> <msg>
▸ .schedules — Pending list
▸ .cancelschedule <id> — Cancel

✨ ᴘᴏᴡᴇʀᴇᴅ ʙʏ @shahnawazzz_1 ✨"""

        try:
            await event.reply(part1)
            log.info("✅ Help Part 1 sent")
        except Exception as e:
            log.error(f"❌ help part1 failed: {e}")
            try:
                await event.reply("⚠️ Part 1 failed. Sending Part 2 only...")
            except:
                pass
        await asyncio.sleep(0.7)
        try:
            await event.reply(part2)
            log.info("✅ Help Part 2 sent")
        except Exception as e:
            log.error(f"❌ help part2 failed: {e}")

    @cmd('alive', delete_cmd=False)
    async def alive_cmd(event):
        start = db.get("start_time", datetime.now().isoformat())
        try: uptime = datetime.now() - datetime.fromisoformat(start)
        except: uptime = datetime.now() - datetime.now()
        days = uptime.days
        h, r = divmod(uptime.seconds, 3600)
        m, s = divmod(r, 60)
        me = await client.get_me()
        caption = (f"𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ USERBOT IS ALIVE\n✨\n⏳ **UPTIME:** {days}d : {h}h : {m}m : {s}s\n👤 **USER:** {me.first_name or 'User'}\n👑 **OWNER:** @shahnawazzz_1\n")
        IMG = "https://files.catbox.moe/rmj6hg.mp4"
        try: await client.send_file(event.chat_id, IMG, caption=caption, parse_mode='markdown', reply_to=event.message.id)
        except: await event.reply(caption, parse_mode='markdown')

    @cmd('profile', delete_cmd=False)
    async def profile_cmd(event):
        me = await client.get_me()
        await event.reply(f"**👤 Your Profile**\nName: {me.first_name}\nID: `{me.id}`\nUsername: @{me.username or 'None'}", parse_mode='markdown')

    @cmd('id', delete_cmd=False)
    async def id_cmd(event):
        if event.is_reply:
            msg = await event.get_reply_message()
            await event.reply(f"**User ID:** `{msg.sender_id}`", parse_mode='markdown')
        else:
            await event.reply(f"**Chat ID:** `{event.chat_id}`", parse_mode='markdown')

    @cmd('info', delete_cmd=False)
    async def info_cmd(event):
        text = get_args(event.message)
        uid = await resolve_user(client, event.message, text)
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            u = await client.get_entity(uid)
            await event.reply(f"**👤 User Info**\nName: {u.first_name}\nID: `{u.id}`\nUsername: @{u.username or 'None'}", parse_mode='markdown')
        except: await event.reply("❌ Could not get user info.")

    # ═══════════════ AI COMMAND (Groq) - CLEAN OUTPUT ═══════════════
    @cmd('ai', delete_cmd=False)
    async def ai_cmd(event):
        query = get_args(event.message).strip()
        reply_msg = await event.get_reply_message() if event.is_reply else None
        reply_text = ""
        if reply_msg:
            reply_text = (reply_msg.text or "").strip()
            if not reply_text and hasattr(reply_msg, 'message'):
                reply_text = (reply_msg.message or "").strip()
            if not reply_text and hasattr(reply_msg, 'caption'):
                reply_text = (reply_msg.caption or "").strip()

        if not query and not reply_text:
            await event.reply("❌ **Usage:** `.ai <your question>`\n\n**Example:** `.ai What is Python?`\n**Reply method:** kisi message pe reply karke `.ai` bhejo")
            return

        if not GROQ_API_KEY:
            await event.reply("❌ Groq API key not set. Contact owner.")
            return

        try:
            MAX_INPUT_CHARS = 12000
            if len(reply_text) > MAX_INPUT_CHARS:
                reply_text = reply_text[:MAX_INPUT_CHARS] + "\n...[input truncated]"

            if reply_text and query:
                final_prompt = (
                    f"User ne ek message pe reply kiya hai. Us message ka content ye hai:\n\n"
                    f"{reply_text}\n\n"
                    f"Ab user ka sawaal/instruction ye hai:\n{query}\n\n"
                    f"Upar wale message ke context me jawab do."
                )
            elif reply_text and not query:
                final_prompt = (
                    f"Is message ko samjho aur user ko batao ki isme kya likha hai:\n\n"
                    f"{reply_text}\n\n"
                    f"Agar ye kisi aur language me hai to Hinglish me translate karo."
                )
            else:
                final_prompt = query

            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GROQ_API_KEY}"
            }
            payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [
                    {"role": "system", "content": (
                        "You are a helpful assistant. Reply in the same language as the user's message "
                        "(Hindi/Hinglish/English). If user gives you a message to translate or explain, "
                        "do it ACCURATELY and COMPLETELY without skipping, summarizing, or truncating any part. "
                        "Output the full translation even if it is very long.\n\n"
                        "STRICT FORMATTING RULES:\n"
                        "- Use PLAIN TEXT only. Do NOT use markdown symbols like *, **, >, #, _, `, ~, or any other formatting characters.\n"
                        "- Do NOT add headers like 'Hinglish Translation' or 'Translation:' unless user explicitly asks.\n"
                        "- Do NOT use bullet points, quotes (>), or bold/italic.\n"
                        "- Just give clean, plain, natural text as if you're typing a normal message.\n"
                        "- Keep it simple and readable."
                    )},
                    {"role": "user", "content": final_prompt}
                ],
                "max_tokens": 8000,
                "temperature": 0.3
            }
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        await event.reply(f"❌ **Groq API Error:** `{resp.status}`\n`{error_text[:200]}`")
                        return
                    data = await resp.json()

            answer = data["choices"][0]["message"]["content"]

            answer = re.sub(r'^\s*>\s*', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'\*\*(.+?)\*\*', r'\1', answer)
            answer = re.sub(r'\*(.+?)\*', r'\1', answer)
            answer = re.sub(r'__(.+?)__', r'\1', answer)
            answer = re.sub(r'_(.+?)_', r'\1', answer)
            answer = re.sub(r'^#{1,6}\s+', '', answer, flags=re.MULTILINE)
            answer = re.sub(r'`(.+?)`', r'\1', answer)
            answer = re.sub(r'~~(.+?)~~', r'\1', answer)
            answer = answer.strip()

            reply_target = event.reply_to_msg_id if event.is_reply else event.message.id
            def split_text(text, chunk_size=3900):
                chunks = []
                while len(text) > chunk_size:
                    split_at = text.rfind('\n', 0, chunk_size)
                    if split_at == -1 or split_at < chunk_size * 0.5:
                        split_at = chunk_size
                    chunks.append(text[:split_at])
                    text = text[split_at:].lstrip('\n')
                if text:
                    chunks.append(text)
                return chunks

            chunks = split_text(answer, 3900)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await client.send_message(event.chat_id, chunk, reply_to=reply_target)
                else:
                    await client.send_message(event.chat_id, chunk)
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.6)

        except asyncio.TimeoutError:
            await event.reply("❌ **Timeout** — Groq took too long. Try again.")
        except Exception as e:
            log.error(f"Groq error: {e}")
            await event.reply(f"❌ **Error:** `{str(e)[:200]}`")

    # ═══════════════ AI REPLY (NATURAL) ═══════════════
    @cmd('aireply')
    async def aireply_cmd(event):
        text = get_args(event.message)
        parts = text.split(' ', 1)
        if not parts or not parts[0]:
            await event.reply(
                "**🤖 AI Reply**\n"
                "━━━━━━━━━━━━━━━━━━━━━\n\n"
                "`.aireply on` — AI auto-reply ON\n"
                "`.aireply off` — AI auto-reply OFF\n"
                "`.aireply status` — Status check\n"
                "`.aireply approve <reply/@id>` — AI reply se exclude\n"
                "`.aireply remove <reply/@id>` — Exclude hatado\n"
                "`.aireply list` — Approved users",
                parse_mode='markdown'
            )
            return
        action = parts[0].lower()

        if action == 'status':
            ar = db.get('ai_reply', {})
            await event.reply(
                f"⚙️ **AI Reply Status**\n\n"
                f"Enabled: `{ar.get('enabled', False)}`\n"
                f"Approved users: `{len(ar.get('approved_users', []))}`\n"
                f"Model: `openai/gpt-oss-120b`",
                parse_mode='markdown'
            )
            return

        if action in ['approve', 'remove', 'list']:
            if action == 'list':
                approved = db['ai_reply'].get('approved_users', [])
                if not approved:
                    await event.reply("📭 Koi approved user nahi hai."); return
                text_out = f"✅ **AI Reply Approved Users ({len(approved)})**\n" + "═" * 25 + "\n\n"
                for i, uid in enumerate(approved, 1):
                    name = "Unknown"
                    username = "Unknown"
                    try:
                        u = await client.get_entity(int(uid))
                        name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
                        username = f"@{u.username}" if u.username else "None"
                    except: pass
                    text_out += f"{i}. 👤 `{name}`\n   🆔 `{uid}`\n   📛 {username}\n\n"
                await event.reply(text_out, parse_mode='markdown')
                return

            target_id = None
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                target_id = reply_msg.sender_id
            else:
                arg = parts[1] if len(parts) > 1 else ""
                if arg.startswith("@"):
                    try: u = await client.get_entity(arg); target_id = u.id
                    except: await event.reply(f"❌ `{arg}` not found."); return
                else:
                    try: target_id = int(arg)
                    except: await event.reply("❌ Invalid user."); return
            if target_id == db.get("owner_id", owner_id):
                await event.reply("❌ Aap khud ko approve nahi kar sakte."); return
            approved = db['ai_reply'].get('approved_users', [])
            name = "Unknown"
            try:
                u = await client.get_entity(int(target_id))
                name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
            except: pass
            if action == 'approve':
                if target_id in approved:
                    await event.reply(f"✅ `{name}` already approved hai."); return
                approved.append(target_id)
                db['ai_reply']['approved_users'] = approved
                await save_db()
                await event.reply(f"✅ **User Approved for AI Reply**\n👤 `{name}`", parse_mode='markdown')
            elif action == 'remove':
                if target_id not in approved:
                    await event.reply(f"⚠️ `{name}` approved list me nahi hai."); return
                approved.remove(target_id)
                db['ai_reply']['approved_users'] = approved
                await save_db()
                await event.reply(f"✅ **User Removed from AI Reply Approve List**\n👤 `{name}`", parse_mode='markdown')
            return

        if action == 'on':
            if not GROQ_API_KEY:
                await event.reply("❌ Groq API key not set. Contact owner."); return
            if db.get('auto_reply', {}).get('enabled', False):
                await event.reply(
                    "⚠️ **Autoreply ON hai**\n\n"
                    "Pehle `.autoreply off` karo.",
                    parse_mode='markdown'
                )
                return
            db['ai_reply']['enabled'] = True
            await save_db()
            await event.reply("✅ **AI Reply ON**", parse_mode='markdown')
        elif action == 'off':
            db['ai_reply']['enabled'] = False
            await save_db()
            await event.reply("✅ **AI Reply OFF**")
        else:
            await event.reply("**Usage:**\n`.aireply on/off`\n`.aireply status`\n`.aireply approve <reply/@id>`\n`.aireply remove <reply/@id>`\n`.aireply list`", parse_mode='markdown')

    # ═══════════════ SAVE MEDIA ═══════════════
    @cmd('save', delete_cmd=False)
    async def save_cmd(event):
        if not is_owner(event.sender_id): return
        try: await event.delete()
        except: pass
        if not event.is_reply: return
        reply = await event.get_reply_message()
        if not reply or not reply.media: return
        media_list = []
        if getattr(reply, 'grouped_id', None):
            try:
                all_msgs = await client.get_messages(reply.chat_id, min_id=reply.id - 20, max_id=reply.id + 20, limit=50)
                for m in all_msgs:
                    if getattr(m, 'grouped_id', None) == reply.grouped_id and m.media: media_list.append(m)
                media_list.sort(key=lambda x: x.id)
                if not media_list: media_list = [reply]
            except: media_list = [reply]
        else:
            media_list = [reply]
        sender = await reply.get_sender()
        sender_name = "Unknown"
        if sender:
            sender_name = ((getattr(sender, 'first_name', '') or "") + (" " + getattr(sender, 'last_name', '') if getattr(sender, 'last_name', None) else "")).strip() or "Unknown"
        sender_username = f"@{sender.username}" if sender and getattr(sender, 'username', None) else "None"
        total = len(media_list)
        for idx, media in enumerate(media_list, 1):
            file_path = None
            try:
                file_path = await media.download_media(file=str(TEMP_DIR))
                if not file_path or not os.path.exists(file_path): continue
                filesize = os.path.getsize(file_path)
                filesize_mb = filesize / (1024 * 1024)
                if filesize > 2000 * 1024 * 1024:
                    try: os.remove(file_path)
                    except: pass
                    continue
                original_caption = media.text or ""
                if total > 1:
                    caption = (f"👤 **From:** {sender_name}\n📛 **Username:** {sender_username}\n📦 **Size:** {filesize_mb:.2f} MB\n")
                else:
                    caption = (f"👤 **From:** {sender_name}\n📛 **Username:** {sender_username}\n📦 **Size:** {filesize_mb:.2f} MB\n")
                if original_caption: caption += f"\n📝 **Caption:** {original_caption[:300]}"
                await client.send_file("me", file_path, caption=caption, parse_mode='markdown', supports_streaming=True)
                try: os.remove(file_path)
                except: pass
                if total > 1: await asyncio.sleep(0.5)
            except Exception as e:
                log.error(f"Save error (item {idx}): {e}")
                if file_path and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except: pass

    # ═══════════════ SAVE AUDIO (Extract from Video) ═══════════════
    @cmd('saveaudio', delete_cmd=False)
    async def saveaudio_cmd(event):
        if not is_owner(event.sender_id): return
        try: await event.delete()
        except: pass
        if not event.is_reply: return
        reply = await event.get_reply_message()
        if not reply or not reply.media: return

        is_video = False
        if getattr(reply, 'video', None): is_video = True
        elif getattr(reply, 'document', None):
            mime = getattr(reply.document, 'mime_type', '') or ''
            if mime.startswith('video/'): is_video = True

        is_audio = False
        if getattr(reply, 'audio', None):
            is_audio = True
        elif getattr(reply, 'voice', None):
            is_audio = True
        elif getattr(reply, 'document', None):
            mime = getattr(reply.document, 'mime_type', '') or ''
            if mime.startswith('audio/'):
                is_audio = True

        sender = await reply.get_sender()
        sender_name = "Unknown"
        if sender:
            sender_name = ((getattr(sender, 'first_name', '') or "") + (" " + getattr(sender, 'last_name', '') if getattr(sender, 'last_name', None) else "")).strip() or "Unknown"
        sender_username = f"@{sender.username}" if sender and getattr(sender, 'username', None) else "None"

        if is_audio and not is_video:
            file_path = None
            try:
                file_path = await reply.download_media(file=str(TEMP_DIR))
                if not file_path or not os.path.exists(file_path): return
                filesize_mb = os.path.getsize(file_path) / (1024 * 1024)
                original_caption = reply.text or ""
                caption = (f"🎵 **Saved Audio**\n\n👤 **From:** {sender_name}\n📛 **Username:** {sender_username}\n📦 **Size:** {filesize_mb:.2f} MB\n")
                if original_caption: caption += f"\n📝 **Caption:** {original_caption[:300]}"
                await client.send_file("me", file_path, caption=caption, parse_mode='markdown')
            except Exception as e:
                log.error(f"saveaudio (already audio) error: {e}")
            finally:
                if file_path and os.path.exists(file_path):
                    try: os.remove(file_path)
                    except: pass
            return

        if not is_video: return

        file_path = None
        audio_path = None
        try:
            file_path = await reply.download_media(file=str(TEMP_DIR))
            if not file_path or not os.path.exists(file_path): return
            filesize = os.path.getsize(file_path)
            if filesize > 2000 * 1024 * 1024: return

            audio_path = str(TEMP_DIR / f"audio_{int(time.time())}_{random.randint(1,9999)}.mp3")
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", file_path,
                "-vn", "-acodec", "libmp3lame",
                "-ab", "192k", "-ar", "44100",
                "-y", audio_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL
            )
            await process.wait()

            if not os.path.exists(audio_path) or os.path.getsize(audio_path) < 1000: return

            audio_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
            original_caption = reply.text or ""
            caption = (f"🎵 **Saved Audio (Extracted)**\n\n👤 **From:** {sender_name}\n📛 **Username:** {sender_username}\n📦 **Size:** {audio_size_mb:.2f} MB\n")
            if original_caption: caption += f"\n📝 **Caption:** {original_caption[:300]}"

            await client.send_file("me", audio_path, caption=caption, parse_mode='markdown')
        except Exception as e:
            log.error(f"saveaudio error: {e}")
        finally:
            if file_path and os.path.exists(file_path):
                try: os.remove(file_path)
                except: pass
            if audio_path and os.path.exists(audio_path):
                try: os.remove(audio_path)
                except: pass

    @cmd('savemsg', delete_cmd=False)
    async def savemsg_cmd(event):
        if not is_owner(event.sender_id): return
        try: await event.delete()
        except: pass
        if not event.is_reply: return
        reply = await event.get_reply_message()
        if not reply or not reply.text: return
        try:
            sender = await reply.get_sender()
            sender_name = "Unknown"
            if sender:
                sender_name = ((getattr(sender, 'first_name', '') or "") + (" " + getattr(sender, 'last_name', '') if getattr(sender, 'last_name', None) else "")).strip() or "Unknown"
            sender_username = f"@{sender.username}" if sender and getattr(sender, 'username', None) else "None"
            original_text = reply.text or ""
            saved_text = (f"👤 **From:** {sender_name}\n📛 **Username:** {sender_username}\n\n📝 **Message:**\n{original_text}")
            await client.send_message("me", saved_text, parse_mode='markdown')
        except Exception as e:
            log.error(f"SaveMsg error: {e}")

    # ═══════════════ SAVE CHANNEL (Updated with Album + Sender Info) ═══════════════
    @cmd('savechannel', delete_cmd=False)
    async def savechannel_cmd(event):
        if not is_owner(event.sender_id): return
        args = get_args(event.message).strip()
        
        async def save_single_msg(msg, chat_title, link_str, index=None, total=None):
            try:
                is_link_preview = isinstance(msg.media, MessageMediaWebPage) if msg.media else False
                is_unsupported = isinstance(msg.media, MessageMediaUnsupported) if msg.media else False
                
                sender_name = "Unknown"
                sender_username = "None"
                try:
                    sender = await msg.get_sender()
                    if sender:
                        sender_name = ((getattr(sender, 'first_name', '') or "") + 
                                       (" " + getattr(sender, 'last_name', '') if getattr(sender, 'last_name', None) else "")).strip() or "Unknown"
                        sender_username = f"@{sender.username}" if getattr(sender, 'username', None) else "None"
                except Exception as e:
                    log.warning(f"savechannel sender fetch failed: {e}")
                
                if msg.media and not is_link_preview and not is_unsupported:
                    file_path = await msg.download_media(file=str(TEMP_DIR))
                    if file_path and os.path.exists(file_path):
                        caption = (f"📢 **From:** {chat_title}\n"
                                   f"👤 **Sender:** {sender_name}\n"
                                   f"📛 **Username:** {sender_username}")
                        if msg.text:
                            caption += f"\n\n📝 **Caption:** {msg.text[:500]}"
                        caption += f"\n\n🔗 **Link:** {link_str}"
                        await client.send_file("me", file_path, caption=caption,
                                               parse_mode='markdown', supports_streaming=True)
                        try: os.remove(file_path)
                        except: pass
                        return True
                elif msg.text:
                    saved_text = (f"📢 **From:** {chat_title}\n"
                                  f"👤 **Sender:** {sender_name}\n"
                                  f"📛 **Username:** {sender_username}\n"
                                  f"📝 **Message:**\n{msg.text}\n\n"
                                  f"🔗 **Link:** {link_str}")
                    await client.send_message("me", saved_text, parse_mode='markdown')
                    return True
            except Exception as e:
                log.error(f"save_single_msg error: {e}")
            return False

        async def save_grouped_album(msg, chat_title, base_link):
            grouped_id = getattr(msg, 'grouped_id', None)
            if not grouped_id:
                return None
            
            try:
                min_id = max(1, msg.id - 15)
                max_id = msg.id + 15
                
                all_msgs = await client.get_messages(
                    msg.chat_id if hasattr(msg, 'chat_id') else msg.peer_id,
                    min_id=min_id,
                    max_id=max_id,
                    limit=50
                )
                
                album_msgs = [m for m in all_msgs if getattr(m, 'grouped_id', None) == grouped_id]
                album_msgs.sort(key=lambda x: x.id)
                
                if not album_msgs:
                    return None
                
                total = len(album_msgs)
                log.info(f"📦 Album detected: {total} media items (grouped_id={grouped_id})")
                
                success_count = 0
                for idx, alb_msg in enumerate(album_msgs, 1):
                    link_str = f"{base_link.rsplit('/', 1)[0]}/{alb_msg.id}"
                    ok = await save_single_msg(alb_msg, chat_title, link_str, index=idx, total=total)
                    if ok:
                        success_count += 1
                    await asyncio.sleep(0.5)
                
                return (success_count, total)
            except Exception as e:
                log.error(f"save_grouped_album error: {e}")
                return None

        if event.is_reply:
            reply = await event.get_reply_message()
            if reply:
                try:
                    chat = await reply.get_chat()
                    chat_title = getattr(chat, 'title', 'Unknown')
                    chat_username = getattr(chat, 'username', None)
                    base_link = f"https://t.me/{chat_username}/{reply.id}" if chat_username else f"Chat ID: {reply.chat_id}"
                    
                    if getattr(reply, 'grouped_id', None):
                        result = await save_grouped_album(reply, chat_title, base_link)
                        if result:
                            await event.reply("✅ **Album Saved!**")
                            return
                    
                    ok = await save_single_msg(reply, chat_title, base_link)
                    if ok:
                        await event.reply("✅ **Saved to Saved Messages!**")
                        return
                except Exception as e:
                    log.error(f"savechannel reply error: {e}")
                return

        if not args:
            await event.reply(
                "❌ **Usage:**\n"
                "**1. Reply:** message pe reply karo → `.savechannel`\n"
                "**2. Link:** `.savechannel https://t.me/channelname/123`\n"
                "`.savechannel -1001234567890 123`"
            )
            return

        try:
            target_chat = None
            target_msg_id = None
            
            link_match = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', args)
            if link_match:
                chat_part = link_match.group(1)
                target_msg_id = int(link_match.group(2))
                try:
                    target_chat = int(chat_part) if (chat_part.startswith('-100') or chat_part.isdigit()) else chat_part
                except:
                    target_chat = chat_part
                if chat_part.isdigit() and not chat_part.startswith('-100'):
                    target_chat = int(f"-100{chat_part}")
            else:
                parts = args.split()
                if len(parts) >= 2:
                    try:
                        target_chat = int(parts[0])
                        target_msg_id = int(parts[1])
                    except:
                        await event.reply("❌ Invalid format. Use: `.savechannel <link>` or `.savechannel <chat_id> <msg_id>`")
                        return
                else:
                    await event.reply("❌ Invalid format. Use: `.savechannel <link>` or `.savechannel <chat_id> <msg_id>`")
                    return
            
            messages = await client.get_messages(target_chat, ids=target_msg_id)
            if not messages:
                await event.reply("❌ Message not found."); return
            msg = messages[0] if isinstance(messages, list) else messages
            if not msg:
                await event.reply("❌ Message not found."); return
            
            chat = await msg.get_chat() if hasattr(msg, 'get_chat') else None
            chat_title = getattr(chat, 'title', 'Channel') if chat else 'Channel'
            
            if getattr(msg, 'grouped_id', None):
                status_msg = await event.reply("📦 **Album detected!** Save kar raha hun...")
                result = await save_grouped_album(msg, chat_title, args)
                if result:
                    await status_msg.edit("✅ **Album Saved!**")
                    return
                else:
                    await status_msg.edit("⚠️ Album fetch failed, trying single media...")
            
            ok = await save_single_msg(msg, chat_title, args)
            if ok:
                await event.reply("✅ **Saved!**")
            else:
                await event.reply("❌ Nothing to save (no media/text).")
                
        except Exception as e:
            log.error(f"savechannel error: {e}")
            await event.reply(f"❌ Error: `{str(e)[:200]}`")

    # ═══════════════ CLONE ═══════════════
    @cmd('clone')
    async def clone_cmd(event):
        if not is_owner(event.sender_id): return
        text = get_args(event.message)
        target_id = await resolve_user(client, event.message, text)
        if not target_id:
            await event.reply("❌ Target not found."); return
        me = await client.get_me()
        db['clone_data']['orig_first_name'] = me.first_name or ''
        db['clone_data']['orig_last_name'] = me.last_name or ''
        try:
            full_me = await client(functions.users.GetFullUserRequest(id=me.id))
            db['clone_data']['orig_bio'] = full_me.full_user.about or ''
        except: db['clone_data']['orig_bio'] = ''
        await save_db()
        await asyncio.sleep(0.3)
        try:
            target = await client.get_entity(target_id)
            await client(UpdateProfileRequest(first_name=getattr(target, 'first_name', '') or '', last_name=getattr(target, 'last_name', '') or ''))
            try:
                full_target = await client(functions.users.GetFullUserRequest(id=target_id))
                await client(functions.account.UpdateProfileRequest(about=getattr(full_target.full_user, 'about', '') or ''))
            except: pass
            try:
                target_photos = await client(functions.photos.GetUserPhotosRequest(user_id=target_id, offset=0, max_id=0, limit=1))
                if target_photos.photos:
                    photo_path = str(TEMP_DIR / "clone_temp.jpg")
                    await client.download_media(target_photos.photos[0], photo_path)
                    if os.path.exists(photo_path):
                        uploaded = await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(photo_path)))
                        db['clone_data']['clone_photo_id'] = uploaded.photo.id
                        await save_db()
                        os.remove(photo_path)
            except: pass
            await event.reply("✅ **Profile Cloned!**")
        except Exception as e:
            await event.reply(f"❌ Clone failed: {str(e)[:100]}")

    @cmd('reclone')
    async def reclone_cmd(event):
        if not is_owner(event.sender_id): return
        cd = db.get("clone_data", {})
        if not cd.get('orig_first_name'):
            cd = await get_fresh_data('clone_data') or {}
        if not cd.get('orig_first_name'):
            await event.reply("❌ No clone data."); return
        me = await client.get_me()
        try:
            await client(UpdateProfileRequest(first_name=cd.get('orig_first_name', ''), last_name=cd.get('orig_last_name', '')))
            await client(functions.account.UpdateProfileRequest(about=cd.get('orig_bio', '')))
        except: pass
        try:
            clone_photo_id = cd.get('clone_photo_id')
            if clone_photo_id:
                photos_obj = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
                for p in photos_obj.photos:
                    if p.id == clone_photo_id:
                        await client(functions.photos.DeletePhotosRequest(id=[p]))
                        break
        except: pass
        db['clone_data'] = {}
        await save_db()
        await event.reply("✅ **Profile Restored!**")

    @cmd('cloneall')
    async def cloneall_cmd(event):
        if not is_owner(event.sender_id): return
        text = get_args(event.message)
        target_id = await resolve_user(client, event.message, text)
        if not target_id:
            await event.reply("❌ Target not found."); return
        me = await client.get_me()
        db['cloneall_data'] = {'orig_first_name': me.first_name or '', 'orig_last_name': me.last_name or '', 'orig_bio': '', 'orig_photo_paths': []}
        try:
            full_me = await client(functions.users.GetFullUserRequest(id=me.id))
            db['cloneall_data']['orig_bio'] = full_me.full_user.about or ''
        except: pass
        try:
            photo_dir = DATA_DIR / f"user_{owner_id}" / "cloneall_photos"
            shutil.rmtree(photo_dir, ignore_errors=True)
            photo_dir.mkdir(parents=True, exist_ok=True)
            all_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            photos_list = list(all_photos.photos)
            photos_list.reverse()
            saved_paths = []
            for i, photo in enumerate(photos_list):
                photo_path = str(photo_dir / f"orig_{i:03d}.jpg")
                if await client.download_media(photo, photo_path): saved_paths.append(photo_path)
            db['cloneall_data']['orig_photo_paths'] = saved_paths
        except: pass
        await save_db()
        await asyncio.sleep(0.3)
        try:
            target = await client.get_entity(target_id)
            await client(UpdateProfileRequest(first_name=getattr(target, 'first_name', '') or '', last_name=getattr(target, 'last_name', '') or ''))
            try:
                full_target = await client(functions.users.GetFullUserRequest(id=target_id))
                await client(functions.account.UpdateProfileRequest(about=getattr(full_target.full_user, 'about', '') or ''))
            except: pass
            try:
                current_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
                if current_photos.photos: await client(DeletePhotosRequest(id=current_photos.photos))
            except: pass
            try:
                target_photos = await client(functions.photos.GetUserPhotosRequest(user_id=target_id, offset=0, max_id=0, limit=1))
                if target_photos.photos:
                    photo_path = str(TEMP_DIR / "cloneall_temp.jpg")
                    await client.download_media(target_photos.photos[0], photo_path)
                    if os.path.exists(photo_path):
                        await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(photo_path)))
                        os.remove(photo_path)
            except: pass
            await event.reply("✅ **Full Clone Done!**")
        except Exception as e:
            await event.reply(f"❌ Clone failed: {str(e)[:100]}")

    @cmd('recloneall')
    async def recloneall_cmd(event):
        if not is_owner(event.sender_id): return
        cd = db.get("cloneall_data", {})
        if not cd.get('orig_first_name'):
            cd = await get_fresh_data('cloneall_data') or {}
        if not cd.get('orig_first_name'):
            await event.reply("❌ No cloneall data."); return
        me = await client.get_me()
        try:
            await client(UpdateProfileRequest(first_name=cd.get('orig_first_name', ''), last_name=cd.get('orig_last_name', '')))
            await client(functions.account.UpdateProfileRequest(about=cd.get('orig_bio', '') or ''))
        except: pass
        try:
            current_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            if current_photos.photos: await client(DeletePhotosRequest(id=current_photos.photos))
        except: pass
        orig_paths = cd.get('orig_photo_paths', [])
        orig_paths.sort()
        for photo_path in orig_paths:
            if os.path.exists(photo_path):
                try:
                    await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(photo_path)))
                    await asyncio.sleep(0.5)
                except: pass
        db['cloneall_data'] = {}
        await save_db()
        await event.reply("✅ **Full Profile Restored!**")

    @cmd('saveprofile')
    async def saveprofile_cmd(event):
        if not is_owner(event.sender_id): return
        me = await client.get_me()
        sp = {'first_name': me.first_name or '', 'last_name': me.last_name or '', 'bio': ''}
        try:
            full_me = await client(functions.users.GetFullUserRequest(id=me.id))
            sp['bio'] = full_me.full_user.about or ''
        except: pass
        try:
            current_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            sp['photo_ids'] = [p.id for p in current_photos.photos]
            sp['photo_count'] = len(sp['photo_ids'])
        except: sp['photo_ids'] = []; sp['photo_count'] = 0
        try:
            photo_dir = DATA_DIR / f"user_{owner_id}" / "saved_profile_photos"
            shutil.rmtree(photo_dir, ignore_errors=True)
            photo_dir.mkdir(parents=True, exist_ok=True)
            all_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            photos_list = list(all_photos.photos)
            photos_list.reverse()
            saved_paths = []
            for i, photo in enumerate(photos_list):
                photo_path = str(photo_dir / f"saved_{i:03d}.jpg")
                if await client.download_media(photo, photo_path): saved_paths.append(photo_path)
            sp['photo_paths'] = saved_paths
            sp['saved_at'] = datetime.now().strftime("%d %b %Y, %I:%M %p")
        except: sp['photo_paths'] = []
        db['saved_profile'] = sp
        await save_db()
        await event.reply("💾 **Original Profile Saved!**")

    @cmd('restoreprofile')
    async def restoreprofile_cmd(event):
        if not is_owner(event.sender_id): return
        sp = db.get('saved_profile', {})
        if not sp or not sp.get('first_name'):
            sp = await get_fresh_data('saved_profile') or {}
        if not sp or not sp.get('first_name'):
            await event.reply("❌ Koi saved profile nahi hai!\n\nApne original account par aakar pehle `.saveprofile` command use karein.")
            return
        saved_count = sp.get('photo_count', 0)
        if saved_count == 0:
            await event.reply("⚠️ Saved profile me photo nahi thi!"); return
        me = await client.get_me()
        msg = await event.reply("⏳ Restoring...")
        try:
            await client(UpdateProfileRequest(first_name=sp.get('first_name', ''), last_name=sp.get('last_name', '')))
            try: await client(functions.account.UpdateProfileRequest(about=sp.get('bio', '') or ''))
            except: pass
            try:
                current_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
                current_count = len(current_photos.photos)
                if current_count > saved_count:
                    extra = current_count - saved_count
                    photos_sorted = sorted(list(current_photos.photos), key=lambda p: p.id, reverse=True)
                    await client(functions.photos.DeletePhotosRequest(id=photos_sorted[:extra]))
                elif current_count < saved_count:
                    photo_dir = DATA_DIR / f"user_{owner_id}" / "saved_profile_photos"
                    if photo_dir.exists():
                        saved_files = sorted(photo_dir.glob("saved_*.jpg"))
                        to_upload_count = saved_count - current_count
                        if len(saved_files) >= to_upload_count:
                            for f in saved_files[-to_upload_count:]:
                                try:
                                    await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(str(f))))
                                    await asyncio.sleep(0.7)
                                except: break
                await msg.edit("✅ **Original Profile Restored!**")
            except: await msg.edit("✅ Restored!")
        except Exception as e:
            await msg.edit(f"❌ Restore failed: `{str(e)[:200]}`")

    @cmd('ghost')
    async def ghost_cmd(event):
        if not is_owner(event.sender_id): return
        if db.get("ghost_on"):
            await event.reply("👻 Ghost already active!"); return
        me = await client.get_me()
        db['ghost_data'] = {'first_name': me.first_name or '', 'last_name': me.last_name or '', 'bio': ''}
        try:
            full_me = await client(functions.users.GetFullUserRequest(id=me.id))
            db['ghost_data']['bio'] = full_me.full_user.about or ''
        except: db['ghost_data']['bio'] = ''
        await save_db()
        await asyncio.sleep(0.3)
        try:
            await client(UpdateProfileRequest(first_name="Deleted", last_name="Account"))
            await client(functions.account.UpdateProfileRequest(about="This account has been deleted"))
            ghost_img_path = await get_ghost_image_path()
            if ghost_img_path and os.path.exists(ghost_img_path):
                uploaded = await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(ghost_img_path)))
                db['ghost_data']['ghost_photo_id'] = uploaded.photo.id
                await save_db()
            db['ghost_on'] = True
            await save_db()
            await event.reply("👻 **Ghost Mode Activated!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:100]}")

    @cmd('unghost')
    async def unghost_cmd(event):
        if not is_owner(event.sender_id): return
        if not db.get("ghost_on"):
            await event.reply("❌ Ghost not active!"); return
        gd = db.get("ghost_data", {})
        if not gd.get('first_name'):
            gd = await get_fresh_data('ghost_data') or {}
        me = await client.get_me()
        try:
            await client(UpdateProfileRequest(first_name=gd.get('first_name', ''), last_name=gd.get('last_name', '')))
            await client(functions.account.UpdateProfileRequest(about=gd.get('bio', '') or ''))
        except: pass
        try:
            gid = gd.get('ghost_photo_id')
            if gid:
                photos_obj = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
                for p in photos_obj.photos:
                    if p.id == gid:
                        await client(functions.photos.DeletePhotosRequest(id=[p]))
                        break
        except: pass
        db['ghost_on'] = False
        db['ghost_data'] = {}
        await save_db()
        await event.reply("✅ **Ghost Mode Deactivated!**")

    @cmd('ghostall')
    async def ghostall_cmd(event):
        if not is_owner(event.sender_id): return
        if db.get("ghostall_on"):
            await event.reply("👻 Ghost All already active!"); return
        me = await client.get_me()
        db['ghostall_data'] = {'first_name': me.first_name or '', 'last_name': me.last_name or '', 'bio': '', 'photo_paths': []}
        try:
            full_me = await client(functions.users.GetFullUserRequest(id=me.id))
            db['ghostall_data']['bio'] = full_me.full_user.about or ''
        except: pass
        try:
            photo_dir = DATA_DIR / f"user_{owner_id}" / "ghostall_photos"
            shutil.rmtree(photo_dir, ignore_errors=True)
            photo_dir.mkdir(parents=True, exist_ok=True)
            all_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            photos_list = list(all_photos.photos)
            photos_list.reverse()
            saved_paths = []
            for i, photo in enumerate(photos_list):
                photo_path = str(photo_dir / f"orig_{i:03d}.jpg")
                if await client.download_media(photo, photo_path): saved_paths.append(photo_path)
            db['ghostall_data']['photo_paths'] = saved_paths
        except: pass
        await save_db()
        await asyncio.sleep(0.3)
        try:
            await client(UpdateProfileRequest(first_name="Deleted", last_name="Account"))
            await client(functions.account.UpdateProfileRequest(about="This account has been deleted"))
            try:
                current_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
                if current_photos.photos: await client(DeletePhotosRequest(id=current_photos.photos))
            except: pass
            ghost_img_path = await get_ghost_image_path()
            if ghost_img_path and os.path.exists(ghost_img_path):
                await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(ghost_img_path)))
            db['ghostall_on'] = True
            await save_db()
            await event.reply("👻 **Ghost Mode Activated!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:100]}")

    @cmd('unghostall')
    async def unghostall_cmd(event):
        if not is_owner(event.sender_id): return
        if not db.get("ghostall_on"):
            await event.reply("❌ Ghost All not active!"); return
        gd = db.get("ghostall_data", {})
        if not gd.get('first_name'):
            gd = await get_fresh_data('ghostall_data') or {}
        me = await client.get_me()
        try:
            await client(UpdateProfileRequest(first_name=gd.get('first_name', ''), last_name=gd.get('last_name', '')))
            await client(functions.account.UpdateProfileRequest(about=gd.get('bio', '') or ''))
        except: pass
        try:
            current_photos = await client(functions.photos.GetUserPhotosRequest(user_id=me.id, offset=0, max_id=0, limit=100))
            if current_photos.photos: await client(DeletePhotosRequest(id=current_photos.photos))
        except: pass
        orig_paths = gd.get('photo_paths', [])
        orig_paths.sort()
        for photo_path in orig_paths:
            if os.path.exists(photo_path):
                try:
                    await client(functions.photos.UploadProfilePhotoRequest(file=await client.upload_file(photo_path)))
                    await asyncio.sleep(0.5)
                except: pass
        db['ghostall_on'] = False
        db['ghostall_data'] = {}
        await save_db()
        await event.reply("✅ **Ghost Mode Deactivated!**")

    # ═══════════════ ADMIN ═══════════════
    @cmd('admins')
    async def admins_cmd(event):
        if not event.is_group:
            await event.reply(f"👑 Owner: `{db.get('owner_id', owner_id)}`\n**Admins:** {db['admins']}", parse_mode='markdown'); return
        try:
            admins = []
            async for participant in client.iter_participants(event.chat_id, filter=ChannelParticipantsAdmins):
                if participant.bot: continue
                name = ((participant.first_name or "") + (" " + participant.last_name if participant.last_name else "")).strip() or "Unknown"
                username = f"@{participant.username}" if participant.username else "None"
                admins.append(f"👤 **{name}**\n   🆔 `{participant.id}`\n   📛 {username}")
            if not admins:
                await event.reply("❌ Koi admin nahi mila."); return
            text = f"👑 **Group Admins ({len(admins)})**\n" + "═" * 25 + "\n\n"
            text += "\n\n".join(admins)
            await event.reply(text, parse_mode='markdown')
        except Exception as e:
            await event.reply(f"❌ Failed: `{str(e)[:150]}`")

    @cmd('addadmin')
    async def addadmin_cmd(event):
        if not is_owner(event.sender_id): return
        if not event.is_group:
            await event.reply("❌ Group me use karo."); return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            rights = ChatAdminRights(change_info=True, post_messages=True, edit_messages=True, delete_messages=True, ban_users=True, invite_users=True, pin_messages=True, add_admins=False, anonymous=False, manage_call=True, other=True, manage_topics=True)
            await client(EditAdminRequest(channel=event.chat_id, user_id=uid, admin_rights=rights, rank="Admin"))
            if uid not in db['admins']:
                db['admins'].append(uid)
                await save_db()
            await event.reply("✅ **Admin Added with Full Rights!**")
        except Exception as e:
            await event.reply(f"❌ Failed: `{str(e)[:150]}`")

    @cmd('deladmin')
    async def deladmin_cmd(event):
        if not is_owner(event.sender_id): return
        if not event.is_group:
            await event.reply("❌ Group me use karo."); return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            empty_rights = ChatAdminRights(change_info=False, post_messages=False, edit_messages=False, delete_messages=False, ban_users=False, invite_users=False, pin_messages=False, add_admins=False, anonymous=False, manage_call=False, other=False, manage_topics=False)
            await client(EditAdminRequest(channel=event.chat_id, user_id=uid, admin_rights=empty_rights, rank=""))
            if uid in db['admins']:
                db['admins'].remove(uid)
                await save_db()
            await event.reply("✅ **Admin Demoted!**")
        except Exception as e:
            await event.reply(f"❌ Failed: `{str(e)[:150]}`")

    # ═══════════════ MUTE & RESTRICT ═══════════════
    @cmd('mute', delete_cmd=False)
    async def mute_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, send_messages=True)))
            await event.reply("🔇 **User Muted!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:100]}")

    @cmd('unmute', delete_cmd=False)
    async def unmute_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, send_messages=False)))
            await event.reply("🔊 **User Unmuted!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:100]}")

    @cmd('gmute')
    async def gmute_cmd(event):
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        db['gmuted'][str(uid)] = True
        await save_db()
        await event.reply("🌐 **User Globally Muted!**")

    @cmd('gunmute')
    async def gunmute_cmd(event):
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        if str(uid) in db['gmuted']:
            del db['gmuted'][str(uid)]
            await save_db()
        await event.reply("🌐 **User Globally Unmuted!**")

    def get_base_banned_rights():
        return ChatBannedRights(until_date=None, send_messages=False, send_media=False, send_stickers=True, send_gifs=True, send_games=False, send_inline=False, send_polls=False, embed_links=True, send_photos=False, send_videos=False, send_audios=False, send_voices=False, send_docs=False, pin_messages=True, change_info=True, invite_users=False)

    @cmd('lock')
    async def lock_cmd(event):
        if not event.is_group:
            await event.reply("❌ Not a group."); return
        try:
            base = get_base_banned_rights()
            base.send_messages = True; base.send_media = True; base.send_photos = True; base.send_videos = True
            base.send_audios = True; base.send_voices = True; base.send_docs = True; base.send_polls = True
            base.send_games = True; base.send_inline = True
            await client(EditChatDefaultBannedRightsRequest(peer=event.chat_id, banned_rights=base))
            await event.reply("🔒 Group Locked!")
        except Exception as e:
            await event.reply(f"❌ Lock failed: {str(e)[:100]}")

    @cmd('unlock')
    async def unlock_cmd(event):
        if not event.is_group:
            await event.reply("❌ Not a group."); return
        try:
            base = get_base_banned_rights()
            base.send_messages = False; base.send_media = False; base.send_photos = False; base.send_videos = False
            base.send_audios = False; base.send_voices = False; base.send_docs = False; base.send_polls = False
            base.send_games = False; base.send_inline = False
            await client(EditChatDefaultBannedRightsRequest(peer=event.chat_id, banned_rights=base))
            await event.reply("🔓 Group Unlocked!")
        except Exception as e:
            await event.reply(f"❌ Unlock failed: {str(e)[:100]}")

    @cmd('ban')
    async def ban_cmd(event):
        if not event.is_group:
            await event.reply("❌ Group me use karo."); return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=True)))
            await event.reply("🚫 **User Banned!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:150]}")

    @cmd('unban')
    async def unban_cmd(event):
        if not event.is_group:
            await event.reply("❌ Group me use karo."); return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=False)))
            await event.reply("✅ **User Unbanned!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:150]}")

    @cmd('tagall')
    async def tagall_cmd(event):
        if not event.is_group:
            await event.reply("❌ Not a group."); return
        text = get_args(event.message)
        chat_id = event.chat_id
        all_users_list = []
        try:
            async for user in client.iter_participants(chat_id):
                if not user.deleted and not user.bot: all_users_list.append(user)
        except Exception as e:
            await event.reply(f"❌ Failed: {str(e)[:100]}"); return
        if not all_users_list:
            await event.reply("❌ No members found."); return
        db['tagall_running'] = True
        chunk_size = 5
        for i in range(0, len(all_users_list), chunk_size):
            if not db.get('tagall_running', False):
                break
            chunk = all_users_list[i:i + chunk_size]
            mentions = []
            for user in chunk:
                name = user.first_name or "User"
                name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                mentions.append(f'<a href="tg://user?id={user.id}">{name}</a>')
            mention_str = ' '.join(mentions)
            msg = f"{text}\n{mention_str}" if text else mention_str
            await event.respond(msg, parse_mode='html', reply_to=event.reply_to_msg_id if event.is_reply else None)
            await asyncio.sleep(2.5)
        db['tagall_running'] = False

    @cmd('tagall2')
    async def tagall2_cmd(event):
        if not event.is_group:
            await event.reply("❌ Not a group."); return
        text = get_args(event.message)
        if not text:
            await event.reply("❌ Usage: `.tagall2 <text>`"); return
        chat_id = event.chat_id
        all_users_list = []
        try:
            async for user in client.iter_participants(chat_id):
                if not user.deleted and not user.bot: all_users_list.append(user)
        except Exception as e:
            await event.reply(f"❌ Failed: {str(e)[:100]}"); return
        if not all_users_list:
            await event.reply("❌ No members found."); return
        db['tagall_running'] = True
        count = 0
        for user in all_users_list:
            if not db.get('tagall_running', False):
                break
            try:
                name = user.first_name or "User"
                name = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                msg = f'<a href="tg://user?id={user.id}">{name}</a> {text}'
                await event.respond(msg, parse_mode='html', reply_to=event.reply_to_msg_id if event.is_reply else None)
                count += 1
                await asyncio.sleep(1.2)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 2)
            except Exception as e:
                log.warning(f"tagall2 error: {e}")
                continue
        db['tagall_running'] = False
        await event.respond(f"✅ Done! {count} members have been tagged.", reply_to=event.reply_to_msg_id if event.is_reply else None)

    @cmd('stoptag')
    async def stoptag_cmd(event):
        db['tagall_running'] = False
        await event.reply("🛑 **Tagall stopped!**")

    @cmd('raid')
    async def raid_cmd(event):
        if not event.is_reply:
            await event.reply("❌ Reply to a user."); return
        count_str = get_args(event.message)
        try: count = int(count_str) if count_str else 10
        except: count = 10
        count = min(count, 50)
        reply = await event.get_reply_message()
        for i in range(count):
            try:
                msg_template = random.choice(RAID_MESSAGES).replace('{n}', str(i + 1))
                await reply.reply(msg_template)
                await asyncio.sleep(0.3)
            except: break

    @cmd('purge', delete_cmd=True)
    async def purge_cmd(event):
        if not event.is_reply:
            await event.reply("❌ Reply to a message."); return
        reply_msg = await event.get_reply_message()
        if not reply_msg:
            await event.reply("❌ Could not fetch."); return
        chat_id = event.chat_id
        start_id = reply_msg.id
        all_ids = []
        last_id = start_id
        while True:
            try:
                msgs = await client.get_messages(chat_id, min_id=last_id, limit=100, reverse=False)
                if not msgs: break
                ids = [m.id for m in msgs]
                all_ids.extend(ids)
                last_id = min(ids) if ids else start_id
                if len(msgs) < 100: break
            except Exception as e:
                await event.reply(f"❌ Error: {str(e)[:100]}"); return
        if not all_ids:
            await event.reply("✅ No messages below replied."); return
        total_deleted = 0
        for i in range(0, len(all_ids), 100):
            chunk = all_ids[i:i + 100]
            try:
                await client.delete_messages(chat_id, chunk)
                total_deleted += len(chunk)
                await asyncio.sleep(0.5)
            except Exception as e:
                await event.reply(f"❌ Delete failed: {str(e)[:100]}"); return
        await event.reply(f"✅ Purged **{total_deleted}** messages.")

    @cmd('throw')
    async def throw_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        try:
            await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=True)))
            await asyncio.sleep(0.5)
            await client(EditBannedRequest(event.chat_id, uid, ChatBannedRights(until_date=None, view_messages=False)))
            await event.reply("👢 **User Kicked!**")
        except Exception as e:
            await event.reply(f"❌ {str(e)[:100]}")

    @cmd('stop')
    async def stop_cmd(event):
        raid_state['reply'].clear()
        raid_state['rr'].clear()
        raid_state['flag'].clear()
        raid_state['hrr'].clear()
        db['spam_running'] = False
        db['fast_gc_data'].clear()
        db['tagall_running'] = False
        await save_db()
        await event.reply("🛑 **All loops stopped!**")

    # ═══════════════ FAST GC ═══════════════
    async def fast_gc_loop(chat_id):
        while True:
            data = db['fast_gc_data'].get(chat_id)
            if not data or not data.get('enabled', False): break
            template = data.get('template', 'FastGC')
            counter = data.get('counter', 0) + 1
            new_title = f"{template} [{counter}]"
            try:
                entity = await client.get_entity(chat_id)
                if hasattr(entity, 'broadcast') and entity.broadcast:
                    await client(EditTitleRequest(channel=entity, title=new_title))
                else:
                    await client(EditChatTitleRequest(chat_id=chat_id, title=new_title))
                data['counter'] = counter
                await save_db()
            except Exception as e:
                log.warning(f"FastGC error: {e}")
                db['fast_gc_data'][chat_id]['enabled'] = False
                await save_db()
                break
            await asyncio.sleep(1.5)

    @cmd('fastgc')
    async def fastgc_cmd(event):
        args = get_args(event.message).split()
        if not args:
            await event.reply("Usage: `.fastgc set <text>` | `.fastgc stop`"); return
        chat_id = event.chat_id
        action = args[0].lower()
        if action == 'set' and len(args) > 1:
            template = ' '.join(args[1:])
            db['fast_gc_data'][chat_id] = {'enabled': True, 'template': template, 'counter': 0}
            await save_db()
            asyncio.create_task(fast_gc_loop(chat_id))
            await event.reply(f"⚡ **Fast GC started!**")
        elif action == 'stop':
            if chat_id in db['fast_gc_data']:
                db['fast_gc_data'][chat_id]['enabled'] = False
                await save_db()
                await event.reply("🛑 **Fast GC stopped!**")
            else:
                await event.reply("❌ No fast GC running.")
        else:
            await event.reply("Usage: `.fastgc set <text>` | `.fastgc stop`")

    # ═══════════════ AUTO SYSTEM ═══════════════
    @cmd('autoreact')
    async def autoreact_cmd(event):
        arg = get_args(event.message).lower()
        if arg == 'on':
            db['auto_react_dm_enabled'] = True
            await save_db()
            await event.reply("✅ **DM Auto‑React ON**")
        elif arg == 'off':
            db['auto_react_dm_enabled'] = False
            await save_db()
            await event.reply("✅ **DM Auto‑React OFF**")
        else:
            await event.reply("**Usage:** `.autoreact on/off`")

    @cmd('autoreactg')
    async def autoreactg_cmd(event):
        arg = get_args(event.message).lower()
        if arg == 'on':
            db['auto_react_group_enabled'] = True
            await save_db()
            await event.reply("✅ **Group Auto‑React ON**")
        elif arg == 'off':
            db['auto_react_group_enabled'] = False
            await save_db()
            await event.reply("✅ **Group Auto‑React OFF**")
        else:
            await event.reply("**Usage:** `.autoreactg on/off`")

    @cmd('autoreply')
    async def autoreply_cmd(event):
        text = get_args(event.message)
        parts = text.split(' ', 1)
        DEFAULT_REPLY = "The owner is busy with some personal work at the moment😌Please wait until they are back online🥀"
        if not parts or not parts[0]:
            await event.reply("**Usage:**\n`.autoreply on/off`\n`.autoreply set <text>`\n`.autoreply reset`\n`.autoreply status`\n`.autoreply approve <reply/@id>`\n`.autoreply remove <reply/@id>`\n`.autoreply list`", parse_mode='markdown')
            return
        action = parts[0].lower()

        if action == 'status':
            ar = db.get('auto_reply', {})
            await event.reply(
                f"⚙️ **Auto Reply Status**\n\n"
                f"Enabled: `{ar.get('enabled', False)}`\n"
                f"Mode: `{ar.get('mode', 'always')}`\n"
                f"Text: `{ar.get('text', 'N/A')[:100]}`\n"
                f"Replied users: `{len(ar.get('replied_users', []))}`\n"
                f"Approved users: `{len(ar.get('approved_users', []))}`",
                parse_mode='markdown'
            )
            return

        if action in ['approve', 'remove', 'list']:
            if action == 'list':
                approved = db['auto_reply'].get('approved_users', [])
                if not approved:
                    await event.reply("📭 Koi approved user nahi hai."); return
                text_out = f"✅ **Auto-Reply Approved Users ({len(approved)})**\n" + "═" * 25 + "\n\n"
                for i, uid in enumerate(approved, 1):
                    name = "Unknown"
                    username = "Unknown"
                    try:
                        u = await client.get_entity(int(uid))
                        name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
                        username = f"@{u.username}" if u.username else "None"
                    except: pass
                    text_out += f"{i}. 👤 `{name}`\n   🆔 `{uid}`\n   📛 {username}\n\n"
                await event.reply(text_out, parse_mode='markdown')
                return

            target_id = None
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                target_id = reply_msg.sender_id
            else:
                arg = parts[1] if len(parts) > 1 else ""
                if arg.startswith("@"):
                    try: u = await client.get_entity(arg); target_id = u.id
                    except: await event.reply(f"❌ `{arg}` not found."); return
                else:
                    try: target_id = int(arg)
                    except: await event.reply("❌ Invalid user."); return
            if target_id == db.get("owner_id", owner_id):
                await event.reply("❌ Aap khud ko approve nahi kar sakte."); return
            approved = db['auto_reply'].get('approved_users', [])
            name = "Unknown"
            try:
                u = await client.get_entity(int(target_id))
                name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
            except: pass
            if action == 'approve':
                if db.get('ai_reply', {}).get('enabled', False):
                    await event.reply(
                        "⚠️ **AI Reply ON hai**\n\n"
                        "Pehle `.aireply off` karo.",
                        parse_mode='markdown'
                    )
                    return
                if target_id in approved:
                    await event.reply(f"✅ `{name}` already approved hai."); return
                approved.append(target_id)
                db['auto_reply']['approved_users'] = approved
                await save_db()
                await event.reply(f"✅ **User Approved for Auto-Reply**\n👤 `{name}`", parse_mode='markdown')
            elif action == 'remove':
                if db.get('ai_reply', {}).get('enabled', False):
                    await event.reply(
                        "⚠️ **AI Reply ON hai**\n\n"
                        "Pehle `.aireply off` karo.",
                        parse_mode='markdown'
                    )
                    return
                if target_id not in approved:
                    await event.reply(f"⚠️ `{name}` approved list me nahi hai."); return
                approved.remove(target_id)
                db['auto_reply']['approved_users'] = approved
                await save_db()
                await event.reply(f"✅ **User Removed from Auto-Reply Approve List**\n👤 `{name}`", parse_mode='markdown')
            return

        if action == 'on':
            if db.get('ai_reply', {}).get('enabled', False):
                await event.reply(
                    "⚠️ **AI Reply ON hai**\n\n"
                    "Pehle `.aireply off` karo.",
                    parse_mode='markdown'
                )
                return
            db['auto_reply']['enabled'] = True
            db['auto_reply']['mode'] = 'always'
            db['auto_reply']['text'] = DEFAULT_REPLY
            db['auto_reply']['replied_users'] = []
            await save_db()
            await event.reply("✅ Auto Reply ON (Always Mode)")
        elif action == 'off':
            db['auto_reply']['enabled'] = False
            db['auto_reply']['replied_users'] = []
            await save_db()
            await event.reply("✅ Auto Reply OFF")
        elif action == 'set' and len(parts) > 1:
            if db.get('ai_reply', {}).get('enabled', False):
                await event.reply(
                    "⚠️ **AI Reply ON hai**\n\n"
                    "Pehle `.aireply off` karo.",
                    parse_mode='markdown'
                )
                return
            db['auto_reply']['text'] = parts[1]
            db['auto_reply']['enabled'] = True
            db['auto_reply']['mode'] = 'once'
            db['auto_reply']['replied_users'] = []
            await save_db()
            await event.reply("✅ Custom Auto Reply Set (Once Mode)")
        elif action == 'reset':
            db['auto_reply']['replied_users'] = []
            await save_db()
            await event.reply("✅ Replied list reset!")

    # ═══════════════ PMGUARD (Default + Custom Alag) ═══════════════
    @cmd('pmguard')
    async def pmguard_cmd(event):
        args = get_args(event.message).lower().split()
        if not args:
            await event.reply(
                "**Usage:**\n"
                "`.pmguard on` — Default PM Guard ON (5 warnings)\n"
                "`.pmguard off` — PM Guard OFF (dono)\n"
                "`.pmguard set <2/3/4/6>` — Custom PM Guard ON\n"
                "`.pmguard set off` — Custom PM Guard OFF\n"
                "`.pmguard status` — Show status",
                parse_mode='markdown'
            )
            return
        action = args[0]
        pg = db.setdefault('pmguard', {})

        if action == 'on':
            if pg.get('custom_enabled', False):
                await event.reply(
                    "⚠️ **Custom PM Guard ON hai**\n\n"
                    "Pehle `.pmguard set off` karo.",
                    parse_mode='markdown'
                )
                return
            pg['enabled'] = True
            pg['warn_limit'] = 5
            await save_db()
            await event.reply("🛡️ **PM Guard ON**")

        elif action == 'off':
            pg['enabled'] = False
            pg['custom_enabled'] = False
            pg['custom_limit'] = None
            await save_db()
            await event.reply("🛡️ **PM Guard OFF (Default + Custom)**")

        elif action == 'set' and len(args) > 1:
            arg1 = args[1]
            if arg1 == 'off':
                pg['custom_enabled'] = False
                pg['custom_limit'] = None
                await save_db()
                await event.reply("🛡️ **Custom PM Guard OFF**")
                return
            try:
                limit = int(arg1)
                if limit < 1:
                    await event.reply("❌ Limit kam se kam **1** honi chahiye.")
                    return
                if pg.get('enabled', False):
                    await event.reply(
                        "⚠️ **Default PM Guard ON hai**\n\n"
                        "Pehle `.pmguard off` karo.",
                        parse_mode='markdown'
                    )
                    return
                pg['custom_enabled'] = True
                pg['custom_limit'] = limit
                await save_db()
                await event.reply(f"✅ **Custom PM Guard ON** ({limit} warnings)")
            except ValueError:
                await event.reply("❌ Use `.pmguard set 2/3/4/6` or `.pmguard set off`")

        elif action == 'status':
            default_on = pg.get('enabled', False)
            custom_on = pg.get('custom_enabled', False)
            custom_limit = pg.get('custom_limit')
            if default_on:
                status = "🛡️ **Default ON** — 5 warnings"
            elif custom_on:
                status = f"🛡️ **Custom ON** — {custom_limit} warnings"
            else:
                status = "❌ **PM Guard OFF**"
            await event.reply(
                f"📊 **PM Guard Status**\n\n"
                f"Status: {status}\n\n"
                f"Default ON: `{default_on}`\n"
                f"Custom ON: `{custom_on}`\n"
                f"Custom Limit: `{custom_limit}`",
                parse_mode='markdown'
            )
        else:
            await event.reply(
                "**Usage:**\n"
                "`.pmguard on/off`\n"
                "`.pmguard set <2/3/4/6>`\n"
                "`.pmguard set off`\n"
                "`.pmguard status`",
                parse_mode='markdown'
            )

    @cmd('approve')
    async def approve_cmd(event):
        text = get_args(event.message).strip()
        target_id = None
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            target_id = reply_msg.sender_id
        elif text:
            arg = text.split()[0]
            if arg.startswith("@"):
                try: u = await client.get_entity(arg); target_id = u.id
                except: await event.reply(f"❌ `{arg}` not found."); return
            else:
                try:
                    target_id = int(arg)
                    try: await client.get_entity(target_id)
                    except: await event.reply(f"❌ ID `{target_id}` not found."); return
                except ValueError:
                    await event.reply("❌ **Usage:** `.approve` (reply) | `.approve <id>` | `.approve @username`", parse_mode='markdown')
                    return
        else:
            await event.reply("❌ **Usage:** `.approve` (reply) | `.approve <id>` | `.approve @username`", parse_mode='markdown')
            return
        if target_id == db.get('owner_id', owner_id):
            await event.reply("❌ Can't approve owner."); return
        if target_id in db.get('approved', []):
            await event.reply(f"✅ Already approved."); return
        db.setdefault('approved', []).append(target_id)
        db.setdefault('pm_warnings', {}).pop(str(target_id), None)
        try: await client(UnblockRequest(id=target_id))
        except: pass
        await save_db()
        await event.reply(f"✅ **User Approved (PM Guard)**")

    @cmd('disapprove')
    async def disapprove_cmd(event):
        text = get_args(event.message).strip()
        target_id = None
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            target_id = reply_msg.sender_id
        elif text:
            arg = text.split()[0]
            if arg.startswith("@"):
                try: u = await client.get_entity(arg); target_id = u.id
                except: await event.reply(f"❌ `{arg}` not found."); return
            else:
                try: target_id = int(arg)
                except ValueError:
                    await event.reply("❌ **Usage:** `.disapprove` (reply) | `.disapprove <id>` | `.disapprove @username`", parse_mode='markdown')
                    return
        else:
            await event.reply("❌ **Usage:** `.disapprove` (reply) | `.disapprove <id>` | `.disapprove @username`", parse_mode='markdown')
            return
        if target_id not in db.get('approved', []):
            await event.reply(f"⚠️ Not in approved list."); return
        db['approved'].remove(target_id)
        await save_db()
        await event.reply(f"✅ **User Disapproved (PM Guard)**")

    @cmd('approvelist')
    async def approvelist_cmd(event):
        approved = db.get('approved', [])
        if not approved:
            await event.reply("📭 Koi approved user nahi hai."); return
        text = f"✅ **PM Guard Approved Users ({len(approved)})**\n" + "═" * 25 + "\n\n"
        for i, uid in enumerate(approved, 1):
            name = "Unknown"
            username = "None"
            try:
                u = await client.get_entity(int(uid))
                name = ((u.first_name or "") + (" " + u.last_name if getattr(u, 'last_name', None) else "")).strip() or "Unknown"
                username = f"@{u.username}" if u.username else "None"
            except: pass
            text += f"**{i}.** 👤 `{name}`\n   🆔 `{uid}`\n   📛 {username}\n\n"
        await event.reply(text, parse_mode='markdown')

    @cmd('blacklist')
    async def blacklist_cmd(event):
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        if uid not in db['blacklist']:
            db['blacklist'].append(uid)
        try: await client(BlockRequest(id=uid))
        except Exception as e: log.warning(f"Block failed: {e}")
        await save_db()
        await event.reply("🚫 **User Blacklisted & Blocked!**")

    @cmd('removeblacklist')
    async def removeblacklist_cmd(event):
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        if uid in db['blacklist']:
            db['blacklist'].remove(uid)
        try: await client(UnblockRequest(id=uid))
        except Exception as e: log.warning(f"Unblock failed: {e}")
        await save_db()
        await event.reply("✅ **User Removed from Blacklist & Unblocked!**")

    # ═══════════════ SCHEDULED MESSAGES ═══════════════
    @cmd('schedule', delete_cmd=True)
    async def schedule_cmd(event):
        args = get_args(event.message).strip()
        if not args:
            await event.reply(
                "**⏰ SCHEDULE MESSAGE**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "**Format:**\n"
                "`.schedule <date> <time> <target> <message>`\n\n"
                "**Date:** `YYYY-MM-DD` ya `DD-MM-YYYY`\n"
                "**Time (24h):** `HH:MM` ya `HH:MM:SS`\n"
                "**Time (12h):** `hh:MM AM/PM` ya `hh:MM:SS AM/PM`\n\n"
                "**Examples:**\n"
                "`.schedule 2026-01-15 06:19:00 PM @user Good morning!`\n"
                "`.schedule 15-01-2026 18:19:00 1234567890 Hello`\n"
                "`.schedule 2026-01-15 6:19 PM 1234567890 Hi`\n\n"
                "**Reply method:**\n"
                "Kisi msg pe reply karke:\n"
                "`.schedule 2026-01-15 06:19:00 PM`\n\n"
                "**Channel comment:** `.schedulech`\n"
                "**List:** `.schedules`\n"
                "**Cancel:** `.cancelschedule <id>`\n\n"
                "🕐 **Timezone:** IST (Asia/Kolkata)",
                parse_mode='markdown'
            )
            return

        parts = args.split()
        if len(parts) < 2:
            await event.reply("❌ **Format:** `.schedule <date> <time> <target> <message>`")
            return

        date_str = parts[0]
        time_str = parts[1]

        consumed = 2
        if len(parts) >= 3 and parts[2].upper() in ("AM", "PM"):
            time_str = f"{time_str} {parts[2].upper()}"
            consumed = 3

        rest = " ".join(parts[consumed:]).strip()

        try:
            sched_dt = _parse_schedule_time(date_str, time_str)
            sched_ts = sched_dt.timestamp()
        except Exception as e:
            await event.reply(f"❌ **Invalid date/time**\n`{str(e)[:150]}`")
            return

        now_ts = time.time()
        if sched_ts <= now_ts:
            await event.reply(
                f"❌ **Time past me hai!**\n"
                f"Ab: `{datetime.now(IST_TZ).strftime('%d %b %Y, %I:%M:%S %p')} IST`"
            )
            return
        if sched_ts - now_ts > 365 * 24 * 3600:
            await event.reply("❌ Max 1 saal aage tak schedule kar sakte ho.")
            return

        target_id = None
        msg_text = rest
        from_saved = None

        if event.is_reply:
            reply_msg = await event.get_reply_message()
            target_id = reply_msg.sender_id
            from_saved = reply_msg.id
            if not msg_text:
                msg_text = (reply_msg.message or "").strip()
            if not msg_text:
                await event.reply("❌ Message text chahiye (ya reply me text hona chahiye).")
                return
        else:
            if not rest:
                await event.reply("❌ Target aur message dono chahiye.")
                return
            tparts = rest.split(' ', 1)
            target_str = tparts[0]
            msg_text = tparts[1].strip() if len(tparts) > 1 else ""

            if target_str.startswith('@'):
                try:
                    ent = await client.get_entity(target_str)
                    target_id = ent.id
                except Exception:
                    await event.reply(f"❌ `{target_str}` not found.")
                    return
            else:
                try:
                    target_id = int(target_str)
                    await client.get_entity(target_id)
                except Exception:
                    await event.reply(f"❌ Chat `{target_str}` not found.")
                    return

            if not msg_text:
                await event.reply("❌ Message empty hai.")
                return

        if sched_col is None:
            await event.reply("❌ SQLite database not available.")
            return

        
        doc = {
            "_id": uuid.uuid4().hex,
            "owner_id": owner_id,
            "target_id": target_id,
            "text": msg_text,
            "reply_to_msg_id": from_saved,
            "comment_to": None,
            "scheduled_ts": sched_ts,
            "scheduled_str": _fmt_ist_full(sched_ts),
            "scheduled_short": _fmt_ist_12h(sched_ts),
            "chat_id": event.chat_id,
            "created_at": time.time(),
            "status": "pending",
        }
        await sched_col.insert_one(doc)
        asyncio.create_task(_scheduler_runner(client, doc))

        try:
            target_ent = await client.get_entity(target_id)
            tname = getattr(target_ent, 'first_name', None) or getattr(target_ent, 'title', None) or str(target_id)
        except Exception:
            tname = str(target_id)

        preview = msg_text[:80] + ("..." if len(msg_text) > 80 else "")
        eta = int(sched_ts - time.time())
        h, rem = divmod(eta, 3600)
        m, s = divmod(rem, 60)

        confirm_msg = await event.reply(
            f"✅ **SCHEDULED!**\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 **ID:** `{doc['_id'][:8]}`\n"
            f"🎯 **Target:** `{tname}` (`{target_id}`)\n"
            f"🕐 **When:** `{doc['scheduled_short']}`\n"
            f"📅 **Full:** `{doc['scheduled_str']}`\n"
            f"⏳ **ETA:** `{h}h {m}m {s}s`\n"
            f"📝 **Text:** {preview}\n\n"
            f"Cancel: `.cancelschedule {doc['_id'][:8]}`",
            parse_mode='markdown'
        )

        await sched_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "confirm_chat_id": confirm_msg.chat_id,
                "confirm_msg_id": confirm_msg.id,
            }}
        )

    @cmd('schedulech', delete_cmd=True)
    async def schedulech_cmd(event):
        args = get_args(event.message).strip()
        if not args:
            await event.reply(
                "**⏰ SCHEDULE CHANNEL COMMENT**\n"
                "━━━━━━━━━━━━━━━━━━\n\n"
                "**Format:**\n"
                "`.schedulech <date> <time> <post_link> <message>`\n\n"
                "**Time (24h):** `HH:MM` / `HH:MM:SS`\n"
                "**Time (12h):** `hh:MM PM` / `hh:MM:SS PM`\n\n"
                "**Example:**\n"
                "`.schedulech 2026-01-15 06:30 PM https://t.me/SomeChannel/123 Hello`\n\n"
                "**Note:**\n"
                "• Channel **public** hona chahiye\n"
                "• Aap discussion group me joined ho\n\n"
                "🕐 **Timezone:** IST (Asia/Kolkata)",
                parse_mode='markdown'
            )
            return

        parts = args.split()
        if len(parts) < 3:
            await event.reply("❌ **Format:** `.schedulech <date> <time> <post_link> <message>`")
            return

        date_str = parts[0]
        time_str = parts[1]
        consumed = 2
        if len(parts) >= 3 and parts[2].upper() in ("AM", "PM"):
            time_str = f"{time_str} {parts[2].upper()}"
            consumed = 3

        rest = " ".join(parts[consumed:]).strip()

        try:
            sched_dt = _parse_schedule_time(date_str, time_str)
            sched_ts = sched_dt.timestamp()
        except Exception as e:
            await event.reply(f"❌ **Invalid date/time**\n`{str(e)[:150]}`")
            return

        if sched_ts <= time.time():
            await event.reply(
                f"❌ **Time past me hai!**\n"
                f"Ab: `{datetime.now(IST_TZ).strftime('%d %b %Y, %I:%M:%S %p')} IST`"
            )
            return
        if sched_ts - time.time() > 365 * 24 * 3600:
            await event.reply("❌ Max 1 saal aage tak schedule kar sakte ho.")
            return

        tparts = rest.split(' ', 1)
        post_link = tparts[0].strip()
        msg_text = tparts[1].strip() if len(tparts) > 1 else ""

        if not msg_text:
            await event.reply("❌ Message empty hai.")
            return

        m = re.match(r'^https?://t\.me/(?:c/)?([^/\s]+)/(\d+)', post_link)
        if not m:
            await event.reply(
                "❌ **Invalid post link**\n"
                "Format: `https://t.me/username/123`"
            )
            return

        chat_part = m.group(1)
        post_id = int(m.group(2))

        try:
            if chat_part.isdigit() or chat_part.startswith('-'):
                channel_id = int(chat_part) if chat_part.startswith('-') else int("-100" + chat_part)
                channel = await client.get_entity(channel_id)
            else:
                channel = await client.get_entity(chat_part)
        except Exception as e:
            await event.reply(f"❌ **Channel not found**\n`{chat_part}`\n\n_Error: {str(e)[:100]}_")
            return

        try:
            post_msg = await client.get_messages(channel, ids=post_id)
            if not post_msg:
                await event.reply(f"❌ Post ID `{post_id}` not found.")
                return
        except Exception as e:
            await event.reply(f"❌ Could not fetch post: `{str(e)[:100]}`")
            return

        if sched_col is None:
            await event.reply("❌ SQLite database not available.")
            return

        
        doc = {
            "_id": uuid.uuid4().hex,
            "owner_id": owner_id,
            "target_id": channel.id,
            "text": msg_text,
            "reply_to_msg_id": None,
            "comment_to": post_id,
            "scheduled_ts": sched_ts,
            "scheduled_str": _fmt_ist_full(sched_ts),
            "scheduled_short": _fmt_ist_12h(sched_ts),
            "chat_id": event.chat_id,
            "created_at": time.time(),
            "status": "pending",
            "post_link": post_link,
        }
        await sched_col.insert_one(doc)
        asyncio.create_task(_scheduler_runner(client, doc))

        preview = msg_text[:80] + ("..." if len(msg_text) > 80 else "")
        eta = int(sched_ts - time.time())
        h, rem = divmod(eta, 3600)
        m2, s = divmod(rem, 60)

        confirm_msg = await event.reply(
            f"✅ **CHANNEL COMMENT SCHEDULED!**\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"🆔 **ID:** `{doc['_id'][:8]}`\n"
            f"📢 **Channel:** `{getattr(channel, 'title', chat_part)}`\n"
            f"🔗 **Post:** `{post_link}`\n"
            f"🕐 **When:** `{doc['scheduled_short']}`\n"
            f"📅 **Full:** `{doc['scheduled_str']}`\n"
            f"⏳ **ETA:** `{h}h {m2}m {s}s`\n"
            f"📝 **Comment:** {preview}\n\n"
            f"Cancel: `.cancelschedule {doc['_id'][:8]}`",
            parse_mode='markdown'
        )

        await sched_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {
                "confirm_chat_id": confirm_msg.chat_id,
                "confirm_msg_id": confirm_msg.id,
            }}
        )

    @cmd('schedules', delete_cmd=False)
    async def schedules_cmd(event):
        if sched_col is None:
            await event.reply("❌ SQLite database not available.")
            return
        cursor = sched_col.find({"owner_id": owner_id, "status": "pending"}).sort("scheduled_ts", 1)
        rows = await cursor.to_list(length=50)
        if not rows:
            await event.reply("📭 Koi pending schedule nahi hai.")
            return
        text = f"⏰ **PENDING SCHEDULES ({len(rows)})**\n" + "═" * 25 + "\n\n"
        for r in rows:
            eta = int(r["scheduled_ts"] - time.time())
            if eta < 0:
                eta = 0
            h, rem = divmod(eta, 3600)
            m, s = divmod(rem, 60)
            if r.get("comment_to"):
                target_label = f"📢 Post: `{r.get('post_link', str(r['comment_to']))}`"
            else:
                try:
                    ent = await client.get_entity(r["target_id"])
                    tname = getattr(ent, 'first_name', None) or getattr(ent, 'title', None) or str(r["target_id"])
                except Exception:
                    tname = str(r["target_id"])
                target_label = f"🎯 `{tname}`"
            preview = r["text"][:50] + ("..." if len(r["text"]) > 50 else "")
            when_str = r.get("scheduled_short") or _fmt_ist_12h(r["scheduled_ts"])
            text += (
                f"🆔 `{r['_id'][:8]}`\n"
                f"{target_label}\n"
                f"🕐 `{when_str}` (ETA {h}h{m}m{s}s)\n"
                f"📝 {preview}\n\n"
            )
        text += "Cancel: `.cancelschedule <id>`"
        await event.reply(text, parse_mode='markdown')

    @cmd('cancelschedule', delete_cmd=False)
    async def cancelschedule_cmd(event):
        if sched_col is None:
            await event.reply("❌ SQLite database not available.")
            return
        args = get_args(event.message).strip()
        if not args:
            await event.reply("❌ `.cancelschedule <id_prefix>`\nList: `.schedules`")
            return
        prefix = args.split()[0]
        matches = await sched_col.find({
            "owner_id": owner_id,
            "status": "pending",
            "_id": {"$regex": f"^{re.escape(prefix)}"}
        }).to_list(length=5)

        if not matches:
            await event.reply(f"❌ `{prefix}` se koi schedule nahi mila.")
            return
        if len(matches) > 1:
            await event.reply(f"⚠️ {len(matches)} matches. Longer prefix do.")
            return

        doc = matches[0]
        await sched_col.update_one(
            {"_id": doc["_id"]},
            {"$set": {"status": "cancelled"}}
        )
        # 🗑️ Delete original confirmation message if exists
        try:
            cf_chat = doc.get("confirm_chat_id")
            cf_msg = doc.get("confirm_msg_id")
            if cf_chat and cf_msg:
                await client.delete_messages(int(cf_chat), [int(cf_msg)])
                log.info(f"🗑️ Deleted confirmation on cancel: {cf_msg}")
        except Exception as e_del:
            log.warning(f"⚠️ Could not delete confirmation on cancel: {e_del}")
        await event.reply(f"✅ **Cancelled** `{doc['_id'][:8]}`\n📝 {doc['text'][:60]}")

    # ═══════════════ JARVIS QUICK SCHEDULER ALIASES ═══════════════
    # .jarsend in 10m @username | message
    # .jarsend at 22:30:15 here | message
    @cmd('jarsend', delete_cmd=True)
    async def jarsend_cmd(event):
        args = get_args(event.message).strip()
        match = re.match(r'(?is)^(in\s+([^\s]+)|at\s+(\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,3})?)?))\s+([^|]+?)\s*\|\s*(.+)$', args)
        if not match:
            return await event.reply(
                "⏰ **JARVIS QUICK SCHEDULE**\n\n"
                "`.jarsend in 10m @username | Hello!`\n"
                "`.jarsend in 2h @username | Kaise ho?`\n"
                "`.jarsend at 22:30:15 @username | Good Night`\n"
                "`.jarsend at 22:30:15.250 here | Quiz shuru!`\n"
                "`.jarsend in 250ms here | Test`\n\n"
                "`10m` = 10 minutes, `250ms` = 250 milliseconds. `at` uses IST."
            )
        relative, duration, clock, target_raw, message_text = match.groups()
        now = datetime.now(IST_TZ)
        if relative:
            d = duration.lower()
            parts = re.fullmatch(r'(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+)ms)?', d)
            if not parts or not any(parts.groups()):
                return await event.reply("❌ Duration format: `10m`, `2h`, `1h30m`, `0.5s`, `250ms`.")
            days, hours, mins = (int(x or 0) for x in parts.groups()[:3])
            secs = float(parts.group(4) or 0)
            millis = int(parts.group(5) or 0)
            delay = days*86400 + hours*3600 + mins*60 + secs + millis/1000.0
            if delay <= 0 or delay > 365*86400:
                return await event.reply("❌ Duration 1 millisecond se 1 year ke beech honi chahiye.")
            sched_ts = time.time() + delay
        else:
            hh, mm, *ss = clock.split(':')
            sec_text = ss[0] if ss else "0"
            sec_parts = sec_text.split('.', 1)
            sec = int(sec_parts[0])
            microsecond = int((sec_parts[1] if len(sec_parts) > 1 else "").ljust(3, "0") or "0") * 1000
            if hh.isdigit() is False or mm.isdigit() is False or int(hh) > 23 or int(mm) > 59 or sec > 59:
                return await event.reply("❌ Valid 24-hour time do, jaise `22:30:15.250`.")
            target_dt = now.replace(hour=int(hh), minute=int(mm), second=sec, microsecond=microsecond)
            if target_dt.timestamp() <= time.time():
                target_dt += __import__('datetime').timedelta(days=1)
            sched_ts = target_dt.timestamp()
        target_raw = target_raw.strip()
        if target_raw.lower() == 'here':
            target_id = event.chat_id
        else:
            try:
                ent = await client.get_entity(target_raw)
                target_id = ent.id
            except Exception:
                return await event.reply(f"❌ Target `{target_raw}` nahi mila ya accessible nahi hai.")
        if sched_col is None:
            return await event.reply("❌ Scheduler database available nahi hai.")
        doc = {
            "_id": uuid.uuid4().hex, "owner_id": owner_id,
            "target_id": target_id, "text": message_text.strip(),
            "reply_to_msg_id": None, "comment_to": None,
            "scheduled_ts": sched_ts, "scheduled_str": _fmt_ist_full(sched_ts),
            "scheduled_short": _fmt_ist_12h(sched_ts), "chat_id": event.chat_id,
            "created_at": time.time(), "status": "pending"
        }
        await sched_col.insert_one(doc)
        asyncio.create_task(_scheduler_runner(client, doc))
        eta = max(0.0, sched_ts-time.time())
        total_ms = int(eta * 1000)
        h, rem = divmod(total_ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        sec, ms = divmod(rem, 1000)
        await event.reply(
            f"✅ **JARVIS MESSAGE SCHEDULED**\n🆔 `{doc['_id'][:8]}`\n"
            f"🎯 Target: `{target_raw}`\n🕒 `{doc['scheduled_short']}` (IST)\n"
            f"⏳ ETA: `{h}h {m}m {sec}s {ms:03d}ms`\n📝 {message_text[:120]}\n\n"
            f"Cancel: `.jarcancel {doc['_id'][:8]}`"
        )

    @cmd('jarsends', delete_cmd=False)
    async def jarsends_cmd(event):
        rows = await sched_col.find({"owner_id": owner_id, "status": "pending"}).sort("scheduled_ts", 1).to_list(length=30)
        if not rows:
            return await event.reply("📭 Koi pending Jarvis schedule nahi hai.")
        lines = ["⏰ **JARVIS SCHEDULES**"]
        for d in rows:
            lines.append(f"`{d['_id'][:8]}` · {d.get('scheduled_short', _fmt_ist_12h(d['scheduled_ts']))} · {d.get('text','')[:55]}")
        lines.append("\nCancel: `.jarcancel <id>`")
        await event.reply("\n".join(lines))

    @cmd('jarcancel', delete_cmd=False)
    async def jarcancel_cmd(event):
        prefix = get_args(event.message).strip().split()[0] if get_args(event.message).strip() else ""
        if not prefix:
            return await event.reply("Usage: `.jarcancel <id>` — list: `.jarsends`")
        matches = await sched_col.find({"owner_id": owner_id, "status": "pending", "_id": {"$regex": f"^{re.escape(prefix)}"}}).to_list(length=2)
        if len(matches) != 1:
            return await event.reply("❌ Schedule nahi mila ya ID ambiguous hai. `.jarsends` check karo.")
        d = matches[0]
        await sched_col.update_one({"_id": d["_id"]}, {"$set": {"status": "cancelled"}})
        await event.reply(f"✅ Cancelled `{d['_id'][:8]}`")

    # ═══════════════ SYNC COMMAND (Auto-Tune) ═══════════════
    @cmd('sync', delete_cmd=False)
    async def sync_cmd(event):
        global PING_OFFSET
        msg = await event.reply("🔄 **Syncing... Calculating network latency...**")
        
        try:
            # Test ping 5 times and find average
            from telethon.tl.functions import PingRequest
            times = []
            for _ in range(5):
                start = time.perf_counter()
                await client(PingRequest(ping_id=random.randint(1, 10**9)))
                end = time.perf_counter()
                times.append(end - start)
                await asyncio.sleep(0.1)
            
            avg_ping = sum(times) / len(times)
            # Add 5ms safety margin
            new_offset = avg_ping + 0.005 
            
            PING_OFFSET = new_offset
            
            await msg.edit(
                f"✅ **Sync Complete!**\n\n"
                f"📡 **Average Ping:** `{avg_ping * 1000:.1f} ms`\n"
                f"⚙️ **New Offset:** `{PING_OFFSET * 1000:.1f} ms`\n\n"
                f"_Scheduler offset calibrated hai; actual delivery Telegram/network latency par depend karegi._",
                parse_mode='markdown'
            )
        except Exception as e:
            await msg.edit(f"❌ **Sync Failed:** `{str(e)[:200]}`")

    # ═══════════════ HANDLERS ═══════════════
    @client.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
    async def pm_guard_handler(event):
        try:
            uid = event.sender_id
            if uid == db.get('owner_id', owner_id): return
            try:
                fresh = await user_data_col.find_one({"_id": str(owner_id)})
                if fresh:
                    fresh.pop("_id", None)
                    for k, v in fresh.items(): db[k] = v
            except: pass
            pg = db.get('pmguard', {})
            default_on = pg.get('enabled', False)
            custom_on = pg.get('custom_enabled', False)
            custom_limit = pg.get('custom_limit')
            warn_limit = None
            if default_on: warn_limit = 5
            elif custom_on and custom_limit: warn_limit = custom_limit
            else: return
            log.info(f"🔍 PM Guard: uid={uid}, default={default_on}, custom={custom_on}, limit={warn_limit}")
            approved_list = db.get('approved', [])
            if uid in approved_list: return
            blacklist = db.get('blacklist', [])
            if uid in blacklist:
                try: await client(BlockRequest(id=uid))
                except: pass
                return
            uid_str = str(uid)
            pm_warn = db.get('pm_warnings', {})
            if not isinstance(pm_warn, dict): pm_warn = {}
            w = pm_warn.get(uid_str, 0) + 1
            pm_warn[uid_str] = w
            db['pm_warnings'] = pm_warn
            await save_db()
            log.info(f"⚠️ PM Guard warning: uid={uid}, count={w}/{warn_limit}")
            if w < warn_limit:
                await event.reply(
                    f"**🔒 SECURITY ALERT**\n\n"
                    f"Owner is currently busy/offline😌\n"
                    f"Please wait and don't spam!\n\n"
                    f"⚠️ Warning: {w}/{warn_limit}\n\n"
                    f"🚫 {warn_limit}th warning = Automatic Block"
                )
            else:
                await event.reply(f"🚫 **You have been blocked for spamming!**\n({warn_limit} warnings reached)")
                try:
                    await client(BlockRequest(id=uid))
                    if uid not in db.get('blocklist', []):
                        db.setdefault('blocklist', []).append(uid)
                    db['pm_warnings'].pop(uid_str, None)
                    await save_db()
                except Exception as e:
                    log.error(f"PM Guard block failed: {e}")
        except Exception as e:
            log.error(f"pm_guard_handler error: {e}", exc_info=True)

    @client.on(events.NewMessage)
    async def gmute_filter(event):
        if event.out: return
        uid = event.sender_id
        if str(uid) in db['gmuted']:
            try:
                await event.delete()
                if event.is_private: await client(BlockRequest(id=uid))
            except Exception as e:
                log.debug(f"gmute failed: {e}")

    @client.on(events.NewMessage)
    async def auto_react_handler(event):
        if event.out: return
        if event.is_private:
            if db['auto_react_dm_enabled']:
                emoji = random.choice(db['auto_react_emojis'])
                try: await client(SendReactionRequest(peer=event.chat_id, msg_id=event.message.id, reaction=[ReactionEmoji(emoticon=emoji)]))
                except: pass
        else:
            if db['auto_react_group_enabled']:
                emoji = random.choice(db['auto_react_emojis'])
                try: await client(SendReactionRequest(peer=event.chat_id, msg_id=event.message.id, reaction=[ReactionEmoji(emoticon=emoji)]))
                except: pass

    @client.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
    async def auto_reply_handler(event):
        try:
            uid = event.sender_id
            if uid == db.get('owner_id', owner_id): return
            try:
                fresh = await user_data_col.find_one({"_id": str(owner_id)})
                if fresh:
                    fresh.pop("_id", None)
                    for k, v in fresh.items(): db[k] = v
            except: pass
            ar = db.get('auto_reply', {})
            enabled = ar.get('enabled', False)
            mode = ar.get('mode', 'always')
            text = ar.get('text', 'The owner is busy with some personal work at the moment😌Please wait until they are back online🥀')
            log.info(f"🔍 Auto Reply check: uid={uid}, enabled={enabled}, mode={mode}")
            if not enabled: return
            approved_users = ar.get('approved_users', [])
            if uid in approved_users:
                log.info(f"⏭️ Auto Reply skipped: uid={uid} in auto_reply approved_users")
                return
            if mode == 'always':
                await asyncio.sleep(0.5)
                try:
                    await event.reply(text)
                    log.info(f"✅ Auto Reply sent to {uid}")
                except Exception as e:
                    log.error(f"Auto Reply send failed: {e}")
                return
            if mode == 'once':
                replied = ar.get('replied_users', [])
                if uid in replied: return
                await asyncio.sleep(0.5)
                try:
                    await event.reply(text)
                    replied.append(uid)
                    db['auto_reply']['replied_users'] = replied
                    await save_db()
                except: pass
        except Exception as e:
            log.error(f"auto_reply_handler error: {e}", exc_info=True)

    # ═══════════════ AI REPLY HANDLER (NATURAL - max_tokens 300) ═══════════════
    @client.on(events.NewMessage(func=lambda e: e.is_private and not e.out))
    async def ai_reply_handler(event):
        try:
            uid = event.sender_id
            if uid == db.get('owner_id', owner_id): return
            try:
                fresh = await user_data_col.find_one({"_id": str(owner_id)})
                if fresh:
                    fresh.pop("_id", None)
                    for k, v in fresh.items(): db[k] = v
            except: pass

            ar = db.get('ai_reply', {})
            if not ar.get('enabled', False): return
            if uid in ar.get('approved_users', []): return
            if db.get('auto_reply', {}).get('enabled', False): return
            if not GROQ_API_KEY: return

            user_msg = (event.message.text or "").strip()
            if not user_msg: return

            url = "https://api.groq.com/openai/v1/chat/completions"
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {GROQ_API_KEY}"
            }
            payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [
                    {"role": "system", "content": (
                        "You are a real human chatting on Telegram on behalf of the account owner. "
                        "The owner is currently away/busy, so you handle their DMs naturally.\n\n"
                        "STRICT RULES:\n"
                        "1. Reply in the SAME language as the user (Hindi/Hinglish/English/Urdu/Arabic). "
                        "If user writes Hindi, reply Hindi. If Urdu, reply Urdu. If English, reply English. If Arabic, reply Arabic.\n"
                        "2. Keep replies NATURAL — usually 2 to 4 lines. Not too short, not too long. "
                        "Reply like a real person chatting on WhatsApp. Sometimes 1 line, sometimes 3-4 lines depending on context.\n"
                        "3. NEVER say things like 'owner busy hai', 'will get back soon', 'message mil gaya', 'aapka sandesh' — this sounds robotic and reveals you're a bot.\n"
                        "4. Chat naturally like a friend. If someone says 'Hi' — reply friendly. If someone asks 'kya kar rahe ho' — reply casually with a bit of detail.\n"
                        "5. ONLY mention 'owner busy hai, wo personally baat karenge' ONCE at the very start IF the user clearly wants to talk about something serious/important with the owner. Otherwise don't bring it up.\n"
                        "6. Do NOT add emojis or 'Note:' or disclaimers in every message.\n"
                        "7. Do NOT repeat the user's message back to them.\n"
                        "8. Match the user's tone — casual, friendly. Like a real WhatsApp chat.\n"
                        "9. If user says something weird/abusive, stay calm and reply politely, don't lecture.\n"
                        "10. NEVER reveal you are an AI or bot. Just chat naturally.\n"
                        "11. Add small natural expressions like 'Hmm', 'Achha', 'Oh', 'Haan' occasionally to make it feel human."
                    )},
                    {"role": "user", "content": user_msg}
                ],
                "max_tokens": 400,
                "temperature": 0.8
            }
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        log.warning(f"AI reply API error: {resp.status}")
                        return
                    data = await resp.json()

            ai_answer = data["choices"][0]["message"]["content"].strip()
            if len(ai_answer) > 3900:
                ai_answer = ai_answer[:3900]

            await event.reply(ai_answer)

        except asyncio.TimeoutError:
            log.warning("AI reply timeout")
        except Exception as e:
            log.error(f"AI reply error: {e}")

    # ═══════════════ RAID ENGINE ═══════════════
    # Keep the old reply-raid feature under a distinct command so /reply
    # can be reserved for the owner's JARVIS answer-on-demand command.
    @cmd('replyraid')
    async def reply_raid_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        chat_id = event.chat_id
        async def send_reply():
            msg = random.choice(REPLY_MESSAGES)
            await client.send_message(chat_id, msg)
        asyncio.create_task(raid_loop(event, uid, "reply", send_reply, 1.5))
        await event.reply(f"⚔️ **Reply raid started!**")

    @cmd('sreply')
    async def sreply_raid_cmd(event):
        chat_id = str(event.chat_id)
        if chat_id in raid_state['reply']:
            raid_state['reply'][chat_id].clear()
            await event.reply("🛑 **Reply raid stopped!**")

    @cmd('rr')
    async def rr_raid_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        chat_id = event.chat_id
        async def send_rr():
            await client.send_message(chat_id, f"🤣 {user_link(uid)}", parse_mode='markdown')
        asyncio.create_task(raid_loop(event, uid, "rr", send_rr, 1))
        await event.reply(f"🤣 **RR raid started!**")

    @cmd('srr')
    async def srr_raid_cmd(event):
        chat_id = str(event.chat_id)
        if chat_id in raid_state['rr']:
            raid_state['rr'][chat_id].clear()
            await event.reply("🛑 **RR raid stopped!**")

    @cmd('flag')
    async def flag_raid_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        chat_id = event.chat_id
        flags = ['🇮🇳', '🇺🇸', '🇬🇧', '🇩🇪', '🇫🇷', '🇯🇵', '🇨🇳', '🇷🇺', '🇧🇷', '🇦🇪']
        async def send_flag():
            await client.send_message(chat_id, f"{random.choice(flags)} {user_link(uid)}", parse_mode='markdown')
        asyncio.create_task(raid_loop(event, uid, "flag", send_flag, 1.2))
        await event.reply(f"🚩 **Flag raid started!**")

    @cmd('sflag')
    async def sflag_raid_cmd(event):
        chat_id = str(event.chat_id)
        if chat_id in raid_state['flag']:
            raid_state['flag'][chat_id].clear()
            await event.reply("🛑 **Flag raid stopped!**")

    @cmd('hrr')
    async def heart_raid_cmd(event):
        if not event.is_group: return
        uid = await resolve_user(client, event.message, get_args(event.message))
        if not uid:
            await event.reply("❌ User not found."); return
        chat_id = event.chat_id
        hearts = ['❤️', '🧡', '💛', '💚', '💙', '💜', '🖤', '🤍', '🤎', '💗']
        async def send_heart():
            await client.send_message(chat_id, f"{random.choice(hearts)} {user_link(uid)}", parse_mode='markdown')
        asyncio.create_task(raid_loop(event, uid, "hrr", send_heart, 1))
        await event.reply(f"💖 **Heart raid started!**")

    @cmd('shrr')
    async def sheart_raid_cmd(event):
        chat_id = str(event.chat_id)
        if chat_id in raid_state['hrr']:
            raid_state['hrr'][chat_id].clear()
            await event.reply("🛑 **Heart raid stopped!**")

    # ═══════════════ SPAM ═══════════════
    @cmd('spray')
    async def spray_cmd(event):
        args = get_args(event.message).strip()
        if not args:
            return
        parts = args.split()
        count = 10
        text = args
        if parts[0].isdigit():
            count = int(parts[0])
            text = ' '.join(parts[1:]).strip()
        elif parts[-1].isdigit():
            count = int(parts[-1])
            text = ' '.join(parts[:-1]).strip()
        if not text:
            return
        count = min(max(count, 1), 100)
        chat_id = event.chat_id
        db['spam_running'] = True

        async def spray_loop():
            for _ in range(count):
                if not db.get('spam_running', False):
                    break
                try:
                    await client.send_message(chat_id, text)
                    await asyncio.sleep(0.4)
                except FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 2)
                except Exception:
                    break
            db['spam_running'] = False

        asyncio.create_task(spray_loop())

    @cmd('dspray')
    async def dspray_cmd(event):
        db['spam_running'] = False
        await event.reply("🛑 **Spam stopped!**")

    # ═══════════════ FUN ═══════════════
    @cmd('ping', delete_cmd=False)
    async def ping_cmd(event):
        start = time.time()
        msg = await event.reply("🏓 Pong!")
        await msg.edit(f"🏓 **Pong!** `{round((time.time() - start) * 1000, 2)}ms`", parse_mode='markdown')

    @cmd('flip', delete_cmd=False)
    async def flip_cmd(event):
        await event.reply(f"{random.choice(['**Heads** 🪙', '**Tails** 🪙'])}", parse_mode='markdown')

    @cmd('dice', delete_cmd=False)
    async def dice_cmd(event):
        await event.reply(f"🎲 **Dice:** `{random.randint(1, 6)}`", parse_mode='markdown')

    # ═══════════════ STALK ═══════════════
    @cmd('stalk', delete_cmd=False)
    async def stalk_cmd(event):
        reply = await event.get_reply_message()
        if not reply:
            await event.reply("❌ Reply to a user's message!"); return
        try:
            user = await reply.get_sender()
            full = await client(functions.users.GetFullUserRequest(user.id))
            fu = full.full_user
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "Unknown"
            username = f"@{user.username}" if user.username else "Hidden"
            bio = fu.about or "No bio"
            photos = await client.get_profile_photos(user)
            status = "Unknown"
            if hasattr(user, 'status') and user.status:
                sn = type(user.status).__name__
                if 'Online' in sn: status = "🟢 Online NOW"
                elif 'Recently' in sn: status = "🟡 Recently"
                elif 'LastWeek' in sn: status = "🟠 Last week"
                elif 'LastMonth' in sn: status = "🔴 Last month"
                else: status = "⚫ Long time ago"
            premium = "⭐ Yes" if getattr(user, 'premium', False) else "No"
            verified = "✅ Yes" if getattr(user, 'verified', False) else "No"
            scam = "⚠️ YES!" if getattr(user, 'scam', False) else "No"
            fake = "⚠️ YES!" if getattr(user, 'fake', False) else "No"
            await event.reply(
                f"🕵️ **Deep Stalk**\n\n👤 **Name:** {name}\n🆔 **ID:** `{user.id}`\n📛 **Username:** {username}\n📝 **Bio:** {bio}\n📊 **Status:** {status}\n📸 **Profile Pics:** {len(photos)}\n⭐ **Premium:** {premium}\n✅ **Verified:** {verified}\n🚫 **Scam:** {scam}\n🎭 **Fake:** {fake}\n💬 **Common Chats:** {fu.common_chats_count}\n\n🔗 [Profile Link](tg://user?id={user.id})",
                parse_mode='markdown'
            )
        except Exception as e:
            await event.reply(f"❌ Error: `{str(e)[:150]}`")

    @cmd('allpfp', delete_cmd=False)
    async def allpfp_cmd(event):
        reply = await event.get_reply_message()
        if reply: user = await reply.get_sender()
        else: user = await event.get_sender()
        try:
            photos = await client.get_profile_photos(user, limit=100)
            if not photos:
                await event.reply("❌ No profile pictures found!"); return
            total = len(photos)
            msg = await event.reply(f"📸 Downloading all {total} pics...")
            sent = 0
            for i, photo in enumerate(photos):
                try:
                    await client.send_file(event.chat_id, photo, caption=f"📸 **{user.first_name or 'User'}'s PFP** ({i + 1}/{total})")
                    sent += 1
                    await asyncio.sleep(0.4)
                except FloodWaitError as e: await asyncio.sleep(e.seconds + 2)
                except: continue
            await msg.edit(f"✅ Sent all **{sent}/{total}** profile pictures!")
        except Exception as e:
            await event.reply(f"❌ Error: `{str(e)[:150]}`")

    @cmd('lastmsg', delete_cmd=False)
    async def lastmsg_cmd(event):
        reply = await event.get_reply_message()
        if not reply:
            await event.reply("❌ Reply to a user's message!"); return
        args = get_args(event.message)
        try: count = min(int(args) if args else 5, 20)
        except: count = 5
        try:
            user = await reply.get_sender()
            chat = await event.get_chat()
            msgs = []
            async for m in client.iter_messages(chat, from_user=user, limit=count):
                text = m.text or "[Media]"
                msgs.append(f"• `{text[:60]}{'...' if len(text) > 60 else ''}`")
            if not msgs:
                await event.reply(f"❌ No messages from {user.first_name or 'user'}!"); return
            await event.reply(f"🕵️ **Last {len(msgs)} messages from {user.first_name or 'user'}**\n\n" + "\n".join(msgs))
        except Exception as e:
            await event.reply(f"❌ Error: `{str(e)[:150]}`")

    # ═══════════════ MUSIC ═══════════════
    async def deezer_search(query, limit=1):
        try:
            url = f"https://api.deezer.com/search?q={url_quote(query)}&limit={limit}"
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as resp:
                    if resp.status == 200: return await resp.json()
        except: pass
        return None

    @cmd('lyrics', delete_cmd=False)
    async def lyrics_cmd(event):
        query = get_args(event.message)
        if not query:
            await event.reply("❌ Usage: `.lyrics <song name or line>`"); return
        msg = await event.reply("🎵 Searching lyrics...")
        async def fetch_lyrics(artist, title):
            try:
                url = f"https://api.lyrics.ovh/v1/{url_quote(artist)}/{url_quote(title)}"
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            d = await resp.json()
                            return d.get("lyrics")
            except: pass
            return None
        try:
            deezer = await deezer_search(query, 3)
            if deezer and deezer.get("data"):
                for track in deezer["data"]:
                    artist = track["artist"]["name"]
                    title = track["title"]
                    lyr = await fetch_lyrics(artist, title)
                    if lyr:
                        await msg.edit(f"🎶 **{title}** — {artist}\n\n{lyr[:3900]}")
                        return
            parts = query.split(maxsplit=1)
            if len(parts) == 2:
                lyr = await fetch_lyrics(parts[0], parts[1])
                if lyr:
                    await msg.edit(f"🎶 **Lyrics for:** `{query}`\n\n{lyr[:3900]}")
                    return
            lyr = await fetch_lyrics("Various Artists", query)
            if lyr:
                await msg.edit(f"🎶 **Lyrics for:** `{query}`\n\n{lyr[:3900]}")
                return
            await msg.edit("❌ Lyrics not found. Try: `.lyrics artist - song`")
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:150]}`")

    @cmd('songinfo', delete_cmd=False)
    async def songinfo_cmd(event):
        query = get_args(event.message)
        if not query:
            await event.reply("❌ Usage: `.songinfo <song>`"); return
        msg = await event.reply("🎵 Fetching details...")
        try:
            search = await deezer_search(query, 1)
            if not search or not search.get("data"):
                await msg.edit("❌ Song not found."); return
            track_id = search["data"][0]["id"]
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(f"https://api.deezer.com/track/{track_id}") as resp:
                    track = await resp.json()
            dur = f"{track['duration'] // 60}:{track['duration'] % 60:02d}"
            explicit = "🔞 Yes" if track.get("explicit_lyrics") else "✅ No"
            await msg.edit(f"🎵 **Detailed Song Info**\n\n🎤 **Title:** `{track.get('title', 'N/A')}`\n👤 **Artist:** `{track['artist']['name']}`\n💿 **Album:** `{track['album']['title']}`\n📅 **Released:** `{track.get('release_date', 'N/A')}`\n⏱ **Duration:** `{dur}`\n🎛 **BPM:** `{track.get('bpm', 'N/A')}`\n🔞 **Explicit:** {explicit}\n🔗 [Listen]({track.get('link', '')})", parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:150]}`")

    @cmd('spotify', delete_cmd=False)
    async def spotify_cmd(event):
        query = get_args(event.message)
        if not query:
            await event.reply("❌ Usage: `.spotify <query>`"); return
        msg = await event.reply("🎵 Searching...")
        try:
            data = await deezer_search(query, 5)
            if not data or not data.get("data"):
                await msg.edit("❌ No results found."); return
            text = f"🎵 **Search results:** `{query}`\n\n"
            for i, track in enumerate(data["data"], 1):
                dur = f"{track['duration'] // 60}:{track['duration'] % 60:02d}"
                preview = f"[▶️]({track['preview']})" if track.get("preview") else ""
                text += (f"**{i}.** `{track['title']}` – {track['artist']['name']}\n   ⏱ {dur} {preview} 🔗 [Listen]({track['link']})\n\n")
            await msg.edit(text, parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:150]}`")

    @cmd('trending', delete_cmd=False)
    async def trending_cmd(event):
        msg = await event.reply("📊 Loading trending...")
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get("https://api.deezer.com/chart/0/tracks?limit=10") as resp:
                    data = await resp.json()
            if not data or not data.get("data"):
                await msg.edit("❌ Failed to load charts."); return
            text = "🔥 **Global Trending Songs**\n\n"
            for i, track in enumerate(data["data"], 1):
                dur = f"{track['duration'] // 60}:{track['duration'] % 60:02d}"
                medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"**{i}.**"
                text += f"{medal} `{track['title']}` – {track['artist']['name']} (⏱ {dur})\n"
            await msg.edit(text, parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:150]}`")

    @cmd('ringtone', delete_cmd=False)
    async def ringtone_cmd(event):
        query = get_args(event.message)
        if not query:
            await event.reply("❌ Usage: `.ringtone <song>`"); return
        msg = await event.reply("🔔 Searching ringtones...")
        try:
            data = await deezer_search(query, 5)
            if not data or not data.get("data"):
                await msg.edit("❌ No ringtones found."); return
            text = f"🔔 **Ringtone Search:** `{query}`\n\n"
            found = False
            for i, track in enumerate(data["data"], 1):
                if track.get("preview"):
                    found = True
                    text += f"**{i}.** `{track['title']}` – {track['artist']['name']}\n   📥 [Download 30s]({track['preview']})\n\n"
            if not found:
                await msg.edit("❌ No previews available."); return
            await msg.edit(text, parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:150]}`")

    @cmd('playlist', delete_cmd=False)
    async def playlist_cmd(event):
        mood = get_args(event.message).lower()
        if not mood:
            await event.reply("❌ Usage: `.playlist <mood>`\nMoods: happy, sad, chill, party, romantic, workout, focus"); return
        MOOD_GENRES = {"happy": "132", "sad": "165", "chill": "106", "party": "113", "romantic": "98", "workout": "129", "focus": "466"}
        msg = await event.reply(f"🎶 Generating `{mood}` playlist...")
        try:
            genre_id = MOOD_GENRES.get(mood, "132")
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(f"https://api.deezer.com/genre/{genre_id}/artists") as resp:
                    artists = await resp.json()
            if not artists or not artists.get("data"):
                await msg.edit("❌ Mood not found."); return
            async def fetch_top(aid):
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                        async with s.get(f"https://api.deezer.com/artist/{aid}/top?limit=2") as r:
                            return await r.json()
                except: return None
            results = await asyncio.gather(*[fetch_top(a["id"]) for a in artists["data"][:5]])
            tracks = []
            for top in results:
                if top and top.get("data"): tracks.extend(top["data"])
            if not tracks:
                await msg.edit("❌ No tracks found."); return
            emoji = {"happy": "😄", "sad": "😢", "chill": "😌", "party": "🎉", "romantic": "💕", "workout": "💪", "focus": "🧠"}.get(mood, "🎵")
            text = f"{emoji} **{mood.title()} Playlist**\n\n"
            for i, t in enumerate(tracks[:10], 1):
                dur = f"{t['duration'] // 60}:{t['duration'] % 60:02d}"
                text += f"**{i}.** `{t['title']}` – {t['artist']['name']} ({dur})\n"
            await msg.edit(text, parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:150]}`")

    # ═══════════════ T2I (Advanced with Fallback) ═══════════════
    @cmd('t2i', delete_cmd=False)
    async def t2i_cmd(event):
        prompt = get_args(event.message)
        if not prompt:
            await event.reply("❌ **Usage:** `.t2i <prompt>`\n\n**Example:** `.t2i a cute cat sitting on a table`")
            return

        status = await event.reply("🎨 **Generating image...**")

        models_to_try = ["flux", "flux-realism", "turbo"]
        attempt = 0
        max_attempts = 6
        img_data = None
        last_error = None

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            for model in models_to_try:
                if img_data:
                    break
                for seed_attempt in range(2):
                    attempt += 1
                    try:
                        encoded = url_quote(prompt)
                        seed = random.randint(1, 999999)
                        url = (f"https://image.pollinations.ai/prompt/{encoded}"
                               f"?nologo=true&enhance=true&width=1024&height=1024"
                               f"&model={model}&seed={seed}&safe=false")

                        async with session.get(url) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                if data and len(data) > 5000:
                                    if data[:3] == b'\xff\xd8\xff' or data[:8] == b'\x89PNG\r\n\x1a\n':
                                        img_data = data
                                        break
                                    else:
                                        last_error = "Invalid image data"
                                else:
                                    last_error = f"Empty image"
                            else:
                                last_error = f"HTTP {resp.status}"
                    except asyncio.TimeoutError:
                        last_error = "Timeout"
                    except Exception as e:
                        last_error = str(e)[:100]
                    await asyncio.sleep(1)

        try:
            if not img_data:
                await status.edit(f"❌ **Image generate nahi hui**\n\n**Reason:** `{last_error}`")
                return
            img_path = TEMP_DIR / f"t2i_{int(time.time())}_{random.randint(1,9999)}.jpg"
            img_path.write_bytes(img_data)
            try: await status.delete()
            except: pass
            await client.send_file(event.chat_id, str(img_path), reply_to=event.message.id)
            try: os.remove(img_path)
            except: pass
        except Exception as e:
            log.error(f"t2i error: {e}")
            try: await status.edit(f"❌ **Error:** `{str(e)[:200]}`")
            except: pass

    # ═══════════════ ANIMATIONS ═══════════════
    @cmd('animate', delete_cmd=True)
    async def animate_cmd(event):
        text = get_args(event.message)
        if not text:
            await event.reply("❌ Usage: `.animate <text>`"); return
        frames = ["🤍", "🦋", "🌸", "💖", "💝", "🎼", "😇", "✨"]
        msg = await send_anim(event, "🤍")
        try:
            for i in range(1, len(text) + 1):
                await msg.edit(text[:i] + " " + random.choice(frames))
                await asyncio.sleep(0.35)
            for _ in range(5):
                await msg.edit(f"🤍 {text} 🌸"); await asyncio.sleep(0.6)
                await msg.edit(f"💖 {text} 💝"); await asyncio.sleep(0.6)
                await msg.edit(f"🦋 {text} 🎼"); await asyncio.sleep(0.6)
            await msg.edit(f"💖 **{text}** 💖")
        except: pass

    @cmd('spinner', delete_cmd=True)
    async def spinner_cmd(event):
        args = get_args(event.message)
        try: secs = int(args) if args else 8
        except: secs = 8
        secs = min(max(secs, 3), 30)
        stages = [("🤍", "Starting up"), ("🦋", "Loading data"), ("🌸", "Processing"), ("💖", "Almost done"), ("💝", "Finalizing")]
        frames = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
        msg = await send_anim(event, "⠋ Starting...")
        try:
            total = secs * 4
            for i in range(total):
                stage_idx = min(int((i / total) * len(stages)), len(stages) - 1)
                emoji, label = stages[stage_idx]
                filled = int((i / total) * 12)
                bar = "█" * filled + "░" * (12 - filled)
                await msg.edit(f"{emoji} **{label}...**\n\n`{bar}` {int((i / total) * 100)}%\n{frames[i % len(frames)]}")
                await asyncio.sleep(0.35)
            await msg.edit("💝 **Complete!** 🎼")
            await asyncio.sleep(1.5)
        except: pass

    @cmd('loveu', delete_cmd=True)
    async def loveu_cmd(event):
        hearts = ["❤", "💓", "❣️"]
        target = event.reply_to_msg_id if event.reply_to_msg_id else None
        msg = None
        for h in hearts:
            try:
                if msg is None:
                    if target:
                        msg = await client.send_message(event.chat_id, h, reply_to=target)
                    else:
                        msg = await event.reply(h)
                else:
                    await msg.edit(h)
            except Exception as e:
                log.error(f"loveu heart error: {e}")
            await asyncio.sleep(2)
        final_box = ("╭═════════💜═╮\n  🇮 🇱‌🇴‌🇻‌🇪 🇾‌🇴‌🇺\n╰═💜═════════╯")
        try:
            if msg:
                await msg.edit(final_box)
            else:
                if target:
                    await client.send_message(event.chat_id, final_box, reply_to=target)
                else:
                    await event.reply(final_box)
        except:
            if target:
                await client.send_message(event.chat_id, final_box, reply_to=target)
            else:
                await event.reply(final_box)

    @cmd('loves', delete_cmd=True)
    async def loves_cmd(event):
        hearts = ["❤", "💓", "❣️"]
        target = event.reply_to_msg_id if event.reply_to_msg_id else None
        msg = None
        for h in hearts:
            try:
                if msg is None:
                    if target:
                        msg = await client.send_message(event.chat_id, h, reply_to=target)
                    else:
                        msg = await event.reply(h)
                else:
                    await msg.edit(h)
            except Exception as e:
                log.error(f"loves heart error: {e}")
            await asyncio.sleep(2)
        final_box = ("╭═════════💗═╮\n❤ 𝐈 𝐋𝐎𝐕𝐄 𝐘𝐎𝐔 𝐓𝐎𝐎 ❤\n╰═💗═════════╯")
        try:
            if msg:
                await msg.edit(final_box)
            else:
                if target:
                    await client.send_message(event.chat_id, final_box, reply_to=target)
                else:
                    await event.reply(final_box)
        except:
            if target:
                await client.send_message(event.chat_id, final_box, reply_to=target)
            else:
                await event.reply(final_box)

    @cmd('matrix', delete_cmd=True)
    async def matrix_cmd(event):
        args = get_args(event.message)
        try: secs = int(args) if args else 8
        except: secs = 8
        secs = min(max(secs, 3), 20)
        chars = "01アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホ"
        msg = await send_anim(event, "`01001000`")
        try:
            total = secs * 2
            for i in range(total):
                line = "".join(random.choice(chars) for _ in range(14))
                await msg.edit(f"🟢 `{line}`" if i % 4 == 0 else f"`{line}`")
                await asyncio.sleep(0.5)
            for _ in range(2):
                await msg.edit("`Wake up, Neo...`"); await asyncio.sleep(0.9)
                await msg.edit("`Follow the white rabbit 🐇`"); await asyncio.sleep(0.9)
            await msg.edit("🎭 **Welcome to 𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ World** 🎭")
        except: pass

    @cmd('hearts', delete_cmd=True)
    async def hearts_cmd(event):
        text = get_args(event.message) or "CipherElite"
        hearts = ["💖", "💗", "💓", "💞", "💕", "❤️", "🧡", "💛", "💚", "💙", "💜"]
        msg = await send_anim(event, f"💖 {text} 💖")
        try:
            for _ in range(15):
                h1, h2, h3 = random.choice(hearts), random.choice(hearts), random.choice(hearts)
                await msg.edit(f"{h1} {text} {h2}")
                await asyncio.sleep(0.4)
            positions = ["💕", "  💕", "    💕", "      💕", "    💕", "  💕"]
            for i in range(12):
                pos = positions[i % len(positions)]
                await msg.edit(f"{pos}\n  {text}\n{pos}")
                await asyncio.sleep(0.3)
            await msg.edit(f"💖💗💓 **{text}** 💓💗💖")
        except: pass

    @cmd('countdown', delete_cmd=True)
    async def countdown_cmd(event):
        args = get_args(event.message)
        try: n = int(args) if args else 5
        except: n = 5
        n = min(max(n, 1), 30)
        msg = await send_anim(event, f"⏱️ Starting countdown from {n}...")
        try:
            for i in range(n, 0, -1):
                await msg.edit(f"⏱️ **{i}**")
                await asyncio.sleep(1.2)
            await msg.edit("🎉 **GO!** 🎉")
        except: pass

    @cmd('wave', delete_cmd=True)
    async def wave_cmd(event):
        text = get_args(event.message)
        if not text:
            await event.reply("❌ Usage: `.wave <text>`"); return
        waves = ["🌊", "〰️", "🌸", "🦋", "🤍", "💖", "✨", "💫", "🌟", "💝", "🎼", "🎵"]
        msg = await send_anim(event, f"🌊 {text} 🌊")
        try:
            for _ in range(2):
                for i in range(len(text)):
                    wave_text = "".join(f"**{ch}**" if j == i else ch for j, ch in enumerate(text))
                    await msg.edit(f"🌊 {wave_text} 🌊", parse_mode='markdown')
                    await asyncio.sleep(0.15)
            for i in range(10):
                pad = "  " * (i % 4)
                w1 = waves[i % len(waves)]
                w2 = waves[(i + 5) % len(waves)]
                await msg.edit(f"{w1} {pad}{text}{pad} {w2}")
                await asyncio.sleep(0.4)
            for i in range(12):
                w1, w2, w3 = waves[i % len(waves)], waves[(i + 3) % len(waves)], waves[(i + 6) % len(waves)]
                await msg.edit(f"{w1} {w2} {text} {w3} {w1}")
                await asyncio.sleep(0.35)
            effects = [f"✨✨✨\n✨ **{text}** ✨\n✨✨✨", f"🌊〰️🌊\n〰️ **{text}** 〰️\n🌊〰️🌊", f"💖🌸💖\n🌸 **{text}** 🌸\n💖🌸💖", f"🦋✨🦋\n✨ **{text}** ✨\n🦋✨🦋"]
            for _ in range(3):
                for eff in effects:
                    await msg.edit(eff, parse_mode='markdown')
                    await asyncio.sleep(0.6)
            await msg.edit(f"╔═══════════════╗\n   ✨ **{text}** ✨\n╚═══════════════╝", parse_mode='markdown')
            await asyncio.sleep(1)
            await msg.edit(f"🦋🌸✨\n💫 **{text}** 💫\n✨🌸🦋", parse_mode='markdown')
        except: pass
                # ═══════════════ FANCY TEXT ═══════════════
    FANCY_WORDS = ["hi", "hey", "bye", "welcome", "ok", "yes", "wow", "cool", "omg", "lol", "love", "hate", "sad", "good", "bad", "best", "go", "fire", "boss"]

    def _make_banner(word, font="big"):
        try:
            import pyfiglet
            banner = pyfiglet.figlet_format(word.upper(), font=font)
            if len(banner) > 3500: banner = banner[:3500] + "..."
            return f"🦋✨\n`{banner}`\n✨🦋"
        except Exception as e:
            return f"❌ Error: `{str(e)[:100]}`"

    def _register_fancy_word(word):
        @cmd(word, delete_cmd=True)
        async def fancy_handler(event, w=word):
            await reply_to_target(event, _make_banner(w))

    for _word in FANCY_WORDS:
        _register_fancy_word(_word)

    @cmd('text', delete_cmd=True)
    async def text_cmd(event):
        text = get_args(event.message)
        if not text:
            await event.reply("❌ Usage: `.text <word>`"); return
        await reply_to_target(event, _make_banner(text))

    @cmd('banner', delete_cmd=True)
    async def banner_cmd(event):
        args = get_args(event.message).split()
        if not args:
            await event.reply("❌ Usage: `.banner <word> [font]`"); return
        text = args[0]
        font = args[1].lower() if len(args) > 1 else "big"
        fonts_available = ["big", "banner", "block", "doom", "slant", "standard", "digital"]
        if font not in fonts_available: font = "big"
        await reply_to_target(event, _make_banner(text, font))

    # ═══════════════ TEXT ART ═══════════════
    ART_TEXTS = {
        "gm": """⁣⋱ ⋮ ⋰
⋯ ◯ ⋯ ..Good Morning..
✿¸.•*¨`*•..¸✿✿¸.•*¨`*•..¸✿
..♥♥
. .((
. . .)
. ( (
████╗
████║
████╝ """,
        "gn": """∩――――∩
|| ∧ ﾍ        ||
||(* ´ ｰ`) Good Night💤
|ﾉ^⌒⌒づ`￣＼Sweet Dreams🦋
(    ノ        ⌒ ヽ ＼Take care💖
＼        ||￣￣￣￣￣||
     ＼,ﾉ||￣￣￣￣￣||""",
        "hbd": """✧✧🔥🔥🔥🔥🔥✧✧
✧╭┻┻┻┻┻┻┻┻┻┻╮✧
✧┃╱╲ 🍓╱╲🍒 ╱╲┃✧
╭┻━🍒━━━🍍━━━┻╮
┃╱╲╱╲ 🍈╱╲🍇 ╱╲ ┃
🎁━━━━━━━━━━━━🎁
✨💕Happy Birthday💕✨""",
        "wlc": """🌟。♥。✨。🍀
。🎁 。🎉。🌟
✨。＼｜／。🌺
WELCOME🌸🌸
💜。／｜＼。💎
。☀。 🌹。🌙。
🌟。 🦋。 🎶""",
        "hello2": """　　　/)＿/)☆ Hello~
    ／(๑^᎑^๑)っ ＼
／|￣∪￣ ￣ |＼／
  |＿＿_＿＿|／""",
        "love2": """　∧__∧
（｀•ω• )づ__∧
（つ     /( •ω•。)
  しーＪ   (nnノ)𝙄𝙩'𝙨 𝙤𝙠""",
        "heyy": """　▬▬.◙.▬▬
▂▄▄▓▄▄▂
◢◤ █▀▀████▄▄▄▄▄◢◤
█Heyy Listen   👀  █▀▀▀▀╬
◥████████◤
══╩══╩══
I will send a helicopter to pick you up!""",
        "gg": """🎩
😁
👕👍Great!
👖""",
        "flower": """　🌷🌸🌷🌸
    🌸🌷🌸🌷🌸
 Λ🌷🌸🌷🌸🌷
( ˘ ᵕ ˘🌷🌸🌷
ヽ  つ＼     ／
   UU   / 🎀 \\""",
        "gum": """૮₍ ˃ ⤙ ˂ ₎ა
./づᡕᠵ᠊ᡃ່࡚ࠢ࠘ ⸝່ࠡࠣ᠊߯᠆ࠣ࠘ᡁࠣ࠘᠊᠊ࠢ࠘~~~~♡""",
        "howdy": """█░█░█ █▀█ █░█░█
▀▄▀▄▀ █▄█ ▀▄▀▄▀""",
        "cool2": """█▀ █▀█ █▀█ █░
█▄ █▄█ █▄█ █▄""",
        "hug": """ଘ😇ଓ""",
        "hug2": """˚∧＿∧  　+        —̳͟͞͞💗
(  •‿• )つ  —̳͟͞͞ 💗         —̳͟͞͞💗 +
(つ　 <                —̳͟͞͞💗
｜　 _つ      +  —̳͟͞͞💗         —̳͟͞͞💗 ˚
`し´""",
        "thnx": """✨╭━━┳╮╭┳━━┳━┳┳┳┳━━╮💖
✨╰╮╭┫╰╯┃╭╮┃┃┃┃╭┫━━┫💖
✨╱┃┃┃╭╮┃┣┫┃┃┃┃╰╋━━┃💖
✨╱╰╯╰╯╰┻╯╰┻┻━┻┻┻━━╯💖""",
        "purpose": """　　👩
👨🌹ノl>
  |/       ▲`
  |┑      ∥
/""",
        "hii": """💗✉️✉️💗✉️✉️💗. 
💗✉️✉️💗✉️✉️💗
💗💗💗💗✉️✉️💗
💗✉️✉️💗✉️✉️💗
💗✉️✉️💗✉️✉️💗.""",
        "om": """╔═══╦═╗╔═╦═══╗😱
║╔═╗║║╚╝║║╔═╗║😱
║║─║║╔╗╔╗║║─╚╝😱
║║─║║║║║║║║╔═╗😱
║╚═╝║║║║║║╚╩═║😱
╚═══╩╝╚╝╚╩═══╝😱 """,
        "sm": """🍀☺🍀☺🍀☺
I LOVE ♥ YOUR
╔╗╔╦╗╦ ╦   ╦═╗
╚╗║║║║ ║   ╠╣
╚╝╝╝╝╩ ╩╝╩═╝
😄🎀😄🎀😄🎀 """,
        "il": """╔══╗....<3
╚╗╔╝..('\\../')
╔╝╚╗..( •.• )
╚══╝..(,,)(,,)
╔╗╔═╦╦╦═╗ ╔╗╔╗
║╚╣║║║║╩╣ ║╚╝║
╚═╩═╩═╩═╝ ╚══╝ """
    }

    def _register_art(key, art):
        @cmd(key, delete_cmd=True)
        async def art_handler(event, a=art):
            await reply_to_target(event, a)

    for _key, _art in ART_TEXTS.items():
        _register_art(_key, _art)

    # ═══════════════ PUBLIC FEATURES ═══════════════
    @cmd('weather', owner_only=True, delete_cmd=False)
    async def weather_cmd(event):
        city = get_args(event.message)
        if not city:
            await event.reply("❌ **Usage:** `.weather city`"); return
        try:
            city = city.strip().lower()
            if city == 'bihar': city = 'patna'
            url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        city_name = data.get('name', city.title())
                        country = data.get('sys', {}).get('country', '')
                        temp = data['main']['temp']
                        feels_like = data['main']['feels_like']
                        desc = data['weather'][0]['description'].title()
                        humidity = data['main']['humidity']
                        wind = data['wind']['speed']
                        pressure = data['main']['pressure']
                        emoji_map = {'clear': '☀️', 'clouds': '☁️', 'rain': '🌧️', 'snow': '❄️', 'thunderstorm': '⛈️', 'drizzle': '🌦️', 'mist': '🌫️', 'smoke': '💨', 'haze': '🌫️'}
                        emoji = '🌤️'
                        for key, em in emoji_map.items():
                            if key in desc.lower(): emoji = em; break
                        msg = (f"**{emoji} Weather Report: {city_name}, {country}**\n📅 {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n🌡️ **Temperature:** {temp}°C\n🌡️ **Feels Like:** {feels_like}°C\n📝 **Condition:** {desc}\n💧 **Humidity:** {humidity}%\n🌬️ **Wind Speed:** {wind} m/s\n📊 **Pressure:** {pressure} hPa")
                        await event.reply(msg)
                    else:
                        await event.reply(f"❌ City '{city}' not found.")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)[:100]}")

    @cmd('news', owner_only=True, delete_cmd=False)
    async def news_cmd(event):
        query = get_args(event.message)
        try:
            if query:
                url = f"https://newsapi.org/v2/everything?q={query}&apiKey={NEWS_API_KEY}&language=en&sortBy=publishedAt&pageSize=5"
            else:
                url = f"https://newsapi.org/v2/top-headlines?country=in&apiKey={NEWS_API_KEY}&pageSize=5"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        articles = data.get('articles', [])
                        if not articles:
                            await event.reply(f"❌ No news found."); return
                        articles = articles[:5]
                        location = query.title() if query else "India"
                        current_time = datetime.now().strftime("%d %b %Y, %I:%M %p")
                        msg = f"**📰 Latest News - {location}**\n🕐 {current_time}\n" + "═" * 30 + "\n\n"
                        for i, article in enumerate(articles, 1):
                            title = article.get('title', 'No title')
                            source = article.get('source', {}).get('name', 'Unknown')
                            published = article.get('publishedAt', '')
                            time_str = ""
                            if published:
                                try:
                                    pub_time = datetime.fromisoformat(published.replace('Z', '+00:00'))
                                    time_str = pub_time.strftime("%I:%M %p")
                                except: pass
                            description = article.get('description', '')
                            if description and len(description) > 120: description = description[:117] + '...'
                            msg += f"**{i}. {title}**\n📰 {source}"
                            if time_str: msg += f" ⏰ {time_str}"
                            msg += "\n"
                            if description: msg += f"📝 {description}\n"
                            msg += "\n" + "─" * 25 + "\n\n"
                        await event.reply(msg)
                    else:
                        await event.reply(f"❌ News API error: {resp.status}")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)[:100]}")

    @cmd('azan', owner_only=True, delete_cmd=False)
    async def azan_cmd(event):
        city = get_args(event.message)
        if not city:
            await event.reply("❌ **Usage:** `.azan city`"); return
        try:
            url = f"http://api.aladhan.com/v1/timingsByCity?city={city}&country=India&method=1"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()
                    if data.get('code') == 200:
                        t = data['data']['timings']
                        date = data['data']['date']['readable']
                        def convert_to_12h(time_str):
                            try:
                                if ':' in time_str:
                                    parts = time_str.split(':')
                                    hour = int(parts[0])
                                    minute = int(parts[1])
                                    ampm = "AM" if hour < 12 else "PM"
                                    hour = hour % 12
                                    if hour == 0: hour = 12
                                    return f"{hour}:{minute:02d} {ampm}"
                                return time_str
                            except: return time_str
                        msg = (f"**🕌 Prayer Times: {city.title()}**\n📅 {date}\n\n🌅 **Fajr:** {convert_to_12h(t['Fajr'])}\n☀️ **Sunrise:** {convert_to_12h(t['Sunrise'])}\n🌙 **Dhuhr:** {convert_to_12h(t['Dhuhr'])}\n🌤️ **Asr:** {convert_to_12h(t['Asr'])}\n🌅 **Maghrib:** {convert_to_12h(t['Maghrib'])}\n🌌 **Isha:** {convert_to_12h(t['Isha'])}")
                        await event.reply(msg)
                    else:
                        await event.reply(f"❌ City '{city}' not found.")
        except Exception as e:
            await event.reply(f"❌ Error: {str(e)[:100]}")

    @cmd('play', owner_only=True, delete_cmd=False)
    async def play_cmd(event):
        query = get_args(event.message)
        if not query:
            await event.reply("❌ **Usage:** `.play song_name`"); return
        status_msg = await event.reply(f"🔍 Searching for '{query}'...")
        try:
            search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
            async with aiohttp.ClientSession() as session:
                async with session.get(search_url) as resp:
                    html = await resp.text()
                    video_ids = re.findall(r'"videoId":"([^"]+)"', html)
                    if not video_ids:
                        await status_msg.edit(f"❌ No results found."); return
                    video_id = video_ids[0]
                    video_url = f"https://youtube.com/watch?v={video_id}"
                    video_titles = re.findall(r'"title":"([^"]+)"', html)
                    title = video_titles[0].replace('\\u0026', '&').replace('\\u003c', '<').replace('\\u003e', '>') if video_titles else query
                    title = re.sub(r'[^\w\s\-\.\&\(\)]', '', title)
                    if len(title) > 60: title = title[:57] + '...'
                    duration = re.findall(r'"lengthSeconds":"([^"]+)"', html)
                    duration_str = ""
                    if duration:
                        try:
                            secs = int(duration[0])
                            mins = secs // 60
                            secs = secs % 60
                            duration_str = f"⏱️ {mins:02d}:{secs:02d}"
                        except: pass
                    channels = re.findall(r'"ownerChannelName":"([^"]+)"', html)
                    channel = channels[0] if channels else "YouTube"
                    await status_msg.delete()
                    msg = (f"**🎵 {title}**\n\n📺 **Channel:** {channel}\n{duration_str}\n\n🔗 **Watch on YouTube:**\n\n[{title}]({video_url})\n\n---\n💡 Tap the link above to open in Telegram")
                    await event.reply(msg, parse_mode='markdown', link_preview=True)
        except Exception as e:
            await status_msg.edit(f"❌ Error: {str(e)[:100]}")

    # ═══════════════ IMAGE TOOLS ═══════════════
    @cmd('sticker', delete_cmd=False)
    async def sticker_cmd(event):
        reply = await event.get_reply_message()
        if not reply or not reply.photo:
            await event.reply("❌ Reply to an image!"); return
        msg = await event.reply("🎨 Creating sticker...")
        try:
            from PIL import Image
            img_path = await reply.download_media(file=str(TEMP_DIR))
            img = Image.open(img_path).convert("RGBA")
            img.thumbnail((512, 512), Image.LANCZOS)
            canvas = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
            offset = ((512 - img.width) // 2, (512 - img.height) // 2)
            canvas.paste(img, offset, img)
            sticker_path = str(TEMP_DIR / "sticker.webp")
            canvas.save(sticker_path, "WEBP")
            await client.send_file(event.chat_id, sticker_path, force_document=False)
            try: os.remove(img_path)
            except: pass
            try: os.remove(sticker_path)
            except: pass
            await msg.delete()
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:200]}`")

    @cmd('toimg', delete_cmd=False)
    async def toimg_cmd(event):
        reply = await event.get_reply_message()
        if not reply or not reply.sticker:
            await event.reply("❌ Reply to a sticker!"); return
        msg = await event.reply("🖼️ Converting...")
        try:
            from PIL import Image
            sticker_path = await reply.download_media(file=str(TEMP_DIR))
            img = Image.open(sticker_path).convert("RGBA")
            img_path = str(TEMP_DIR / "from_sticker.png")
            img.save(img_path, "PNG")
            await client.send_file(event.chat_id, img_path, caption="🖼️ **Sticker → Image**")
            try: os.remove(sticker_path)
            except: pass
            try: os.remove(img_path)
            except: pass
            await msg.delete()
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:200]}`")

    @cmd('blur', delete_cmd=False)
    async def blur_cmd(event):
        reply = await event.get_reply_message()
        if not reply or not reply.photo:
            await event.reply("❌ Reply to an image!"); return
        msg = await event.reply("🎨 Blurring...")
        try:
            from PIL import Image, ImageFilter
            img_path = await reply.download_media(file=str(TEMP_DIR))
            img = Image.open(img_path)
            blurred = img.filter(ImageFilter.GaussianBlur(radius=15))
            output = str(TEMP_DIR / "blurred.jpg")
            blurred.save(output, "JPEG", quality=85)
            await client.send_file(event.chat_id, output, caption="🎨 **Blurred**")
            try: os.remove(img_path)
            except: pass
            try: os.remove(output)
            except: pass
            await msg.delete()
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:200]}`")

    @cmd('img2pdf', delete_cmd=False)
    async def img2pdf_cmd(event):
        reply = await event.get_reply_message()
        if not reply or not reply.photo:
            await event.reply("❌ Reply to an image!"); return
        msg = await event.reply("📄 Converting to PDF...")
        try:
            import img2pdf
            img_path = await reply.download_media(file=str(TEMP_DIR))
            pdf_path = str(TEMP_DIR / "output.pdf")
            with open(pdf_path, "wb") as f:
                f.write(img2pdf.convert(img_path))
            await client.send_file(event.chat_id, pdf_path, caption="📄 **PDF Created**")
            try: os.remove(img_path)
            except: pass
            try: os.remove(pdf_path)
            except: pass
            await msg.delete()
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:200]}`")

    @cmd('forecast', owner_only=True, delete_cmd=False)
    async def forecast_cmd(event):
        city = get_args(event.message)
        if not city:
            await event.reply("❌ Usage: `.forecast <city>`"); return
        msg = await event.reply("🌤️ Loading forecast...")
        try:
            url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={WEATHER_API_KEY}&units=metric&cnt=40"
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
                    if r.status != 200:
                        await msg.edit("❌ City not found"); return
                    data = await r.json()
            text = f"🌤️ **5-Day Forecast: {data['city']['name']}**\n\n"
            daily = {}
            for item in data['list']:
                date = item['dt_txt'].split()[0]
                if date not in daily: daily[date] = item
            for date, item in list(daily.items())[:5]:
                temp = item['main']['temp']
                desc = item['weather'][0]['description'].title()
                emoji_map = {'clear': '☀️', 'clouds': '☁️', 'rain': '🌧️', 'snow': '❄️', 'thunderstorm': '⛈️', 'drizzle': '🌦️', 'mist': '🌫️'}
                em = '🌤️'
                for k, v in emoji_map.items():
                    if k in desc.lower(): em = v; break
                text += f"**{date}** {em} {temp}°C — {desc}\n"
            await msg.edit(text, parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:200]}`")

    @cmd('aqi', owner_only=True, delete_cmd=False)
    async def aqi_cmd(event):
        city = get_args(event.message)
        if not city:
            await event.reply("❌ Usage: `.aqi <city>`"); return
        msg = await event.reply("🌫️ Loading AQI...")
        try:
            geo_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&limit=1&appid={WEATHER_API_KEY}"
            async with aiohttp.ClientSession() as s:
                async with s.get(geo_url) as r:
                    geo = await r.json()
            if not geo:
                await msg.edit("❌ City not found"); return
            lat, lon = geo[0]['lat'], geo[0]['lon']
            aqi_url = f"http://api.openweathermap.org/data/2.5/air_pollution?lat={lat}&lon={lon}&appid={WEATHER_API_KEY}"
            async with aiohttp.ClientSession() as s:
                async with s.get(aqi_url) as r:
                    data = await r.json()
            aqi = data['list'][0]['main']['aqi']
            components = data['list'][0]['components']
            levels = {1: "🟢 Good", 2: "🟡 Fair", 3: "🟠 Moderate", 4: "🔴 Poor", 5: "⚫ Very Poor"}
            text = (f"🌫️ **AQI: {city.title()}**\n\n**Air Quality:** {levels.get(aqi, 'Unknown')} ({aqi}/5)\n\n**Pollutants:**\n• PM2.5: {components.get('pm2_5', 0)} µg/m³\n• PM10: {components.get('pm10', 0)} µg/m³\n• NO₂: {components.get('no2', 0)} µg/m³\n• O₃: {components.get('o3', 0)} µg/m³\n• CO: {components.get('co', 0)} µg/m³")
            await msg.edit(text, parse_mode='markdown')
        except Exception as e:
            await msg.edit(f"❌ Error: `{str(e)[:200]}`")


# ═══════════════ STARTUP ═══════════════
async def main():
    asyncio.create_task(cleanup_temp_loop())
    db_ok = await init_database()
    if db_ok:
        await load_all()
        async def delayed_cleanup():
            await asyncio.sleep(5)
            await cleanup_old_command_logs()
        asyncio.create_task(delayed_cleanup())
        asyncio.create_task(auto_cleanup_loop())
    else:
        log.warning("⚠️ SQLite database unavailable — data will not be saved")

    asyncio.create_task(get_ghost_image_path())

    me = await bot.get_me()
    log.info(f"✅ Bot: @{me.username}")
    log.info(f"👑 Owner: {OWNER_ID}")

    restored = 0
    failed = 0
    for uid, u in list(users_db.items()):
        if u.get("logged_in") and u.get("session_string"):
            if uid in blocked_users or not is_bot_approved(uid):
                continue
            try:
                await start_ub(uid, u["session_string"])
                restored += 1
            except Exception as e:
                failed += 1
                log.error(f"Restore {uid}: {e}")

    log.info(f"✅ Restored {restored} userbot(s)")
    if failed:
        log.warning(f"⚠️ Failed to restore: {failed} userbot(s)")
    log.info(f"🚫 Blocked users: {len(blocked_users)}")
    log.info(f"📊 Command usage tracked: {len(command_usage_db)} users")
    log.info("⚙️ Type .help in Telegram.")

    # Start the health endpoint only after startup has succeeded, and always
    # cancel/await it when the Telegram client disconnects. This prevents
    # "Task was destroyed but it is pending" during Render shutdown/redeploy.
    health_task = asyncio.create_task(health_server(), name="health_server")
    try:
        await bot.run_until_disconnected()
    finally:
        health_task.cancel()
        await asyncio.gather(health_task, return_exceptions=True)


if __name__ == '__main__':
    print("=" * 45)
    print("  𝕾𝕳𝕬𝕳𝕹𝕬𝖂𝕬𝖅 ⚡ USERBOT")
    print("=" * 45)
    try:
        bot.loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("🛑 Stopped.")
    except Exception as e:
        log.error(f"Fatal: {e}")
