"""
JARVIS Islamic content module.
Commands are registered on the existing Telethon bot client.
Media is sent directly in Telegram from locally supplied, authorized files.
"""
from pathlib import Path
import random
from telethon import events

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "islamic_media"

# Original poetry written for this module; not attributed to living scholars.
ORIGINAL_SHAYARI = [
    ("Sabr", "Raat kitni bhi gehri ho, subah ka raasta band nahi hota;\nJo Rab par bharosa rakhe, woh tanha kabhi nahi hota."),
    ("Tauba", "Gunahon ka bojh lekar bhi uske dar par aa jaana;\nRehmat ka samandar hai, bas sachche dil se laut aana."),
    ("Maa", "Maa ki dua ka saya ho to raaste roshan rehte hain;\nUske khamosh aansu bhi Rab ke huzoor bayan rehte hain."),
    ("Zindagi", "Duniya ki taareef se apna qeemti waqt na tol;\nKirdaar aisa bana ke khamoshi bhi de paaye bol."),
    ("Tawakkul", "Jab apni tadbeer ke saare dar band nazar aayein;\nDua ke haath uthana, Rab naye raaste dikhaye."),
    ("Akhlaq", "Ibadat ka noor chehre se zyada kirdaar mein dikhe;\nZubaan se aman nikle aur dil mein insaaniyat rahe.")
]

IQBAL_PUBLIC_DOMAIN = [
    "Khudi ko kar buland itna ke har taqdeer se pehle,\nKhuda bande se khud poochhe, bata teri raza kya hai?\n— Allama Muhammad Iqbal"
]

QURAN_SAMPLES = [
    ("Surah Ash-Sharh 94:5–6", "فَإِنَّ مَعَ الْعُسْرِ يُسْرًا ۝ إِنَّ مَعَ الْعُسْرِ يُسْرًا",
     "Pas beshak mushkil ke saath aasani hai. Beshak mushkil ke saath aasani hai."),
    ("Surah Az-Zumar 39:53", "لَا تَقْنَطُوا مِن رَّحْمَةِ اللَّهِ",
     "Allah ki rehmat se mayoos na ho. (Ayat ka mukhtasar hissa)")
]

HADITH_SAMPLES = [
    ("Sahih al-Bukhari 1", "Aamaal ka daromadar niyyat par hai.",
     "Yeh mafhoom hadees ka mukhtasar tarjuma hai; poora matn aur sharh ke liye asal kitab dekhein."),
    ("Sahih al-Bukhari 6029", "Jo Allah aur Aakhirat par iman rakhta ho, woh achhi baat kahe ya khamosh rahe.",
     "Mafhoomi tarjuma; edition ke mutabiq numbering/alfaz mein farq ho sakta hai.")
]

DUAS = [
    ("رَبِّ زِدْنِي عِلْمًا", "Rabbi zidni ilma", "Aye mere Rab, mere ilm mein izafa farma. (Quran 20:114)"),
    ("رَبَّنَا آتِنَا فِي الدُّنْيَا حَسَنَةً وَفِي الْآخِرَةِ حَسَنَةً وَقِنَا عَذَابَ النَّارِ",
     "Rabbana atina fid-dunya hasanatan wa fil-akhirati hasanatan wa qina adhaban-nar",
     "Aye hamare Rab, humein duniya mein bhalai de, Aakhirat mein bhalai de aur aag ke azaab se bacha. (Quran 2:201)")
]

PEOPLE = {
    "peer": "peer_ajmal",
    "ajmal": "peer_ajmal",
    "peer_ajmal": "peer_ajmal",
    "iqbal": "allama_iqbal",
    "allama_iqbal": "allama_iqbal",
    "salman": "mufti_salman_azhari",
    "azhari": "mufti_salman_azhari",
    "salman_azhari": "mufti_salman_azhari",
    "mufti_salman": "mufti_salman_azhari",
}

