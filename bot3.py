"""
CLONIX JARVIS - Gemini-powered Telegram assistant module.

Keep bot3.py beside your existing bot.py.
This module does NOT create or start a Telegram client.

Render environment variables:
  GEMINI_API_KEY  (required)
  GEMINI_MODEL    (optional; default: gemini-2.5-flash)

Python dependencies: telethon, requests
"""

import os
import re
import sqlite3
import asyncio
from pathlib import Path
from datetime import datetime, timezone

import requests
from telethon import events

DATA_DIR = Path(os.getenv("JARVIS_DATA_DIR", "./clonix_data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "jarvis_memory.sqlite3"

SYSTEM_PROMPT = """
You are JARVIS, a friendly and capable assistant inside a Telegram bot.
Speak naturally and clearly, usually in the language and style used by the person.
Understand the user's intent before answering. Be useful, calm, concise and honest.
You can explain, plan, summarize, brainstorm and help with ordinary tasks.
You cannot claim to have performed actions outside the tools explicitly available.
Never claim you accessed a phone, account, private messages, files, or server unless
a provided tool actually did so. Do not request passwords, login codes, bot tokens,
API keys, or session files. Do not help with covert surveillance, account intrusion,
spam, harassment, or bypassing security.
For real-world or irreversible actions, explain what would happen and ask for
confirmation. This version can save and track simple tasks, but cannot execute
external actions such as sending messages, changing accounts, or controlling a phone.
"""

def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_memory (
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jarvis_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn

def _remember(chat_id, role, content):
    conn = _db()
    conn.execute(
        "INSERT INTO chat_memory(chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content[:4000], datetime.now(timezone.utc).isoformat())
    )
    conn.execute("""
        DELETE FROM chat_memory
        WHERE chat_id = ? AND rowid NOT IN (
            SELECT rowid FROM chat_memory WHERE chat_id = ?
            ORDER BY rowid DESC LIMIT 12
        )
    """, (chat_id, chat_id))
    conn.commit()
    conn.close()

def _history(chat_id):
    conn = _db()
    rows = conn.execute(
        "SELECT role, content FROM chat_memory WHERE chat_id = ? ORDER BY rowid DESC LIMIT 12",
        (chat_id,)
    ).fetchall()
    conn.close()
    return [{"role": role, "parts": [{"text": content}]} for role, content in reversed(rows)]

def _ask_gemini(chat_id, prompt):
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return (
            "JARVIS ko Gemini se connect karne ke liye Render Environment mein "
            "GEMINI_API_KEY set karo. API key GitHub ya Telegram par share mat karna."
        )

    model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    contents = _history(chat_id)
    contents.append({"role": "user", "parts": [{"text": prompt[:4000]}]})

    response = requests.post(
        url,
        headers={
            "x-goog-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": contents,
            "generationConfig": {
                "temperature": 0.7,
                "maxOutputTokens": 700,
            },
        },
        timeout=45,
    )

    if response.status_code != 200:
        try:
            err = response.json().get("error", {})
            detail = err.get("message", "Gemini API request failed")
            status = err.get("status", "")
        except Exception:
            detail, status = "Gemini API request failed", ""
        if response.status_code == 429:
            return "Gemini ki free usage limit abhi poori ho gayi hai. Thodi der baad try karo."
        return f"Gemini error ({response.status_code} {status}): {detail[:220]}"

    data = response.json()
    candidates = data.get("candidates") or []
    if not candidates:
        return "Gemini ne abhi response nahi diya. Thodi der baad try karo."
    parts = candidates[0].get("content", {}).get("parts", [])
    answer = "\n".join(part.get("text", "") for part in parts if part.get("text"))
    return answer.strip() or "Gemini se empty response mila."

def _add_task(owner_id, task):
    conn = _db()
    cur = conn.execute(
        "INSERT INTO jarvis_tasks(owner_id, task, created_at) VALUES (?, ?, ?)",
        (owner_id, task[:500], datetime.now(timezone.utc).isoformat())
    )
    task_id = cur.lastrowid
    conn.commit()
    conn.close()
    return task_id

def _list_tasks(owner_id):
    conn = _db()
    rows = conn.execute(
        "SELECT id, task, status FROM jarvis_tasks WHERE owner_id = ? ORDER BY id DESC LIMIT 20",
        (owner_id,)
    ).fetchall()
    conn.close()
    return rows

def _complete_task(owner_id, task_id):
    conn = _db()
    cur = conn.execute(
        "UPDATE jarvis_tasks SET status='done' WHERE id=? AND owner_id=?",
        (task_id, owner_id)
    )
    conn.commit()
    changed = cur.rowcount
    conn.close()
    return changed > 0

def register_jarvis(client, owner_id):
    """Register JARVIS handlers on an already-created Telethon client."""
    if not owner_id:
        raise ValueError("OWNER_ID is required for JARVIS task controls")

    @client.on(events.NewMessage(incoming=True))
    async def jarvis_handler(event):
        text = (event.raw_text or "").strip()
        if not text:
            return

        # JARVIS replies only when a message begins with "Jarvis".
        match = re.match(r"(?is)^jarvis(?:[\s,:-]+|$)(.*)$", text)
        if not match:
            return

        prompt = (match.group(1) or "").strip()
        sender_id = event.sender_id
        is_owner = sender_id == int(owner_id)

        if is_owner and prompt.lower().startswith("task add "):
            task = prompt[9:].strip()
            if not task:
                await event.reply("Task likho: `Jarvis task add NEET Biology revise`")
                return
            task_id = await asyncio.to_thread(_add_task, int(owner_id), task)
            await event.reply(f"Task #{task_id} save ho gaya. Main ise track kar sakta hoon.")
            return

        if is_owner and prompt.lower() in ("tasks", "task list", "show tasks"):
            rows = await asyncio.to_thread(_list_tasks, int(owner_id))
            if not rows:
                await event.reply("Abhi koi task saved nahi hai.")
            else:
                lines = [f"#{i} [{status}] {task}" for i, task, status in rows]
                await event.reply("**Tumhare tasks:**\n" + "\n".join(lines))
            return

        done_match = re.match(r"(?i)^task done\s+(\d+)$", prompt)
        if is_owner and done_match:
            ok = await asyncio.to_thread(
                _complete_task, int(owner_id), int(done_match.group(1))
            )
            await event.reply("Task complete mark kar diya." if ok else "Ye task ID nahi mila.")
            return

        if not prompt:
            await event.reply(
                "Haan, bolo! Main sun raha hoon.\n"
                "Examples:\n"
                "`Jarvis aaj ka study plan banao`\n"
                "`Jarvis task add Physics revision`\n"
                "`Jarvis tasks`"
            )
            return

        await event.reply("Soch raha hoon…")
        try:
            answer = await asyncio.to_thread(_ask_gemini, event.chat_id, prompt)
            _remember(event.chat_id, "user", prompt)
            _remember(event.chat_id, "model", answer)
            await event.reply(answer[:4000])
        except requests.Timeout:
            await event.reply("Gemini ko response dene mein time lag raha hai. Dobara try karo.")
        except Exception as exc:
            await event.reply(f"JARVIS error: {type(exc).__name__}: {str(exc)[:180]}")

    print("JARVIS Gemini handlers registered.")
