#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEET & JEE Quiz Bot  (Python + Telethon + SQLite + Groq)
=========================================================

ENVIRONMENT VARIABLES
  API_ID, API_HASH   : from https://my.telegram.org
  QUIZ_BOT_TOKEN     : token of THIS quiz bot from @BotFather (falls back to BOT_TOKEN).
                       Use a different token than your existing bot!
  OWNER_ID           : your numeric Telegram user id (comma separated for several owners)
  GROQ_API_KEY       : from https://console.groq.com
  GROQ_MODEL         : optional, default "llama-3.3-70b-versatile"
  DATA_DIR           : optional, folder for DB + session + books.
                       Default: /var/data if it exists (Render Persistent Disk), else ./data
  PORT               : set automatically by Render (tiny health server is started)
  KEEP_FILES         : "1" to keep uploaded book files on disk (default "0": only extracted
                       text is kept in the DB, which is enough for the library)
  CHUNK_CHARS        : optional chunk size, default 3500
  GEN_PER_CHUNK      : questions generated per theory chunk, default 3
  GROQ_MIN_INTERVAL  : seconds between Groq calls, default 2.0

START COMMAND (Render):  python bot5.py
"""
import asyncio
import contextlib
import hashlib
import html
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import httpx
from telethon import Button, TelegramClient, events, functions, types

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("quizbot")


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        print(f"ERROR: environment variable {name} is required.", file=sys.stderr)
        sys.exit(1)
    return v


API_ID = int(env("API_ID", required=True))
API_HASH = env("API_HASH", required=True)
BOT_TOKEN = os.environ.get("QUIZ_BOT_TOKEN") or env("BOT_TOKEN", required=True)
OWNER_IDS = {int(x) for x in re.split(r"[,\s]+", env("OWNER_ID", "")) if x.strip().lstrip("-").isdigit()}
GROQ_API_KEY = env("GROQ_API_KEY", "")
GROQ_MODEL = env("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MIN_INTERVAL = float(env("GROQ_MIN_INTERVAL", "2.0"))
KEEP_FILES = env("KEEP_FILES", "0") == "1"
CHUNK_CHARS = int(env("CHUNK_CHARS", "3500"))
OVERLAP = 300
GEN_PER_CHUNK = int(env("GEN_PER_CHUNK", "3"))

DATA_DIR = env("DATA_DIR") or ("/var/data" if os.path.isdir("/var/data") else "./data")
BOOKS_DIR = os.path.join(DATA_DIR, "books")
os.makedirs(BOOKS_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "bot5_quiz.db")
SESSION_PATH = os.path.join(DATA_DIR, "bot5_quiz_session")

QUESTION_TIME = 60
MARKS_CORRECT = 4
MARKS_WRONG = -1

SUBJECTS = {"NEET": ["Physics", "Chemistry", "Biology"], "JEE": ["Physics", "Chemistry", "Mathematics"]}
SUBJECT_ALIASES = {
    "physics": "Physics", "chemistry": "Chemistry", "biology": "Biology", "botany": "Biology",
    "zoology": "Biology", "mathematics": "Mathematics", "maths": "Mathematics", "math": "Mathematics",
}
MOCK_PLAN = {
    "NEET": {"full": {"Physics": 45, "Chemistry": 45, "Biology": 90},
             "mini": {"Physics": 10, "Chemistry": 10, "Biology": 10}},
    "JEE": {"full": {"Physics": 25, "Chemistry": 25, "Mathematics": 25},
            "mini": {"Physics": 10, "Chemistry": 10, "Mathematics": 10}},
}

asyncio.set_event_loop(asyncio.new_event_loop())
client = TelegramClient(SESSION_PATH, API_ID, API_HASH)
esc = html.escape

# ----------------------------------------------------------------------------
# SMALL HELPERS
# ----------------------------------------------------------------------------
_norm_re = re.compile(r"[^a-z0-9]+")
NON_EN = re.compile(r"[\u0900-\u0DFF\u0600-\u06FF]")  # Devanagari & other Indic / Arabic scripts
LABEL_RE = re.compile(r"^\s*(?:\(?[A-Da-d1-4]\)\s*|\(?[A-Da-d1-4][\.\:]\s+)")
ANS_CUE = re.compile(r"\b(ans|answer|key|sol|solution|explanation|correct)\b|\bans[\.\:\-]", re.I)


def now() -> int:
    return int(time.time())


def norm(s: str) -> str:
    return _norm_re.sub("", (s or "").lower())


def clean(s) -> str:
    if s is None:
        return ""
    s = str(s).replace("\x00", "")
    return re.sub(r"[ \t]+", " ", re.sub(r"\s*\n\s*", " ", s)).strip()


def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def strip_label(t: str) -> str:
    return LABEL_RE.sub("", t, count=1).strip()


def covered(text: str, src_norm: str, thresh: float = 0.9) -> bool:
    """True if `text` really occurs in the source (tolerant to tiny extraction noise)."""
    n = norm(text)
    if not n:
        return False
    if n in src_norm:
        return True
    if len(n) < 8:
        return False
    hits = total = 0
    for i in range(0, len(n) - 5, 3):
        total += 1
        if n[i:i + 6] in src_norm:
            hits += 1
    return total > 0 and hits / total >= thresh


def clean_chapter(c) -> Optional[str]:
    if not c or not isinstance(c, str):
        return None
    c = re.sub(r"\s+", " ", c).strip(" .:-–—#*")
    if len(c) < 3 or c.lower() in ("unknown", "n/a", "none", "null", "general", "chapter"):
        return None
    return c[:70]


def canon_subject(s) -> Optional[str]:
    return SUBJECT_ALIASES.get(str(s or "").strip().lower())


def dkey_for(stem: str, opts: dict) -> str:
    n = norm(stem)
    base = n if len(n) >= 40 else n + "|" + "|".join(norm(opts[l]) for l in "ABCD")
    return hashlib.sha1(base.encode()).hexdigest()


def pct(a, b) -> str:
    return f"{(100.0 * a / b):.1f}%" if b else "0.0%"


def is_owner(uid) -> bool:
    return uid in OWNER_IDS


def human_size(n) -> str:
    n = float(n or 0)
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.1f} {u}"
        n /= 1024


# ----------------------------------------------------------------------------
# DATABASE (SQLite)
# ----------------------------------------------------------------------------
_db_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS books(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT, kind TEXT, exam TEXT, subject TEXT,
  file_name TEXT, file_hash TEXT UNIQUE, file_size INTEGER, stored_path TEXT,
  status TEXT, status_msg TEXT, chunk_count INTEGER DEFAULT 0, chunks_done INTEGER DEFAULT 0,
  dup_count INTEGER DEFAULT 0, rejected_count INTEGER DEFAULT 0,
  uploaded_by INTEGER, created_at INTEGER);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  idx INTEGER, page_from INTEGER, page_to INTEGER, text TEXT, chapter TEXT,
  done INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_chunks_book ON chunks(book_id, idx);
CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER REFERENCES books(id) ON DELETE CASCADE,
  mode TEXT, exam TEXT, subject TEXT, chapter TEXT, difficulty TEXT, qno INTEGER,
  question TEXT, opt_a TEXT, opt_b TEXT, opt_c TEXT, opt_d TEXT,
  answer TEXT, explanation TEXT, expl_source TEXT, dkey TEXT, created_at INTEGER,
  UNIQUE(mode, exam, dkey));
CREATE INDEX IF NOT EXISTS ix_q_filter ON questions(exam, subject, mode, chapter);
CREATE INDEX IF NOT EXISTS ix_q_book ON questions(book_id);
CREATE TABLE IF NOT EXISTS answer_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER REFERENCES books(id) ON DELETE CASCADE, qno INTEGER, answer TEXT);
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, name TEXT, username TEXT, joined_at INTEGER);
CREATE TABLE IF NOT EXISTS attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, question_id INTEGER, exam TEXT, subject TEXT,
  chosen TEXT, status TEXT, marks INTEGER, taken REAL, kind TEXT, ts INTEGER);
CREATE INDEX IF NOT EXISTS ix_att_user ON attempts(user_id, question_id);
CREATE INDEX IF NOT EXISTS ix_att_ts ON attempts(ts);
CREATE TABLE IF NOT EXISTS sessions_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER, kind TEXT, title TEXT, total INTEGER, correct INTEGER, wrong INTEGER,
  skipped INTEGER, marks INTEGER, max_marks INTEGER, started_at INTEGER, finished_at INTEGER);
"""


def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
    return _conn


def init_db():
    with _db_lock:
        db().executescript(SCHEMA)


def db_all(sql, params=()):
    with _db_lock:
        return [dict(r) for r in db().execute(sql, params).fetchall()]


def db_one(sql, params=()):
    with _db_lock:
        r = db().execute(sql, params).fetchone()
        return dict(r) if r else None


def db_run(sql, params=()):
    with _db_lock:
        cur = db().execute(sql, params)
        return cur.lastrowid


def db_many(sql, rows):
    with _db_lock:
        c = db()
        c.execute("BEGIN")
        try:
            c.executemany(sql, rows)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise


def get_book(bid):
    return db_one("SELECT * FROM books WHERE id=?", (bid,))


def set_status(bid, status, msg=None):
    db_run("UPDATE books SET status=?, status_msg=? WHERE id=?", (status, msg, bid))


# ----------------------------------------------------------------------------
# GROQ
# ----------------------------------------------------------------------------
class GroqDailyLimit(Exception):
    pass


HTTP: Optional[httpx.AsyncClient] = None
GROQ_SEM: Optional[asyncio.Semaphore] = None
_last_call = [0.0]