def _media_file(kind: str, person: str):
    key = PEOPLE.get((person or "").lower().replace("-", "_").strip())
    if not key:
        return None
    folder = MEDIA_DIR / kind
    for ext in (".mp3", ".m4a", ".ogg", ".wav", ".mp4", ".mov", ".mkv"):
        candidate = folder / f"{key}{ext}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None

def register_islamic(bot, owner_id):
    """Register public Islamic text commands and safe direct-media delivery."""
    @bot.on(events.NewMessage(pattern=r"^/islamic(?:@\w+)?$"))
    async def islamic_home(event):
        await event.reply(
            "🌙 **JARVIS Islamic Center**\\n\\n"
            "/shayari — Original Islamic poetry\\n"
            "/iqbal — Allama Iqbal\\n"
            "/quran — Selected Quran ayat with translation\\n"
            "/hadith — Selected hadees with reference\\n"
            "/dua — Dua with meaning\\n"
            "/voice peer|iqbal|salman — Send available audio\\n"
            "/video peer|iqbal|salman — Send available video\\n\\n"
            "Media tabhi bheja jayega jab authorized file `islamic_media` folder mein maujood ho."
        )

    @bot.on(events.NewMessage(pattern=r"^/shayari(?:@\w+)?$"))
    async def shayari_cmd(event):
        title, poem = random.choice(ORIGINAL_SHAYARI)
        await event.reply(f"🌙 **{title}**\\n\\n{poem}\\n\\n_Original JARVIS collection_")

    @bot.on(events.NewMessage(pattern=r"^/iqbal(?:@\w+)?$"))
    async def iqbal_cmd(event):
        await event.reply("📜 **Allama Muhammad Iqbal**\\n\\n" + random.choice(IQBAL_PUBLIC_DOMAIN))

    @bot.on(events.NewMessage(pattern=r"^/quran(?:@\w+)?$"))
    async def quran_cmd(event):
        ref, arabic, meaning = random.choice(QURAN_SAMPLES)
        await event.reply(f"📖 **Quran Majeed — {ref}**\\n\\n{arabic}\\n\\n**Mafhoom (Hindi):** {meaning}\\n\\n_Translation is a rendering of the meaning; consult a trusted Quran edition for study._")

    @bot.on(events.NewMessage(pattern=r"^/hadith(?:@\w+)?$"))
    async def hadith_cmd(event):
        ref, meaning, note = random.choice(HADITH_SAMPLES)
        await event.reply(f"📚 **Hadees — {ref}**\\n\\n{meaning}\\n\\n_{note}_")

    @bot.on(events.NewMessage(pattern=r"^/dua(?:@\w+)?$"))
    async def dua_cmd(event):
        arabic, transliteration, meaning = random.choice(DUAS)
        await event.reply(f"🤲 **Dua**\\n\\n{arabic}\\n\\n{transliteration}\\n\\n{meaning}")

    async def send_media(event, kind):
        parts = (event.raw_text or "").split(maxsplit=1)
        if len(parts) < 2:
            await event.reply(f"Usage: `/{kind} peer` ya `/{kind} iqbal` ya `/{kind} salman`")
            return
        person = parts[1].strip().split()[0]
        media = _media_file(kind, person)
        if not media:
            await event.reply(
                f"📭 {person} ki authorized {kind} file abhi available nahi hai.\\n"
                f"Owner ko file `islamic_media/{kind}/` folder mein is naam se add karni hogi: "
                f"`peer_ajmal`, `allama_iqbal`, ya `mufti_salman_azhari`."
            )
            return
        try:
            await bot.send_file(
                event.chat_id, str(media),
                caption=f"🌙 Islamic Media • {person.replace('_', ' ').title()}",
                reply_to=event.message.id
            )
        except Exception as exc:
            await event.reply(f"Media send nahi ho paya: {str(exc)[:160]}")

    @bot.on(events.NewMessage(pattern=r"^/voice(?:@\w+)?(?:\s+.*)?$"))
    async def voice_cmd(event):
        await send_media(event, "voice")

    @bot.on(events.NewMessage(pattern=r"^/video(?:@\w+)?(?:\s+.*)?$"))
    async def video_cmd(event):
        await send_media(event, "video")
