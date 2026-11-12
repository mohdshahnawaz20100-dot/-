"""JARVIS ULTRA add-on module.

Place beside bot.py and register with:
    from bot4 import register_bot4
    register_bot4(bot, OWNER_ID)

Optional packages: yt-dlp, youtube-transcript-api, pypdf.
Uses existing GROQ_API_KEY for AI summaries/answers. No video/audio downloading.
"""
from __future__ import annotations

import os
import re
import io
import html
import asyncio
import logging
from urllib.parse import urlparse

import requests
from telethon import events, functions, Button

log = logging.getLogger("jarvis.bot4")
MAX_TEXT = 12000


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _video_id(value: str) -> str | None:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    try:
        u = urlparse(value)
        host = (u.netloc or "").lower().replace("www.", "")
        if host == "youtu.be":
            return u.path.strip("/").split("/")[0][:11] or None
        if host.endswith("youtube.com"):
            if u.path == "/watch":
                from urllib.parse import parse_qs
                return parse_qs(u.query).get("v", [None])[0]
            parts = [p for p in u.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
                return parts[1][:11]
    except Exception:
        pass
    return None


def _yt_extract(target: str, search: bool = False):
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("YouTube tools ke liye `yt-dlp` install karo: pip install yt-dlp")
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist" if search else False,
            "socket_timeout": 15, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(("ytsearch5:" + target) if search else target, download=False)


def _transcript(video_id: str) -> str:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        raise RuntimeError("Transcript ke liye `youtube-transcript-api` install karo.")
    # Support both current and older youtube-transcript-api versions.
    try:
        api = YouTubeTranscriptApi()
        result = api.fetch(video_id, languages=["en", "hi", "ur"])
        items = result.to_raw_data() if hasattr(result, "to_raw_data") else result
    except Exception:
        try:
            result = YouTubeTranscriptApi.get_transcript(video_id, languages=["en", "hi", "ur"])
            items = result
        except Exception as exc:
            raise RuntimeError("Is video ka accessible transcript/captions nahi mile. Creator captions off kar sakta hai.") from exc
    lines = []
    for item in items:
        if isinstance(item, dict):
            t = _clean(item.get("text", ""))
        else:
            t = _clean(getattr(item, "text", ""))
        if t:
            lines.append(t)
    return " ".join(lines)[:MAX_TEXT]


def _groq(prompt: str, system: str = "You are JARVIS, a precise and helpful assistant. Reply in the user's language.") -> str:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GROQ_API_KEY Render Environment mein set nahi hai.")
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {"model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": prompt[:MAX_TEXT]}],
               "temperature": 0.35, "max_tokens": 1400}
    r = requests.post(url, headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"},
                      json=payload, timeout=45)
    if r.status_code != 200:
        raise RuntimeError("AI service error: " + str(r.status_code) + " " + _clean(r.text)[:180])
    return _clean(r.json()["choices"][0]["message"]["content"])


def _format_video(info: dict) -> str:
    title = _clean(info.get("title")) or "Unknown title"
    channel = _clean(info.get("channel") or info.get("uploader")) or "Unknown channel"
    duration = int(info.get("duration") or 0)
    mins, secs = divmod(duration, 60)
    url = info.get("webpage_url") or ("https://www.youtube.com/watch?v=" + str(info.get("id", "")))
    return (f"🎬 **{title[:180]}**\n👤 Channel: {channel[:100]}\n"
            f"⏱ Duration: {mins}:{secs:02d}\n👁 Views: {info.get('view_count') or 'N/A'}\n"
            f"📅 Upload: {info.get('upload_date') or 'N/A'}\n🔗 {url}")


