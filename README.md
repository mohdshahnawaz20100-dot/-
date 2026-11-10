# 🤖 CODERNOVA Multi-User Userbot

Complete multi-user Telegram userbot with AI, scheduler, raids, and 100+ commands.

## ✨ Features

- 🔐 Multi-user login (phone + OTP + 2FA)
- 🤖 AI Assistant (Groq GPT-OSS-120B)
- ⏰ Perfect-timing scheduler (IST)
- 👻 Ghost mode
- 🎭 Clone (single / full)
- 🎯 Raid engine
- ⚡ FastGC
- 📥 Media saver (album support)
- 🎵 Music tools
- 🎨 AI image generation
- 🌍 Weather, News, AQI
- 🛡️ PM Guard, Auto-reply, AI-reply
- 👑 Owner panel

## 🚀 Setup

### 1. Get credentials
- **API_ID/API_HASH** → my.telegram.org
- **BOT_TOKEN** → @BotFather
- **OWNER_ID** → @userinfobot
- **MONGO_URI** → mongodb.com/atlas (free)
- **GROQ_API_KEY** → console.groq.com

### 2. Install
```bash
git clone <repo>
cd codernova-userbot
pip install -r requirements.txt
cp .env.example .env
# Edit .env
python3 bot.py