async def groq_chat(system, user, json_mode=True, max_tokens=3000, temperature=0.2) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set")
    payload = {
        "model": GROQ_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(7):
        async with GROQ_SEM:
            wait = GROQ_MIN_INTERVAL - (time.time() - _last_call[0])
            if wait > 0:
                await asyncio.sleep(wait)
            _last_call[0] = time.time()
            try:
                r = await HTTP.post(GROQ_URL, headers=headers, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_err = e
                await asyncio.sleep(3 * (attempt + 1))
                continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
        txt = r.text[:300]
        low = txt.lower()
        if r.status_code == 429:
            if "per day" in low or "tpd" in low or "rpd" in low:
                raise GroqDailyLimit(txt)
            ra = r.headers.get("retry-after", "")
            delay = float(ra) if re.match(r"^[\d.]+$", ra or "") else 8 * (attempt + 1)
            last_err = txt
            await asyncio.sleep(min(delay + 1, 90))
            continue
        if r.status_code in (500, 502, 503, 504) or (r.status_code == 400 and "json" in low):
            last_err = txt
            await asyncio.sleep(3 * (attempt + 1))
            continue
        raise RuntimeError(f"Groq error {r.status_code}: {txt}")
    raise RuntimeError(f"Groq failed after retries: {last_err}")


def parse_json(s: str) -> dict:
    try:
        d = json.loads(s)
    except Exception:
        m = re.search(r"\{.*\}", s or "", re.S)
        if not m:
            return {}
        try:
            d = json.loads(m.group(0))
        except Exception:
            return {}
    return d if isinstance(d, dict) else {}


ORIG_SYS = """You are a precise data-extraction engine for Indian NEET/JEE exam books. From the given book excerpt, extract multiple-choice questions EXACTLY as printed.
STRICT RULES:
1. Copy each question stem and each option VERBATIM (character for character). You may only fix line-break hyphenation and extra whitespace. NEVER paraphrase, translate, shorten, complete, correct or invent any text.
2. Do NOT put the question number or option labels ((a), A), 1. etc.) inside the text. Put the printed question number in "qno" (integer or null).
3. Only extract questions that have exactly four options. Map them in printed order to A, B, C, D.
4. "answer": give A/B/C/D ONLY if the excerpt itself explicitly states the correct answer for that specific question (answer line, solution, key next to it). If the printed key uses 1-4, map 1->A, 2->B, 3->C, 4->D. If it is not explicitly in the excerpt use null. NEVER guess and NEVER solve the question yourself.
5. "explanation": copy the book's own printed solution/explanation verbatim if it is printed with the question, else null.
6. Skip any question written in Hindi or any non-English language, and any question that depends on a figure/diagram/table that is not present as text.
7. If the excerpt contains a standalone answer key (like "1. (b) 2. (c) ..."), output those entries in "answer_key" as {"qno": int, "answer": "A|B|C|D"}.
8. "subject" must be one of the allowed subjects given by the user. "chapter" is the chapter/topic this excerpt belongs to (use the hint if the excerpt shows no new chapter heading).
Return ONLY a JSON object:
{"chapter": string|null, "questions": [{"qno": int|null, "subject": string, "chapter": string, "question": string, "options": {"A": string, "B": string, "C": string, "D": string}, "answer": "A"|"B"|"C"|"D"|null, "explanation": string|null}], "answer_key": [{"qno": int, "answer": string}]}
If there are no questions return empty lists."""

GEN_SYS = """You are an expert NEET/JEE paper setter. Using ONLY the concepts present in the given study material, write ORIGINAL multiple-choice questions in the style of the real NEET (single correct, 4 options) / JEE Main exams.
Rules: English only (no Hindi). Exactly four options, exactly ONE correct, plausible distractors. Avoid "all of the above / none of the above". Each question must be self-contained (never refer to "the text", "the passage" or "the figure"). Numerical questions must have consistent units and a correctly computed answer. Explanation: concise (2-4 sentences), English, justifying the correct option.
Difficulty: Easy = direct concept/recall, Medium = application / multi-step, Hard = multi-concept, tricky or calculation heavy.
Return ONLY a JSON object:
{"chapter": string, "questions": [{"subject": string, "difficulty": "Easy"|"Medium"|"Hard", "question": string, "options": {"A": string, "B": string, "C": string, "D": string}, "answer": "A"|"B"|"C"|"D", "explanation": string}]}"""


def allowed_subjects(book) -> list:
    if book["subject"] and book["subject"] != "Mixed":
        return [book["subject"]]
    return SUBJECTS[book["exam"]]


# ----------------------------------------------------------------------------
# QUESTION STORAGE
# ----------------------------------------------------------------------------
def add_question(book, mode, stem, opts, answer, chapter, subject, difficulty=None,
                 qno=None, explanation=None, expl_source=None) -> str:
    """Returns 'ok' | 'dup' | 'bad'."""
    if len(stem) < 10 or any(not opts.get(l) for l in "ABCD"):
        return "bad"
    if NON_EN.search(stem + "".join(opts.values()) + (explanation or "")):
        return "bad"
    if mode == "generated" and answer not in list("ABCD"):
        return "bad"
    dk = dkey_for(stem, opts)
    ex = db_one("SELECT id, answer FROM questions WHERE mode=? AND exam=? AND dkey=?", (mode, book["exam"], dk))
    if ex:
        if ex["answer"] is None and answer:
            db_run("UPDATE questions SET answer=?, explanation=COALESCE(explanation,?), "
                   "expl_source=COALESCE(expl_source,?) WHERE id=?", (answer, explanation, expl_source, ex["id"]))
        return "dup"
    db_run(
        "INSERT INTO questions(book_id,mode,exam,subject,chapter,difficulty,qno,question,opt_a,opt_b,opt_c,opt_d,"
        "answer,explanation,expl_source,dkey,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (book["id"], mode, book["exam"], subject, chapter or "General", difficulty, qno, stem,
         opts["A"], opts["B"], opts["C"], opts["D"], answer, explanation, expl_source, dk, now()))
    return "ok"


def bump_book(bid, dup=0, bad=0):
    if dup or bad:
        db_run("UPDATE books SET dup_count=dup_count+?, rejected_count=rejected_count+? WHERE id=?", (dup, bad, bid))


async def extract_original(book, ch, hint):
    subs = allowed_subjects(book)
    user = (f"Book: {book['title']}\nExam: {book['exam']}\nAllowed subjects: {', '.join(subs)}\n"
            f"Current chapter hint: {hint or 'unknown'}\n\nEXCERPT:\n{ch['text']}")
    data = parse_json(await groq_chat(ORIG_SYS, user, max_tokens=3500, temperature=0))
    chap = clean_chapter(data.get("chapter")) or hint
    src = norm(ch["text"])
    has_cue = bool(ANS_CUE.search(ch["text"]))
    added = dup = bad = 0
    for q in data.get("questions") or []:
        try:
            stem = strip_label(clean(q.get("question")))
            o = q.get("options") or {}
            opts = {l: strip_label(clean(o.get(l))) for l in "ABCD"}
            if not stem or any(not v for v in opts.values()):
                bad += 1
                continue
            if not covered(stem, src, 0.9) or any(not covered(v, src, 0.85) for v in opts.values()):
                bad += 1  # model rewrote something -> reject, originals must stay verbatim
                continue
            ans = str(q.get("answer") or "").strip().upper()[:1]
            ans = ans if ans in list("ABCD") and has_cue else None
            expl = clean(q.get("explanation")) or None
            if expl and (not ans or not covered(expl, src, 0.8)):
                expl = None
            subj = book["subject"] if book["subject"] != "Mixed" else canon_subject(q.get("subject"))
            if subj not in SUBJECTS[book["exam"]]:
                bad += 1
                continue
            qno = q.get("qno")
            qno = int(qno) if str(qno).isdigit() else None
            r = add_question(book, "original", stem, opts, ans, clean_chapter(q.get("chapter")) or chap, subj,
                            qno=qno, explanation=expl, expl_source="book" if expl else None)
            added += r == "ok"
            dup += r == "dup"
            bad += r == "bad"
        except Exception:
            bad += 1
    keys = []
    for k in data.get("answer_key") or []:
        try:
            a = str(k.get("answer", "")).strip().upper()[:1]
            if a in list("ABCD") and str(k.get("qno")).isdigit():
                keys.append((book["id"], int(k["qno"]), a))
        except Exception:
            pass
    if keys:
        db_many("INSERT INTO answer_keys(book_id,qno,answer) VALUES(?,?,?)", keys)
    return chap, added, dup, bad


def shuffle_opts(opts, ans):
    letters = list("ABCD")
    texts = [opts[l] for l in letters]
    correct = opts[ans]
    random.shuffle(texts)
    return dict(zip(letters, texts)), letters[texts.index(correct)]


async def generate_theory(book, ch, hint, n=GEN_PER_CHUNK, diffs=None):
    subs = allowed_subjects(book)
    diffs = diffs or [["Easy", "Medium", "Hard"][i % 3] for i in range(n)]
    user = (f"Book: {book['title']}\nExam: {book['exam']}\nAllowed subjects: {', '.join(subs)}\n"
            f"Current chapter hint: {hint or 'unknown'}\n"
            f"Generate exactly {len(diffs)} questions with these difficulties in order: {', '.join(diffs)}.\n\n"
            f"STUDY MATERIAL:\n{ch['text']}")
    data = parse_json(await groq_chat(GEN_SYS, user, max_tokens=3000, temperature=0.5))
    chap = clean_chapter(data.get("chapter")) or hint
    added = dup = bad = 0
    for q in data.get("questions") or []:
        try:
            stem = clean(q.get("question"))
            o = q.get("options") or {}
            opts = {l: strip_label(clean(o.get(l))) for l in "ABCD"}
            ans = str(q.get("answer") or "").strip().upper()[:1]
            if ans not in list("ABCD") or len({norm(v) for v in opts.values()}) < 4:
                bad += 1
                continue
            opts, ans = shuffle_opts(opts, ans)
            subj = book["subject"] if book["subject"] != "Mixed" else canon_subject(q.get("subject"))
            if subj not in SUBJECTS[book["exam"]]:
                bad += 1
                continue
            diff = str(q.get("difficulty") or "Medium").strip().capitalize()
            diff = diff if diff in ("Easy", "Medium", "Hard") else "Medium"
            expl = clean(q.get("explanation")) or None
            r = add_question(book, "generated", stem, opts, ans, clean_chapter(q.get("chapter")) or chap, subj,
                             difficulty=diff, explanation=expl, expl_source="ai" if expl else None)
            added += r == "ok"
            dup += r == "dup"
            bad += r == "bad"
        except Exception:
            bad += 1
    return chap, added, dup, bad


async def ai_explain(q) -> Optional[str]:
    try:
        user = (f"Question: {q['question']}\nA) {q['opt_a']}\nB) {q['opt_b']}\nC) {q['opt_c']}\nD) {q['opt_d']}\n"
                f"The official correct answer is {q['answer']}. Write a concise explanation (max 80 words, English) "
                f"of why this option is correct. Do not question the official answer.")
        out = await asyncio.wait_for(
            groq_chat("You are an expert NEET/JEE teacher. Reply in plain English text only.", user,
                      json_mode=False, max_tokens=300, temperature=0.2), timeout=25)
        out = clean(out)
        if out and not NON_EN.search(out):
            db_run("UPDATE questions SET explanation=?, expl_source='ai' WHERE id=? AND explanation IS NULL",
                   (out, q["id"]))
            return out
    except Exception as e:
        log.warning("ai_explain failed: %s", e)
    return None


def reconcile_keys(bid):
    """Attach end-of-book answer-key entries to unanswered questions only when the match is unambiguous."""
    rows = db_all("SELECT qno, COUNT(*) c, MIN(answer) a, COUNT(DISTINCT answer) d FROM answer_keys "
                  "WHERE book_id=? GROUP BY qno", (bid,))
    n = 0
    for r in rows:
        if r["c"] != 1:
            continue
        qs = db_all("SELECT id, answer FROM questions WHERE book_id=? AND qno=?", (bid, r["qno"]))
        if len(qs) == 1 and qs[0]["answer"] is None:
            db_run("UPDATE questions SET answer=? WHERE id=?", (r["a"], qs[0]["id"]))
            n += 1
    return n


# ----------------------------------------------------------------------------
# TEXT EXTRACTION (PDF / DOCX / TXT) - streaming, chunked
# ----------------------------------------------------------------------------
def iter_units(path, ext):
    """Yield (unit_no, text, total_units). Streams so huge books don't blow memory."""
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise RuntimeError("PDF is password-protected.")
        total = len(reader.pages)
        for i in range(total):
            try:
                t = reader.pages[i].extract_text() or ""
            except Exception:
                t = ""
            yield i + 1, t, total
    elif ext == "docx":
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        d = docx.Document(path)
        body = list(d.element.body.iterchildren())
        total = max(1, len(body) // 40 + 1)
        for i, child in enumerate(body):
            tag = child.tag.rsplit("}", 1)[-1]
            t = ""
            if tag == "p":
                t = Paragraph(child, d).text
            elif tag == "tbl":
                t = "\n".join(" | ".join(c.text for c in row.cells) for row in Table(child, d).rows)
            yield i // 40 + 1, t, total
    else:  # txt
        size = max(1, os.path.getsize(path))
        read = 0
        buf, unit = [], 1
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                read += len(line.encode("utf-8", "ignore"))
                buf.append(line)
                if len(buf) >= 80:
                    yield unit, "".join(buf), 100
                    buf, unit = [], unit + 1
        if buf:
            yield unit, "".join(buf), 100


def extract_book(book_id, path, ext, progress_cb=None) -> int:
    db_run("DELETE FROM chunks WHERE book_id=?", (book_id,))
    buf, idx, p_from, p_to = "", 0, 1, 1
    rows = []

    def flush_rows():
        nonlocal rows
        if rows:
            db_many("INSERT INTO chunks(book_id,idx,page_from,page_to,text) VALUES(?,?,?,?,?)", rows)
            rows = []

    last_cb = 0
    for unit, text, total in iter_units(path, ext):
        text = text.replace("\x00", "")
        if not buf:
            p_from = unit
        buf += re.sub(r"[ \t]+", " ", text) + "\n"
        p_to = unit
        while len(buf) >= CHUNK_CHARS:
            cut = buf.rfind("\n", CHUNK_CHARS - 600, CHUNK_CHARS)
            cut = CHUNK_CHARS if cut == -1 else cut
            rows.append((book_id, idx, p_from, p_to, buf[:cut]))
            idx += 1
            buf = buf[max(0, cut - OVERLAP):]
            p_from = p_to
        if len(rows) >= 100:
            flush_rows()
        if progress_cb and time.time() - last_cb > 5:
            last_cb = time.time()
            progress_cb(unit, total)
    if len(buf.strip()) > 80 and (idx == 0 or len(buf) > OVERLAP + 60):
        rows.append((book_id, idx, p_from, p_to, buf))
        idx += 1
    flush_rows()
    db_run("UPDATE books SET chunk_count=?, chunks_done=0 WHERE id=?", (idx, book_id))
    return idx


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(blk)
    return h.hexdigest()


# ----------------------------------------------------------------------------
# PROGRESS MESSAGES + BOOK PROCESSING QUEUE
# ----------------------------------------------------------------------------
class Progress:
    def __init__(self, chat, msg_id=None, interval=8):
        self.chat, self.msg_id, self.interval, self.last = chat, msg_id, interval, 0.0

    async def set(self, text, force=False):
        if not self.chat:
            return
        if not force and time.time() - self.last < self.interval:
            return
        self.last = time.time()
        try:
            if self.msg_id:
                await client.edit_message(self.chat, self.msg_id, text, parse_mode="html")
            else:
                m = await client.send_message(self.chat, text, parse_mode="html")
                self.msg_id = m.id
        except Exception:
            pass


JOBS: Optional[asyncio.Queue] = None


async def do_extract(book, prog):
    loop = asyncio.get_running_loop()
    path, ext = book["stored_path"], book["file_name"].rsplit(".", 1)[-1].lower()
    if not path or not os.path.exists(path):
        raise RuntimeError("Stored file missing - please upload the book again.")

    def cb(done, total):
        asyncio.run_coroutine_threadsafe(
            prog.set(f"📖 <b>{esc(book['title'])}</b>\nReading text… {done}/{total}"), loop)

    set_status(book["id"], "extracting")
    n = await asyncio.to_thread(extract_book, book["id"], path, ext, cb)
    if not KEEP_FILES and n > 0:
        with contextlib.suppress(Exception):
            os.remove(path)
            db_run("UPDATE books SET stored_path=NULL WHERE id=?", (book["id"],))
    return n


async def run_chunks(book, prog):
    bid = book["id"]
    ids = [r["id"] for r in db_all("SELECT id FROM chunks WHERE book_id=? AND done=0 ORDER BY idx", (bid,))]
    total = book["chunk_count"] or len(ids)
    hint, failed = None, 0
    for n, cid in enumerate(ids):
        if not get_book(bid):  # deleted meanwhile
            return
        ch = db_one("SELECT * FROM chunks WHERE id=?", (cid,))
        if len(ch["text"].strip()) < 200:
            db_run("UPDATE chunks SET done=1 WHERE id=?", (cid,))
            continue
        try:
            fn = extract_original if book["kind"] == "original" else generate_theory
            chap, added, dup, bad = await fn(book, ch, hint)
            hint = chap or hint
            bump_book(bid, dup, bad)
            db_run("UPDATE chunks SET done=1, chapter=? WHERE id=?", (hint, cid))
        except GroqDailyLimit:
            raise
        except Exception as e:
            failed += 1
            log.warning("chunk %s failed: %s", cid, e)
            db_run("UPDATE chunks SET done=2 WHERE id=?", (cid,))
        db_run("UPDATE books SET chunks_done=chunks_done+1 WHERE id=?", (bid,))
        c = db_one("SELECT COUNT(*) c FROM questions WHERE book_id=?", (bid,))["c"]
        await prog.set(f"⚙️ <b>{esc(book['title'])}</b>\nChunk {n + 1}/{len(ids)} (of {total}) · "
                       f"questions so far: <b>{c}</b>" + (f" · failed chunks: {failed}" if failed else ""))
    return failed


async def process_book(job):
    bid = job["book_id"]
    book = get_book(bid)
    if not book:
        return
    prog = Progress(job.get("chat"), job.get("msg_id"))
    try:
        if book["chunk_count"] == 0:
            await do_extract(book, prog)
            book = get_book(bid)
        if book["chunk_count"] == 0:
            raise RuntimeError("No readable text found. Scanned/image-only PDFs are not supported (no OCR).")
        set_status(bid, "processing")
        failed = await run_chunks(book, prog)
        if not get_book(bid):
            return
        linked = reconcile_keys(bid) if book["kind"] == "original" else 0
        set_status(bid, "ready", f"{failed} chunk(s) failed" if failed else None)
        await prog.set(book_report(bid, linked), force=True)
    except GroqDailyLimit:
        set_status(bid, "paused", "Groq daily limit reached")
        await prog.set(f"⏸ <b>{esc(book['title'])}</b> paused: Groq daily limit reached.\n"
                       f"Progress is saved. Use /library → book → Resume later.", force=True)
    except Exception as e:
        log.exception("process_book failed")
        set_status(bid, "failed", str(e)[:200])
        await prog.set(f"❌ <b>{esc(book['title'])}</b> failed:\n{esc(str(e)[:300])}\nYou can retry from /library.",
                       force=True)


async def gen_more(job):
    """Generate extra questions from random chunks of a theory book."""
    book = get_book(job["book_id"])
    if not book or book["kind"] != "theory":
        return
    prog = Progress(job.get("chat"), job.get("msg_id"))
    target, got, tries = job.get("n", 20), 0, 0
    try:
        while got < target and tries < target * 2:
            tries += 1
            ch = db_one("SELECT * FROM chunks WHERE book_id=? AND length(text)>600 ORDER BY RANDOM() LIMIT 1",
                        (book["id"],))
            if not ch:
                break
            diffs = [random.choice(["Easy", "Medium", "Hard"]) for _ in range(GEN_PER_CHUNK)]
            _, added, dup, bad = await generate_theory(book, ch, ch["chapter"], diffs=diffs)
            bump_book(book["id"], dup, bad)
            got += added
            await prog.set(f"🤖 Generating for <b>{esc(book['title'])}</b>: {got}/{target}")
        await prog.set(f"✅ Generated <b>{got}</b> new questions for <b>{esc(book['title'])}</b>.", force=True)
    except GroqDailyLimit:
        await prog.set("⏸ Groq daily limit reached. Try later.", force=True)
    except Exception as e:
        await prog.set(f"❌ Generation failed: {esc(str(e)[:200])}", force=True)


async def worker():
    while True:
        job = await JOBS.get()
        try:
            if job["type"] == "gen":
                await gen_more(job)
            else:
                await process_book(job)
        except Exception:
            log.exception("worker error")


def book_counts(bid):
    r = db_one("SELECT SUM(answer IS NOT NULL) a, SUM(answer IS NULL) n, COUNT(*) t FROM questions WHERE book_id=?",
               (bid,))
    return int(r["a"] or 0), int(r["n"] or 0), int(r["t"] or 0)


def book_report(bid, linked=0):
    b = get_book(bid)
    a, n, t = book_counts(bid)
    mode = "Original" if b["kind"] == "original" else "AI-generated"
    s = (f"✅ <b>{esc(b['title'])}</b> is ready\n{b['exam']} · {esc(b['subject'])} · {mode}\n"
         f"Questions ready for quizzes: <b>{a}</b>\n")
    if n:
        s += f"Questions without a verified answer (not used in quizzes): {n}\n"
    if linked:
        s += f"Answers linked from answer-key: {linked}\n"
    s += f"Duplicates skipped: {b['dup_count']} · Rejected: {b['rejected_count']}"
    if b["status_msg"]:
        s += f"\n⚠️ {esc(b['status_msg'])}"
    return s


# ----------------------------------------------------------------------------
# QUESTION POOLS
# ----------------------------------------------------------------------------
def pool_where(f):
    w, p = ["q.answer IS NOT NULL"], []
    if f.get("exam"):
        w.append("q.exam=?"); p.append(f["exam"])
    if f.get("subject") and f["subject"] != "all":
        w.append("q.subject=?"); p.append(f["subject"])
    if f.get("mode") in ("original", "generated"):
        w.append("q.mode=?"); p.append(f["mode"])
    if f.get("chapter"):
        w.append("q.chapter=?"); p.append(f["chapter"])
    if f.get("book"):
        w.append("q.book_id=?"); p.append(int(f["book"]))
    if f.get("difficulty") and f["difficulty"] != "Mixed":
        w.append("(q.mode='original' OR q.difficulty=?)"); p.append(f["difficulty"])
    return " AND ".join(w), p


def pool(uid, f, n):
    where, p = pool_where(f)
    return db_all(
        f"SELECT q.* FROM questions q WHERE {where} "
        f"ORDER BY (SELECT COUNT(*) FROM attempts a WHERE a.user_id=? AND a.question_id=q.id), RANDOM() LIMIT ?",
        p + [uid, n])


def pool_count(f):
    where, p = pool_where(f)
    return db_one(f"SELECT COUNT(*) c FROM questions q WHERE {where}", p)["c"]


async def topup(f, need):
    """Generate fresh AI questions on demand from stored theory chunks."""
    if not GROQ_API_KEY:
        return
    sql = ("SELECT c.id FROM chunks c JOIN books b ON b.id=c.book_id WHERE b.kind='theory' "
           "AND b.status IN ('ready','processing') AND b.exam=? AND length(c.text)>600")
    p = [f["exam"]]
    if f.get("subject") and f["subject"] != "all":
        sql += " AND (b.subject=? OR b.subject='Mixed')"; p.append(f["subject"])
    if f.get("chapter"):
        sql += " AND c.chapter=?"; p.append(f["chapter"])
    if f.get("book"):
        sql += " AND b.id=?"; p.append(int(f["book"]))
    sql += " ORDER BY RANDOM() LIMIT 12"
    base = pool_count(f)
    diff = f.get("difficulty")
    for r in db_all(sql, p):
        if pool_count(f) >= base + need:
            break
        ch = db_one("SELECT * FROM chunks WHERE id=?", (r["id"],))
        book = get_book(ch["book_id"])
        d = [diff] * GEN_PER_CHUNK if diff and diff != "Mixed" else None
        try:
            _, _, dup, bad = await generate_theory(book, ch, ch["chapter"], diffs=d)
            bump_book(book["id"], dup, bad)
        except GroqDailyLimit:
            break
        except Exception as e:
            log.warning("topup failed: %s", e)


async def fetch_questions(uid, f, n):
    qs = pool(uid, f, n)
    if len(qs) < n and f.get("mode") == "generated" and f.get("exam"):
        await topup(f, n - len(qs))
        qs = pool(uid, f, n)
    return qs


def mistakes_sql():
    return ("SELECT q.* FROM questions q JOIN attempts a ON a.question_id=q.id "
            "WHERE a.user_id=? AND a.status!='correct' AND q.answer IS NOT NULL "
            "AND a.id=(SELECT MAX(id) FROM attempts WHERE user_id=a.user_id AND question_id=a.question_id) ")


# ----------------------------------------------------------------------------
# QUIZ ENGINE
# ----------------------------------------------------------------------------
@dataclass
class Session:
    sid: str
    user_id: int
    chat_id: int
    kind: str
    title: str
    qs: list
    idx: int = -1
    correct: int = 0
    wrong: int = 0
    skipped: int = 0
    marks: int = 0
    msg_id: Optional[int] = None
    answered: bool = True
    q_start: float = 0.0
    timer: Optional[asyncio.Task] = None
    started: float = field(default_factory=time.time)
    last: float = field(default_factory=time.time)
    by_subject: dict = field(default_factory=dict)


SESS: dict = {}


def render_question(s: Session, q, remaining=None, result=None):
    head = f"<b>Q{s.idx + 1}/{len(s.qs)}</b> · {esc(q['subject'])}"
    if q.get("chapter") and q["chapter"] != "General":
        head += f" · {esc(clip(q['chapter'], 35))}"
    src = "📚 Original" if q["mode"] == "original" else f"🤖 AI · {q.get('difficulty') or 'Mixed'}"
    meta = f"{src}  |  +{MARKS_CORRECT} / {MARKS_WRONG}"
    if result is None and remaining is not None:
        meta += f"  |  ⏱ {remaining}s"
    lines = [head, meta, "", esc(clip(q["question"], 1400)), ""]
    for L in "ABCD":
        lines.append(f"<b>{L})</b> {esc(clip(q['opt_' + L.lower()], 300))}")
    if result:
        lines += ["", result]
    return "\n".join(lines)


def answer_buttons(s: Session):
    return [[Button.inline(L, f"a|{s.sid}|{s.idx}|{L}".encode()) for L in "ABCD"],
            [Button.inline("⏹ Stop quiz", f"x|{s.sid}".encode())]]


async def safe_edit(chat, msg_id, text, buttons=None):
    try:
        await client.edit_message(chat, msg_id, text, buttons=buttons, parse_mode="html")
    except Exception as e:
        if "not modified" not in str(e).lower():
            log.debug("edit failed: %s", e)


async def send_question(s: Session):
    s.idx += 1
    s.answered = False
    s.last = time.time()
    if s.idx >= len(s.qs):
        s.answered = True
        s.idx = len(s.qs) - 1
        await finish(s)
        return
    q = s.qs[s.idx]
    s.q_start = time.time()
    m = await client.send_message(s.chat_id, render_question(s, q, QUESTION_TIME),
                                  buttons=answer_buttons(s), parse_mode="html")
    s.msg_id = m.id
    s.timer = asyncio.create_task(timer_task(s, s.idx))


async def timer_task(s: Session, idx):
    try:
        for wait, left in ((30, 30), (20, 10), (10, 0)):
            await asyncio.sleep(wait)
            if s.idx != idx or s.answered or SESS.get(s.user_id) is not s:
                return
            if left:
                await safe_edit(s.chat_id, s.msg_id, render_question(s, s.qs[idx], left), answer_buttons(s))
        await resolve(s, None, "timeout")
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("timer error")


async def resolve(s: Session, letter, status):
    if s.answered:
        return
    s.answered = True
    s.last = time.time()
    if s.timer and s.timer is not asyncio.current_task():
        s.timer.cancel()
    q = s.qs[s.idx]
    taken = time.time() - s.q_start
    bs = s.by_subject.setdefault(q["subject"], [0, 0, 0, 0])
    if status == "timeout":
        st, marks, s.skipped = "timeout", 0, s.skipped + 1
        bs[2] += 1
        head = "⌛ <b>Time up!</b>  0 marks"
    elif letter == q["answer"]:
        st, marks, s.correct = "correct", MARKS_CORRECT, s.correct + 1
        bs[0] += 1
        head = f"✅ <b>Correct!</b>  +{MARKS_CORRECT}"
    else:
        st, marks, s.wrong = "wrong", MARKS_WRONG, s.wrong + 1
        bs[1] += 1
        head = f"❌ <b>Wrong</b>  {MARKS_WRONG}"
    bs[3] += marks
    s.marks += marks
    db_run("INSERT INTO attempts(user_id,question_id,exam,subject,chosen,status,marks,taken,kind,ts) "
           "VALUES(?,?,?,?,?,?,?,?,?,?)",
           (s.user_id, q["id"], q["exam"], q["subject"], letter, st, marks, round(taken, 1), s.kind, now()))
    ca = q["answer"]
    res = head + f"\nCorrect answer: <b>{ca}) {esc(clip(q['opt_' + ca.lower()], 300))}</b>"
    if letter and letter != ca:
        res += f"\nYour answer: {letter}"
    last = s.idx + 1 >= len(s.qs)
    nav = [[Button.inline("🏁 Finish" if last else "Next ▶️", f"n|{s.sid}".encode()),
            Button.inline("⏹ Stop", f"x|{s.sid}".encode())]]
    expl = q.get("explanation")
    label = "💡 <b>Explanation</b>" if q.get("expl_source") != "ai" else "💡 <b>Explanation</b> (AI)"
    if expl:
        await safe_edit(s.chat_id, s.msg_id, render_question(s, q, result=res + f"\n\n{label}\n" + esc(clip(expl, 1200))), nav)
        return
    await safe_edit(s.chat_id, s.msg_id, render_question(s, q, result=res + "\n\n💡 <i>Loading explanation…</i>"), nav)
    expl = await ai_explain(q)
    q["explanation"], q["expl_source"] = expl, "ai"
    tail = f"\n\n💡 <b>Explanation</b> (AI)\n{esc(clip(expl, 1200))}" if expl else "\n\n💡 Explanation not available."
    await safe_edit(s.chat_id, s.msg_id, render_question(s, q, result=res + tail), nav)


async def finish(s: Session, auto=False):
    if SESS.get(s.user_id) is s:
        del SESS[s.user_id]
    if s.timer and s.timer is not asyncio.current_task():
        s.timer.cancel()
    if s.idx >= 0 and s.msg_id and not s.answered:
        await safe_edit(s.chat_id, s.msg_id, render_question(s, s.qs[s.idx], result="⏹ <i>Stopped</i>"), None)
    attempted = s.correct + s.wrong + s.skipped
    maxm = len(s.qs) * MARKS_CORRECT
    db_run("INSERT INTO sessions_log(user_id,kind,title,total,correct,wrong,skipped,marks,max_marks,started_at,finished_at)"
           " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
           (s.user_id, s.kind, s.title, len(s.qs), s.correct, s.wrong, s.skipped, s.marks, maxm,
            int(s.started), now()))
    mins = int((time.time() - s.started) // 60)
    t = [f"🏁 <b>{esc(s.title)} — Result</b>" + (" (auto-ended: inactive)" if auto else ""), "",
         f"Score: <b>{s.marks}</b> / {maxm}",
         f"✅ Correct: {s.correct}   ❌ Wrong: {s.wrong}   ⌛ Unattempted: {len(s.qs) - s.correct - s.wrong}",
         f"🎯 Accuracy: <b>{pct(s.correct, attempted)}</b> (of {attempted} questions seen)",
         f"⏱ Time: {mins} min"]
    if len(s.by_subject) > 1:
        t.append("\n<b>Subject-wise</b>")
        for sub, (c, w, k, m) in s.by_subject.items():
            t.append(f"• {esc(sub)}: {m} marks  (✅{c} ❌{w} ⌛{k})")
    btn = [[Button.inline("🔁 Revise mistakes", b"p|revision"), Button.inline("🏆 Leaderboard", b"lb|all|all")],
           [Button.inline("🎯 New quiz", b"p|makequiz")]]
    await client.send_message(s.chat_id, "\n".join(t), buttons=btn, parse_mode="html")


async def launch(chat_id, uid, kind, title, qs):
    old = SESS.pop(uid, None)
    if old and old.timer:
        old.timer.cancel()
    s = Session(sid=uuid.uuid4().hex[:8], user_id=uid, chat_id=chat_id, kind=kind, title=title, qs=qs)
    SESS[uid] = s
    await client.send_message(
        chat_id,
        f"📝 <b>{esc(title)}</b>\n{len(qs)} questions · ⏱ {QUESTION_TIME}s each · "
        f"+{MARKS_CORRECT} correct / {MARKS_WRONG} wrong\nGood luck! 🍀", parse_mode="html")
    await send_question(s)


async def janitor():
    while True:
        await asyncio.sleep(300)
        for s in list(SESS.values()):
            if time.time() - s.last > 45 * 60:
                with contextlib.suppress(Exception):
                    await finish(s, auto=True)


# ----------------------------------------------------------------------------
# QUIZ SETUP WIZARD
# ----------------------------------------------------------------------------
WIZ: dict = {}
MODE_LABEL = {"original": "Original", "generated": "AI Generated", "any": "Original + AI"}


def wb(text, field_, val):
    return Button.inline(text, f"w|{field_}|{val}".encode())


def next_field(w):
    if w["kind"] == "mock":
        steps = ["exam", "mode", "size"]
    else:
        steps = ["mode", "exam", "subject", "scope"]
        if w.get("scope") == "chapter":
            steps.append("chapter")
        if w.get("scope") == "book":
            steps.append("book")
        if w.get("mode") in ("generated", "any"):
            steps.append("difficulty")
        steps.append("count")
    for s_ in steps:
        if s_ not in w:
            return s_
    return None


def wiz_summary(w):
    bits = []
    if w.get("mode"):
        bits.append(MODE_LABEL[w["mode"]])
    if w.get("exam"):
        bits.append(w["exam"])
    if w.get("subject"):
        bits.append("All subjects" if w["subject"] == "all" else w["subject"])
    if w.get("chapter"):
        bits.append(clip(w["chapter"], 30))
    if w.get("book"):
        bits.append(f"Book #{w['book']}")
    if w.get("difficulty"):
        bits.append(w["difficulty"])
    return " · ".join(bits)


def build_step(w):
    f = next_field(w)
    head = "🎯 <b>Mock test setup</b>" if w["kind"] == "mock" else "🎯 <b>Quiz setup</b>"
    if wiz_summary(w):
        head += f"\n<i>{esc(wiz_summary(w))}</i>"
    rows, prompt = [], ""
    if f == "mode":
        prompt = "Choose question source:"
        rows = [[wb("📚 Original (books / PYQ)", "mode", "original")], [wb("🤖 AI Generated", "mode", "generated")],
                [wb("🔀 Both", "mode", "any")]]
    elif f == "exam":
        prompt = "Choose exam:"
        rows = [[wb("🧬 NEET", "exam", "NEET"), wb("⚙️ JEE", "exam", "JEE")]]
    elif f == "subject":
        prompt = "Choose subject:"
        rows = [[wb(s_, "subject", s_)] for s_ in SUBJECTS[w["exam"]]] + [[wb("📚 All subjects", "subject", "all")]]
    elif f == "scope":
        prompt = "Choose scope:"
        rows = [[wb("🎲 Random practice", "scope", "random")], [wb("📖 Chapter-wise", "scope", "chapter")],
                [wb("📕 Book-wise", "scope", "book")]]
    elif f == "chapter":
        flt = {"exam": w["exam"], "subject": w["subject"], "mode": w["mode"]}
        where, p = pool_where(flt)
        chs = db_all(f"SELECT q.chapter ch, COUNT(*) n FROM questions q WHERE {where} GROUP BY q.chapter "
                     f"ORDER BY q.chapter LIMIT 60", p)
        w["opts"]["chapter"] = [c["ch"] for c in chs]
        if not chs:
            prompt = "No chapters available yet for this selection."
            rows = [[wb("🎲 Random practice instead", "scope", "random")]]
        else:
            prompt = "Choose chapter:"
            rows = [[wb(f"{clip(c['ch'], 38)} ({c['n']})", "chapter", i)] for i, c in enumerate(chs)]
    elif f == "book":
        flt = {"exam": w["exam"], "subject": w["subject"], "mode": w["mode"]}
        where, p = pool_where(flt)
        bks = db_all(f"SELECT b.id, b.title, COUNT(*) n FROM books b JOIN questions q ON q.book_id=b.id "
                     f"WHERE {where} GROUP BY b.id ORDER BY b.id DESC LIMIT 40", p)
        w["opts"]["book"] = [b["id"] for b in bks]
        if not bks:
            prompt = "No books available yet for this selection."
            rows = [[wb("🎲 Random practice instead", "scope", "random")]]
        else:
            prompt = "Choose book:"
            rows = [[wb(f"{clip(b['title'], 34)} ({b['n']})", "book", i)] for i, b in enumerate(bks)]
    elif f == "difficulty":
        prompt = "Choose difficulty (applies to AI questions):"
        rows = [[wb("🟢 Easy", "difficulty", "Easy"), wb("🟡 Medium", "difficulty", "Medium"),
                 wb("🔴 Hard", "difficulty", "Hard")], [wb("🔀 Mixed", "difficulty", "Mixed")]]
    elif f == "count":
        prompt = "How many questions?"
        rows = [[wb(str(n), "count", n) for n in (10, 20, 30, 50)]]
    elif f == "size":
        prompt = "Choose mock test size:"
        full = MOCK_PLAN[w["exam"]]["full"]
        tot = sum(full.values())
        rows = [[wb(f"📄 Full mock ({tot} Qs)", "size", "full")], [wb("⚡ Mini mock (30 Qs)", "size", "mini")]]
    rows.append([Button.inline("❌ Cancel", b"w|cancel|x")])
    return f"{head}\n\n{prompt}", rows


async def show_step(chat_id, w, edit_ev=None):
    text, rows = build_step(w)
    if edit_ev:
        try:
            await edit_ev.edit(text, buttons=rows, parse_mode="html")
            return
        except Exception as e:
            if "not modified" in str(e).lower():
                return
    await client.send_message(chat_id, text, buttons=rows, parse_mode="html")


async def open_wizard(ev, **preset):
    await touch(ev)
    w = {"kind": "practice", "opts": {}}
    w.update(preset)
    WIZ[ev.sender_id] = w
    await show_step(ev.chat_id, w)


async def on_wizard(ev, parts):
    uid = ev.sender_id
    w = WIZ.get(uid)
    if not w:
        await ev.answer("Setup expired. Send /makequiz again.", alert=True)
        return
    _, fld, val = parts
    if fld == "cancel":
        WIZ.pop(uid, None)
        await ev.answer()
        await ev.edit("Cancelled.")
        return
    try:
        if fld in ("chapter", "book"):
            val = w["opts"][fld][int(val)]
        elif fld == "count":
            val = int(val)
    except Exception:
        await ev.answer("Option expired, please restart.", alert=True)
        return
    w[fld] = val
    if fld == "scope":
        w.pop("chapter", None)
        w.pop("book", None)
    await ev.answer()
    if next_field(w) is None:
        WIZ.pop(uid, None)
        await ev.edit("⏳ Preparing your quiz…")
        await start_from_wizard(ev, w)
    else:
        await show_step(ev.chat_id, w, edit_ev=ev)


async def start_from_wizard(ev, w):
    uid, chat = ev.sender_id, ev.chat_id
    if w["kind"] == "mock":
        plan = MOCK_PLAN[w["exam"]][w["size"]]
        qs, short = [], []
        for subj, n in plan.items():
            f = {"exam": w["exam"], "subject": subj, "mode": w["mode"]}
            got = await fetch_questions(uid, f, n)
            qs += got
            if len(got) < n:
                short.append(f"{subj}: {len(got)}/{n}")
        if not qs:
            await client.send_message(chat, "😕 No questions available for this mock yet. Ask the admin to upload books.")
            return
        if short:
            await client.send_message(chat, "ℹ️ Not enough questions in the library for a full paper — "
                                            "using what's available:\n" + "\n".join(short))
        title = f"{w['exam']} {'Full' if w['size'] == 'full' else 'Mini'} Mock Test ({MODE_LABEL[w['mode']]})"
        await launch(chat, uid, "mock", title, qs)
        return
    f = {k: w.get(k) for k in ("exam", "subject", "mode", "chapter", "book", "difficulty")}
    qs = await fetch_questions(uid, f, w["count"])
    if not qs:
        await client.send_message(chat, "😕 No questions available for this selection yet. "
                                        "Try another option or ask the admin to upload books.")
        return
    if len(qs) < w["count"]:
        await client.send_message(chat, f"ℹ️ Only {len(qs)} questions available for this selection.")
    subj = "All subjects" if w["subject"] == "all" else w["subject"]
    await launch(chat, uid, "practice", f"{w['exam']} {subj} · {MODE_LABEL[w['mode']]}", qs)


# ----------------------------------------------------------------------------
# USERS / STATS / LEADERBOARD
# ----------------------------------------------------------------------------
async def touch(ev):
    try:
        s = await ev.get_sender()
        name = " ".join(x for x in (getattr(s, "first_name", None), getattr(s, "last_name", None)) if x) \
            or getattr(s, "title", None) or str(ev.sender_id)
        db_run("INSERT INTO users(id,name,username,joined_at) VALUES(?,?,?,?) "
               "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
               (ev.sender_id, name, getattr(s, "username", None), now()))
    except Exception:
        pass


def uname(r):
    return esc(r.get("name") or (("@" + r["username"]) if r.get("username") else str(r["user_id"])))


def leaderboard_text(exam, period, me):
    w, p = [], []
    if exam != "all":
        w.append("a.exam=?"); p.append(exam)
    if period == "week":
        w.append("a.ts>=?"); p.append(now() - 7 * 86400)
    where = ("WHERE " + " AND ".join(w)) if w else ""
    rows = db_all(
        f"SELECT a.user_id, SUM(a.marks) m, SUM(a.status='correct') c, COUNT(*) n, u.name, u.username "
        f"FROM attempts a LEFT JOIN users u ON u.id=a.user_id {where} GROUP BY a.user_id "
        f"ORDER BY m DESC, (SUM(a.status='correct')*1.0/COUNT(*)) DESC", p)
    title = f"🏆 <b>Leaderboard</b> — {'All exams' if exam == 'all' else exam} · {'Last 7 days' if period == 'week' else 'All-time'}"
    if not rows:
        return title + "\n\nNo attempts yet."
    medals = ["🥇", "🥈", "🥉"]
    lines = [title, ""]
    for i, r in enumerate(rows[:10]):
        mk = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mk} {uname(r)} — <b>{r['m']}</b> marks · {pct(r['c'], r['n'])} ({r['n']} Qs)")
    for i, r in enumerate(rows):
        if r["user_id"] == me and i >= 10:
            lines.append(f"\nYour rank: #{i + 1} — {r['m']} marks · {pct(r['c'], r['n'])}")
    return "\n".join(lines)


def leaderboard_buttons(exam, period):
    def b(label, e, pr):
        mark = "• " if (e, pr) == (exam, period) else ""
        return Button.inline(mark + label, f"lb|{e}|{pr}".encode())
    return [[b("All", "all", period), b("NEET", "NEET", period), b("JEE", "JEE", period)],
            [b("All-time", exam, "all"), b("This week", exam, "week")]]


def score_text(uid):
    t = db_one("SELECT COUNT(*) n, SUM(status='correct') c, SUM(status='wrong') w, SUM(status='timeout') k, "
               "SUM(marks) m FROM attempts WHERE user_id=?", (uid,))
    if not t["n"]:
        return "You haven't attempted any questions yet. Try /makequiz 🎯"
    lines = ["📊 <b>Your score</b>", "",
             f"Total marks: <b>{t['m']}</b>",
             f"Questions: {t['n']}  (✅{t['c']}  ❌{t['w']}  ⌛{t['k']})",
             f"Accuracy: <b>{pct(t['c'], t['n'])}</b>", ""]
    for r in db_all("SELECT exam, subject, COUNT(*) n, SUM(status='correct') c, SUM(marks) m FROM attempts "
                    "WHERE user_id=? GROUP BY exam, subject ORDER BY exam, subject", (uid,)):
        lines.append(f"• {r['exam']} {r['subject']}: {r['m']} marks · {pct(r['c'], r['n'])} ({r['n']} Qs)")
    sl = db_all("SELECT title, marks, max_marks, correct, total FROM sessions_log WHERE user_id=? "
                "ORDER BY id DESC LIMIT 5", (uid,))
    if sl:
        lines.append("\n<b>Recent tests</b>")
        for r in sl:
            lines.append(f"• {esc(clip(r['title'], 40))}: {r['marks']}/{r['max_marks']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# LIBRARY (student summary + owner management)
# ----------------------------------------------------------------------------
STATUS_EMOJI = {"ready": "✅", "processing": "⚙️", "queued": "⏳", "extracting": "📖", "failed": "❌",
                "paused": "⏸", "downloading": "⬇️"}


def library_view(uid):
    if not is_owner(uid):
        rows = db_all("SELECT exam, subject, mode, COUNT(*) n FROM questions WHERE answer IS NOT NULL "
                      "GROUP BY exam, subject, mode ORDER BY exam, subject, mode")
        if not rows:
            return "📚 The library is empty. Please wait for the admin to upload books.", None
        lines = ["📚 <b>Question Library</b>"]
        cur = None
        for r in rows:
            if r["exam"] != cur:
                cur = r["exam"]
                lines.append(f"\n<b>{cur}</b>")
            lines.append(f"• {r['subject']} — {MODE_LABEL[r['mode']]}: {r['n']}")
        return "\n".join(lines), [[Button.inline("🎯 Start a quiz", b"p|makequiz")]]
    bks = db_all("SELECT b.*, (SELECT COUNT(*) FROM questions q WHERE q.book_id=b.id AND q.answer IS NOT NULL) qa, "
                 "(SELECT COUNT(*) FROM questions q WHERE q.book_id=b.id AND q.answer IS NULL) qn "
                 "FROM books b ORDER BY b.id DESC LIMIT 40")
    tot = db_one("SELECT COUNT(*) c FROM books")["c"]
    tq = db_all("SELECT exam, mode, COUNT(*) n FROM questions WHERE answer IS NOT NULL GROUP BY exam, mode")
    lines = [f"📚 <b>Library</b> — {tot} book(s)"]
    for r in tq:
        lines.append(f"{r['exam']} {MODE_LABEL[r['mode']]}: {r['n']} questions")
    lines.append("")
    rows = []
    for b in bks:
        e = STATUS_EMOJI.get(b["status"], "•")
        kind = "Orig" if b["kind"] == "original" else "AI"
        lines.append(f"{e} <b>#{b['id']}</b> {esc(clip(b['title'], 40))} — {b['exam']} {esc(b['subject'])} · {kind} · "
                     f"<b>{b['qa']}</b> Qs" + (f" (+{b['qn']} no key)" if b["qn"] else ""))
        rows.append([Button.inline(f"{e} #{b['id']} {clip(b['title'], 30)}", f"bk|view|{b['id']}".encode())])
    if not bks:
        lines.append("No books yet. Use /uploadbook")
    rows.append([Button.inline("⬆️ Upload book", b"p|uploadbook")])
    return "\n".join(lines), rows


def book_detail(bid):
    b = get_book(bid)
    if not b:
        return "Book not found.", [[Button.inline("⬅️ Library", b"bk|list|0")]]
    a, n, t = book_counts(bid)
    per = db_all("SELECT subject, COUNT(*) n FROM questions WHERE book_id=? AND answer IS NOT NULL GROUP BY subject", (bid,))
    chs = db_one("SELECT COUNT(DISTINCT chapter) c FROM questions WHERE book_id=?", (bid,))["c"]
    lines = [f"📖 <b>#{b['id']} {esc(b['title'])}</b>",
             f"{b['exam']} · {esc(b['subject'])} · {'Original questions' if b['kind'] == 'original' else 'Theory (AI MCQs)'}",
             f"Status: {STATUS_EMOJI.get(b['status'], '')} {b['status']}" + (f" — {esc(b['status_msg'])}" if b["status_msg"] else ""),
             f"File: {esc(b['file_name'])} ({human_size(b['file_size'])})",
             f"Chunks: {b['chunks_done']}/{b['chunk_count']}",
             f"Questions usable: <b>{a}</b> · without verified answer: {n} · chapters: {chs}",
             f"Duplicates skipped: {b['dup_count']} · rejected: {b['rejected_count']}"]
    if per:
        lines.append("By subject: " + ", ".join(f"{p['subject']} {p['n']}" for p in per))
    rows = []
    if b["kind"] == "theory" and b["status"] in ("ready", "processing"):
        rows.append([Button.inline("🤖 Generate +20 questions", f"bk|gen|{bid}".encode())])
    if b["status"] in ("failed", "paused", "ready"):
        label = "▶️ Resume / retry" if b["status"] != "ready" else "🔁 Re-process failed chunks"
        rows.append([Button.inline(label, f"bk|retry|{bid}".encode())])
    rows.append([Button.inline("🗑 Delete book", f"bk|del|{bid}".encode()), Button.inline("⬅️ Library", b"bk|list|0")])
    return "\n".join(lines), rows


async def on_book_cb(ev, parts):
    if not is_owner(ev.sender_id):
        await ev.answer("Owner only.", alert=True)
        return
    act, bid = parts[1], int(parts[2] or 0)
    await ev.answer()
    if act == "list":
        t, r = library_view(ev.sender_id)
        await ev.edit(t, buttons=r, parse_mode="html")
    elif act == "view":
        t, r = book_detail(bid)
        await ev.edit(t, buttons=r, parse_mode="html")
    elif act == "del":
        b = get_book(bid)
        if not b:
            return
        await ev.edit(f"⚠️ Delete <b>{esc(b['title'])}</b> and all its questions permanently?",
                      buttons=[[Button.inline("Yes, delete", f"bk|delok|{bid}".encode()),
                                Button.inline("Cancel", f"bk|view|{bid}".encode())]], parse_mode="html")
    elif act == "delok":
        b = get_book(bid)
        if b:
            if b["stored_path"]:
                with contextlib.suppress(Exception):
                    os.remove(b["stored_path"])
            db_run("DELETE FROM books WHERE id=?", (bid,))
        t, r = library_view(ev.sender_id)
        await ev.edit("🗑 Deleted.\n\n" + t, buttons=r, parse_mode="html")
    elif act == "gen":
        await JOBS.put({"type": "gen", "book_id": bid, "n": 20, "chat": ev.chat_id})
        await client.send_message(ev.chat_id, "🤖 Generation queued.")
    elif act == "retry":
        db_run("UPDATE chunks SET done=0 WHERE book_id=? AND done=2", (bid,))
        set_status(bid, "queued")
        await JOBS.put({"type": "process", "book_id": bid, "chat": ev.chat_id})
        await client.send_message(ev.chat_id, "⏳ Queued again.")


# ----------------------------------------------------------------------------
# BOOK UPLOAD (owner only)
# ----------------------------------------------------------------------------
UP: dict = {}


def file_ext(msg):
    name = (msg.file.name or "").lower() if msg.file else ""
    mime = (msg.file.mime_type or "").lower() if msg.file else ""
    for e in ("pdf", "docx", "txt"):
        if name.endswith("." + e):
            return e
    if mime == "application/pdf":
        return "pdf"
    if "wordprocessingml" in mime:
        return "docx"
    if mime == "text/plain":
        return "txt"
    return None


async def on_upload_cb(ev, parts):
    uid = ev.sender_id
    if not is_owner(uid):
        await ev.answer("Owner only.", alert=True)
        return
    st = UP.get(uid)
    if not st:
        await ev.answer("Upload session expired. Send the file again.", alert=True)
        return
    _, fld, val = parts
    await ev.answer()
    if fld == "cancel":
        UP.pop(uid, None)
        await ev.edit("Cancelled.")
        return
    st[fld] = val
    if fld == "kind":
        await ev.edit("Which exam is this book for?",
                      buttons=[[Button.inline("🧬 NEET", b"up|exam|NEET"), Button.inline("⚙️ JEE", b"up|exam|JEE")],
                               [Button.inline("❌ Cancel", b"up|cancel|x")]])
    elif fld == "exam":
        rows = [[Button.inline(s_, f"up|subj|{s_}".encode())] for s_ in SUBJECTS[val]]
        rows.append([Button.inline("📚 Mixed (all subjects)", b"up|subj|Mixed")])
        await ev.edit("Which subject?", buttons=rows)
    elif fld == "subj":
        UP.pop(uid, None)
        await ev.edit("⬇️ Starting download…")
        await begin_book(ev.chat_id, uid, st)


async def begin_book(chat, uid, st):
    msg = st["msg"]
    size = msg.file.size or 0
    free = shutil.disk_usage(DATA_DIR).free
    if size and free < size * 1.3 + 50 * 1024 * 1024:
        await client.send_message(
            chat, f"❌ Not enough free disk space.\nFile: {human_size(size)} · Free: {human_size(free)}.\n"
                  f"Increase the Persistent Disk size on Render (or free some space) and upload again.")
        return
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", st["file_name"])[:80]
    bid = db_run("INSERT INTO books(title,kind,exam,subject,file_name,file_size,status,uploaded_by,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?)",
                 (st["title"], st["kind"], st["exam"], st["subj"], st["file_name"], size, "downloading", uid, now()))
    path = os.path.join(BOOKS_DIR, f"{bid}_{safe}")
    prog = Progress(chat, None, interval=4)
    await prog.set(f"⬇️ Downloading <b>{esc(st['title'])}</b> ({human_size(size)})…", force=True)

    def cb(cur, total):
        asyncio.ensure_future(prog.set(f"⬇️ Downloading <b>{esc(st['title'])}</b>\n{human_size(cur)} / {human_size(total)} "
                                       f"({pct(cur, total)})"))

    try:
        await client.download_media(msg, file=path, progress_callback=cb)
        h = await asyncio.to_thread(sha256_file, path)
        dup = db_one("SELECT id, title FROM books WHERE file_hash=?", (h,))
        if dup:
            with contextlib.suppress(Exception):
                os.remove(path)
            db_run("DELETE FROM books WHERE id=?", (bid,))
            await prog.set(f"⚠️ This exact file is already in the library as #{dup['id']} "
                           f"({esc(dup['title'])}). Skipped.", force=True)
            return
        db_run("UPDATE books SET file_hash=?, stored_path=?, status='queued' WHERE id=?", (h, path, bid))
    except Exception as e:
        log.exception("download failed")
        with contextlib.suppress(Exception):
            os.remove(path)
        db_run("DELETE FROM books WHERE id=?", (bid,))
        await prog.set(f"❌ Download failed: {esc(str(e)[:200])}\nPlease upload again.", force=True)
        return
    await prog.set(f"✅ Downloaded. Queued for processing (book #{bid}). I'll keep you posted.", force=True)
    await JOBS.put({"type": "process", "book_id": bid, "chat": chat, "msg_id": None})


# ----------------------------------------------------------------------------
# ACTIONS (shared by commands + panel buttons)
# ----------------------------------------------------------------------------
def panel_buttons(uid):
    rows = [[Button.inline("🧬 NEET", b"p|neet"), Button.inline("⚙️ JEE", b"p|jee")],
            [Button.inline("📚 Original Qs", b"p|original"), Button.inline("🤖 AI Qs", b"p|generated")],
            [Button.inline("📖 Chapter-wise", b"p|chapter"), Button.inline("📝 Mock test", b"p|mocktest")],
            [Button.inline("❗ Mistakes", b"p|mistakes"), Button.inline("🔁 Revision", b"p|revision")],
            [Button.inline("🏆 Leaderboard", b"p|leaderboard"), Button.inline("📊 My score", b"p|score")],
            [Button.inline("🎯 Custom quiz", b"p|makequiz"), Button.inline("📚 Library", b"p|library")]]
    if is_owner(uid):
        rows.append([Button.inline("⬆️ Upload book", b"p|uploadbook")])
    return rows


async def act_start(ev):
    await touch(ev)
    await client.send_message(
        ev.chat_id,
        "👋 <b>Welcome to the NEET &amp; JEE Quiz Bot!</b>\n\n"
        "• MCQs from real question banks (Original) and AI-generated from theory books\n"
        f"• ⏱ {QUESTION_TIME}s per question · +{MARKS_CORRECT} correct · {MARKS_WRONG} wrong\n"
        "• Explanations, leaderboard, mistake revision &amp; full mock tests\n\nTap below to begin 👇",
        buttons=panel_buttons(ev.sender_id), parse_mode="html")


async def act_panel(ev):
    await client.send_message(ev.chat_id, "🎛 <b>Panel</b>", buttons=panel_buttons(ev.sender_id), parse_mode="html")


async def act_uploadbook(ev):
    if not is_owner(ev.sender_id):
        await client.send_message(ev.chat_id, "🔒 Only the owner can upload or manage books.")
        return
    if not ev.is_private:
        await client.send_message(ev.chat_id, "Please use /uploadbook in a private chat with me.")
        return
    await client.send_message(
        ev.chat_id, "📤 Send me the book now as a <b>PDF / DOCX / TXT</b> file (any size, up to Telegram's own limit).\n"
                    "Tip: the file caption becomes the book title.", parse_mode="html")


async def act_library(ev):
    t, r = library_view(ev.sender_id)
    await client.send_message(ev.chat_id, t, buttons=r, parse_mode="html")


async def act_leaderboard(ev):
    await client.send_message(ev.chat_id, leaderboard_text("all", "all", ev.sender_id),
                              buttons=leaderboard_buttons("all", "all"), parse_mode="html")


async def act_score(ev):
    await touch(ev)
    await client.send_message(ev.chat_id, score_text(ev.sender_id), parse_mode="html")


async def act_mistakes(ev):
    uid = ev.sender_id
    rows = db_all(mistakes_sql() + "ORDER BY a.id DESC", (uid,))
    if not rows:
        await client.send_message(ev.chat_id, "🎉 No pending mistakes! Attempt some quizzes first.")
        return
    by = {}
    for r in rows:
        by[r["subject"]] = by.get(r["subject"], 0) + 1
    lines = [f"❗ <b>Your mistakes</b> — {len(rows)} question(s) to revise",
             ", ".join(f"{k}: {v}" for k, v in by.items()), "", "<b>Latest:</b>"]
    for r in rows[:8]:
        lines.append(f"• [{esc(r['subject'])}] {esc(clip(r['question'], 90))} → <b>{r['answer']}</b>")
    await client.send_message(ev.chat_id, "\n".join(lines),
                              buttons=[[Button.inline("🔁 Start revision", b"p|revision")]], parse_mode="html")


async def act_revision(ev, n=30):
    uid = ev.sender_id
    await touch(ev)
    rows = db_all(mistakes_sql() + "ORDER BY RANDOM() LIMIT ?", (uid, n))
    if not rows:
        await client.send_message(ev.chat_id, "🎉 No mistakes to revise right now!")
        return
    await launch(ev.chat_id, uid, "revision", "Mistakes Revision", rows)


async def act_stop(ev):
    s = SESS.get(ev.sender_id)
    if not s:
        await client.send_message(ev.chat_id, "You have no running quiz.")
        return
    await finish(s)


ACTIONS = {
    "start": act_start, "panel": act_panel, "uploadbook": act_uploadbook, "library": act_library,
    "leaderboard": act_leaderboard, "score": act_score, "mistakes": act_mistakes, "revision": act_revision,
    "stop": act_stop,
    "makequiz": lambda ev: open_wizard(ev),
    "neet": lambda ev: open_wizard(ev, exam="NEET"),
    "jee": lambda ev: open_wizard(ev, exam="JEE"),
    "original": lambda ev: open_wizard(ev, mode="original"),
    "generated": lambda ev: open_wizard(ev, mode="generated"),
    "chapter": lambda ev: open_wizard(ev, scope="chapter"),
    "mocktest": lambda ev: open_wizard(ev, kind="mock"),
}


# ----------------------------------------------------------------------------
# TELEGRAM HANDLERS
# ----------------------------------------------------------------------------
def cmd(name):
    return events.NewMessage(incoming=True, pattern=re.compile(rf"(?is)^/{name}(?:@\w+)?(?:\s+(.*))?$"))


def guarded(fn):
    async def wrapper(ev):
        try:
            await fn(ev)
        except events.StopPropagation:
            raise
        except Exception:
            log.exception("handler error")
            with contextlib.suppress(Exception):
                await client.send_message(ev.chat_id, "⚠️ Something went wrong. Please try again.")
    return wrapper


for _name, _fn in ACTIONS.items():
    if _name == "revision":
        async def _rev(ev):
            arg = (ev.pattern_match.group(1) or "").strip()
            await act_revision(ev, int(arg) if arg.isdigit() and 1 <= int(arg) <= 100 else 30)
        client.add_event_handler(guarded(_rev), cmd("revision"))
    else:
        client.add_event_handler(guarded(_fn if asyncio.iscoroutinefunction(_fn) else _fn), cmd(_name))


@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private and e.document is not None))
@guarded
async def on_document(ev):
    uid = ev.sender_id
    if not is_owner(uid):
        await ev.respond("🔒 Only the owner can upload books.")
        return
    ext = file_ext(ev.message)
    if not ext:
        await ev.respond("❌ Unsupported file. Please send PDF, DOCX or TXT (convert .doc to .docx first).")
        return
    name = ev.message.file.name or f"book.{ext}"
    title = clean(ev.raw_text) or re.sub(r"\.[A-Za-z0-9]+$", "", name).replace("_", " ")
    UP[uid] = {"msg": ev.message, "file_name": name, "title": clip(title, 80)}
    await ev.respond(
        f"📘 <b>{esc(clip(title, 80))}</b> ({human_size(ev.message.file.size)})\n\nWhat kind of book is this?",
        buttons=[[Button.inline("📚 Question bank / PYQ (Original)", b"up|kind|original")],
                 [Button.inline("📖 Theory / NCERT / notes (AI MCQs)", b"up|kind|theory")],
                 [Button.inline("❌ Cancel", b"up|cancel|x")]], parse_mode="html")


@client.on(events.CallbackQuery())
async def on_callback(ev):
    try:
        parts = ev.data.decode().split("|")
        key = parts[0]
        if key == "a":
            _, sid, idx, letter = parts
            s = SESS.get(ev.sender_id)
            if not s or s.sid != sid:
                other = any(x.sid == sid for x in SESS.values())
                await ev.answer("This quiz belongs to another student." if other else "This quiz has ended.", alert=True)
                return
            if int(idx) != s.idx or s.answered:
                await ev.answer("Already answered.")
                return
            await ev.answer()
            await resolve(s, letter, "answered")
        elif key == "n":
            s = SESS.get(ev.sender_id)
            if not s or s.sid != parts[1]:
                await ev.answer("This quiz has ended.", alert=True)
                return
            if not s.answered:
                await ev.answer()
                return
            await ev.answer()
            await send_question(s)
        elif key == "x":
            s = SESS.get(ev.sender_id)
            if not s or s.sid != parts[1]:
                await ev.answer("Not your quiz / already ended.", alert=True)
                return
            await ev.answer("Quiz stopped.")
            await finish(s)
        elif key == "w":
            await on_wizard(ev, parts)
        elif key == "p":
            await ev.answer()
            fn = ACTIONS.get(parts[1])
            if fn:
                await fn(ev)
        elif key == "lb":
            _, exam, period = parts
            with contextlib.suppress(Exception):
                await ev.edit(leaderboard_text(exam, period, ev.sender_id),
                              buttons=leaderboard_buttons(exam, period), parse_mode="html")
            await ev.answer()
        elif key == "bk":
            await on_book_cb(ev, parts)
        elif key == "up":
            await on_upload_cb(ev, parts)
        else:
            await ev.answer()
    except Exception:
        log.exception("callback error")
        with contextlib.suppress(Exception):
            await ev.answer("Something went wrong.", alert=True)


# ----------------------------------------------------------------------------
# STARTUP
# ----------------------------------------------------------------------------
async def health_server():
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return

    async def handle(reader, writer):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(reader.read(1024), 3)
            body = b"NEET/JEE Quiz Bot is running"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n%s" % (len(body), body))
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()

    try:
        await asyncio.start_server(handle, "0.0.0.0", port)
        log.info("Health server on port %s", port)
    except OSError as e:
        log.warning("Health server not started (port busy?): %s", e)


async def main():
    global HTTP, GROQ_SEM, JOBS
    init_db()
    HTTP = httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0))
    GROQ_SEM = asyncio.Semaphore(2)
    JOBS = asyncio.Queue()
    if not OWNER_IDS:
        log.warning("OWNER_ID is not set - nobody can upload books!")
    if not GROQ_API_KEY:
        log.warning("GROQ_API_KEY is not set - AI features will fail.")
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    log.info("Bot started as @%s | data dir: %s", me.username, DATA_DIR)
    with contextlib.suppress(Exception):
        await client(functions.bots.SetBotCommandsRequest(
            scope=types.BotCommandScopeDefault(), lang_code="",
            commands=[types.BotCommand(c, d) for c, d in [
                ("start", "Welcome & panel"), ("panel", "Open main panel"), ("uploadbook", "Upload a book (owner)"),
                ("library", "Question library"), ("makequiz", "Create a custom quiz"), ("neet", "NEET quizzes"),
                ("jee", "JEE quizzes"), ("original", "Original questions mode"), ("generated", "AI generated mode"),
                ("chapter", "Chapter-wise quiz"), ("mocktest", "Full mock test"), ("mistakes", "My mistakes"),
                ("revision", "Revise wrong questions"), ("leaderboard", "Leaderboard"), ("score", "My score"),
                ("stop", "Stop current quiz")]]))
    # recover interrupted work
    db_run("UPDATE books SET status='failed', status_msg='Interrupted while downloading - upload again' "
           "WHERE status='downloading'")
    for b in db_all("SELECT id FROM books WHERE status IN ('queued','extracting','processing')"):
        await JOBS.put({"type": "process", "book_id": b["id"], "chat": None})
    asyncio.create_task(worker())
    asyncio.create_task(janitor())
    await health_server()
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        pass