def register_bot4(client, owner_id=0):
    """Register independent JARVIS ULTRA commands on an existing Telethon client."""
    owner_id = int(owner_id or 0)

    async def safe_reply(event, text):
        text = str(text or "")
        # Telegram message limit safety; split long plain text into chunks.
        chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or ["No result."]
        for chunk in chunks:
            await event.reply(chunk, link_preview=False)

    menu_text = (
        "✨ **JARVIS COMMAND PANEL**\n\n"
        "Neeche category button dabao aur naye commands dekho."
    )

    def menu_buttons():
        return [
            [Button.inline("🧠 AI Commands", b"jarvis4:ai"), Button.inline("▶️ YouTube", b"jarvis4:yt")],
            [Button.inline("🔗 Link Tools", b"jarvis4:links"), Button.inline("📄 PDF Tools", b"jarvis4:pdf")],
            [Button.inline("🤖 Telegram Tools", b"jarvis4:telegram")],
        ]

    @client.on(events.NewMessage(pattern=r"(?i)^/(?:panel|new|newcommands|help4)(?:@\w+)?$"))
    async def new_commands_menu(event):
        await event.reply(menu_text, buttons=menu_buttons())

    @client.on(events.NewMessage(pattern=r"(?i)^/(?:ultra|bot4|ultrahelp)(?:\s|$)"))
    async def ultra_help(event):
        await event.reply(menu_text, buttons=menu_buttons())

    @client.on(events.CallbackQuery(data=re.compile(rb"^jarvis4:")))
    async def new_commands_callback(event):
        key = event.data.decode("utf-8", "ignore").split(":", 1)[-1]
        pages = {
            "ai": "🧠 **AI COMMANDS**\n\n/ask `question` — AI se jawab\n/solve `problem` — step-by-step solution\n/research `topic` — research brief",
            "yt": "▶️ **YOUTUBE COMMANDS**\n\n/ytsearch `topic` — videos search\n/ytinfo `URL` — video details\n/yttranscript `URL` — available captions\n/ytsummary `URL` — video summary\n/ytnotes `URL` — study notes\n/ytquiz `URL` — MCQ quiz",
            "links": "🔗 **LINK COMMANDS**\n\n/short `URL` — short link\n/expand `URL` — final destination\n/linkinfo `URL` — domain and HTTPS info",
            "pdf": "📄 **PDF COMMANDS**\n\nPDF par reply karke /pdf — summary\nPDF par reply karke /pdfnotes — notes",
            "telegram": "🤖 **TELEGRAM COMMANDS**\n\n/botinfo `@username` — public bot profile details",
        }
        if key == "home":
            await event.edit(menu_text, buttons=menu_buttons())
        elif key in pages:
            await event.edit(pages[key], buttons=[[Button.inline("⬅️ Back to Panel", b"jarvis4:home")]])
        else:
            await event.answer("Unknown menu", alert=True)

    async def ai_command(event, mode):
        query = (event.pattern_match.group(1) or "").strip()
        if not query and event.is_reply:
            reply = await event.get_reply_message()
            query = (reply.raw_text or "").strip() if reply else ""
        if not query:
            return await event.reply(f"Usage: `/{mode} your question or topic`")
        msg = await event.reply("🧠 JARVIS is analysing...")
        try:
            instruction = {
                "ask": "Answer clearly, accurately and practically. Explain complex points simply.",
                "solve": "Solve step by step. State assumptions and verify the result.",
                "research": "Give a structured research brief. Separate established facts from uncertainty; do not invent sources or claim live web access."
            }[mode]
            answer = await asyncio.to_thread(_groq, query, instruction + " Reply in the language of the user's request.")
            await msg.edit("🧠 **JARVIS " + mode.upper() + "**\n\n" + answer[:3900])
        except Exception as exc:
            await msg.edit("❌ " + str(exc)[:700])

    for mode in ("ask", "solve", "research"):
        pattern = rf"(?is)^/{mode}(?:@\w+)?(?:\s+(.+))?$"
        async def handler(event, _mode=mode):
            await ai_command(event, _mode)
        client.add_event_handler(handler, events.NewMessage(pattern=pattern))

    @client.on(events.NewMessage(pattern=r"(?is)^/ytsearch(?:@\w+)?\s+(.+)$"))
    async def ytsearch(event):
        query = _clean(event.pattern_match.group(1))[:180]
        status = await event.reply("🔎 YouTube par search kar raha hoon...")
        try:
            data = await asyncio.to_thread(_yt_extract, query, True)
            entries = data.get("entries") or []
            out = ["▶️ **YouTube Search Results**"]
            for i, item in enumerate(entries[:5], 1):
                vid = item.get("id")
                url = item.get("url") or ("https://www.youtube.com/watch?v=" + str(vid))
                out.append(f"\n{i}. **{_clean(item.get('title'))[:150]}**\n👤 {_clean(item.get('channel') or item.get('uploader'))[:80]}\n{url}")
            await status.edit("\n".join(out) if entries else "Koi result nahi mila.", link_preview=False)
        except Exception as exc:
            await status.edit("❌ " + str(exc)[:700])

    async def get_video_from_event(event):
        raw = (event.pattern_match.group(1) or "").strip()
        if not raw and event.is_reply:
            rep = await event.get_reply_message()
            raw = rep.raw_text or "" if rep else ""
        match = re.search(r"https?://\S+", raw)
        if not match:
            raise RuntimeError("YouTube URL do. Example: `/ytinfo https://youtu.be/VIDEO_ID`")
        url = match.group(0).rstrip(").,]")
        vid = _video_id(url)
        if not vid:
            raise RuntimeError("Valid YouTube video URL nahi mila.")
        return url, vid

    @client.on(events.NewMessage(pattern=r"(?is)^/ytinfo(?:@\w+)?(?:\s+(.+))?$"))
    async def ytinfo(event):
        try:
            url, vid = await get_video_from_event(event)
            info = await asyncio.to_thread(_yt_extract, "https://www.youtube.com/watch?v=" + vid)
            await safe_reply(event, _format_video(info))
        except Exception as exc:
            await event.reply("❌ " + str(exc)[:700])

    async def transcript_action(event, action):
        status = await event.reply("⏳ Video captions check kar raha hoon...")
        try:
            url, vid = await get_video_from_event(event)
            transcript = await asyncio.to_thread(_transcript, vid)
            if action == "transcript":
                await status.edit("📝 **Available video transcript**\n\n" + transcript[:3900])
                return
            prompts = {
                "summary": "Is transcript ka accurate, well-structured summary Hindi/Hinglish mein do. Main points aur conclusion include karo.",
                "notes": "Is transcript se chapter-wise style mein clean study notes banao: headings, definitions, key points, examples, and quick revision.",
                "quiz": "Is transcript ke basis par 10 objective MCQs banao, har question ke 4 options aur answer + short explanation do. Sirf transcript-supported facts use karo."
            }
            answer = await asyncio.to_thread(_groq, prompts[action] + "\n\nTranscript:\n" + transcript)
            await status.edit({"summary":"📌 **VIDEO SUMMARY**\n\n", "notes":"📚 **VIDEO NOTES**\n\n", "quiz":"🧩 **VIDEO QUIZ**\n\n"}[action] + answer[:3900])
        except Exception as exc:
            await status.edit("❌ " + str(exc)[:700])

    for action in ("transcript", "summary", "notes", "quiz"):
        cmd = {"transcript":"yttranscript", "summary":"ytsummary", "notes":"ytnotes", "quiz":"ytquiz"}[action]
        async def yt_handler(event, _action=action):
            await transcript_action(event, _action)
        client.add_event_handler(yt_handler, events.NewMessage(pattern=rf"(?is)^/{cmd}(?:@\w+)?(?:\s+(.+))?$"))

    @client.on(events.NewMessage(pattern=r"(?is)^/short(?:@\w+)?\s+(https?://\S+)$"))
    async def short_link(event):
        url = event.pattern_match.group(1).rstrip(".,)")
        try:
            r = await asyncio.to_thread(requests.get, "https://tinyurl.com/api-create.php", params={"url": url}, timeout=20)
            if r.status_code != 200 or not r.text.startswith("http"):
                raise RuntimeError("Shortener ne link accept nahi kiya.")
            await event.reply("🔗 **Short link:**\n" + r.text.strip(), link_preview=False)
        except Exception as exc:
            await event.reply("❌ " + str(exc)[:400])

    @client.on(events.NewMessage(pattern=r"(?is)^/expand(?:@\w+)?\s+(https?://\S+)$"))
    async def expand_link(event):
        url = event.pattern_match.group(1)
        try:
            r = await asyncio.to_thread(requests.get, url, allow_redirects=True, timeout=15,
                                        headers={"User-Agent":"Mozilla/5.0"}, stream=True)
            await event.reply("↗️ **Final destination:**\n" + r.url[:1000], link_preview=False)
            r.close()
        except Exception as exc:
            await event.reply("❌ Link resolve nahi hua: " + str(exc)[:300])

    @client.on(events.NewMessage(pattern=r"(?is)^/linkinfo(?:@\w+)?\s+(https?://\S+)$"))
    async def link_info(event):
        url = event.pattern_match.group(1)
        try:
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname:
                raise ValueError("Invalid URL")
            await event.reply(f"🔗 **LINK INFO**\n🌐 Domain: `{p.hostname}`\n🔒 HTTPS: `{p.scheme == 'https'}`\n⚠️ Unknown links par password/OTP ya personal details mat dena.")
        except Exception as exc:
            await event.reply("❌ " + str(exc)[:250])

    @client.on(events.NewMessage(pattern=r"(?i)^/botinfo(?:@\w+)?\s+(@?[A-Za-z0-9_]{4,})$"))
    async def bot_info(event):
        username = event.pattern_match.group(1).lstrip("@")
        try:
            entity = await client.get_entity(username)
            if not getattr(entity, "bot", False):
                return await event.reply("Ye public username Telegram bot account nahi lagta.")
            first = _clean(getattr(entity, "first_name", ""))
            uname = getattr(entity, "username", username)
            bot_id = getattr(entity, "id", "N/A")
            about = ""
            try:
                full = await client(functions.users.GetFullUserRequest(entity))
                about = _clean(getattr(full.full_user, "about", ""))
            except Exception:
                pass
            await event.reply(f"🤖 **PUBLIC BOT PROFILE**\nName: {first}\nUsername: `@{uname}`\nID: `{bot_id}`\nAbout: {about[:700] or 'Public bio unavailable'}\n\nPrivate commands, hidden settings aur source code public profile se access nahi hote.")
        except Exception as exc:
            await event.reply("❌ Public bot details nahi mil sake: " + str(exc)[:350])

    async def pdf_action(event, notes=False):
        reply = await event.get_reply_message()
        if not reply or not reply.document:
            return await event.reply("Kisi PDF document par reply karke `/pdf` ya `/pdfnotes` likho.")
        status = await event.reply("📄 PDF read kar raha hoon...")
        path = None
        try:
            try:
                from pypdf import PdfReader
            except ImportError:
                raise RuntimeError("PDF tools ke liye `pypdf` install karo.")
            path = await reply.download_media(file=bytes)
            if not path:
                raise RuntimeError("PDF download nahi ho payi.")
            reader = PdfReader(io.BytesIO(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:40])[:MAX_TEXT]
            if not text.strip():
                raise RuntimeError("PDF mein selectable text nahi mila. Scanned PDF ke liye OCR chahiye.")
            prompt = ("Is PDF content ka structured study notes banao with headings and key points." if notes
                      else "Is PDF ka concise but useful summary banao, important points include karo.")
            answer = await asyncio.to_thread(_groq, prompt + "\n\n" + text)
            await status.edit(("📚 **PDF NOTES**\n\n" if notes else "📄 **PDF SUMMARY**\n\n") + answer[:3900])
        except Exception as exc:
            await status.edit("❌ " + str(exc)[:700])

    @client.on(events.NewMessage(pattern=r"(?i)^/pdf(?:@\w+)?$"))
    async def pdf_summary(event):
        await pdf_action(event, False)

    @client.on(events.NewMessage(pattern=r"(?i)^/pdfnotes(?:@\w+)?$"))
    async def pdf_notes(event):
        await pdf_action(event, True)

    print("JARVIS ULTRA bot4 registered: AI, YouTube, links, public bot info and PDF tools.")
    return True
