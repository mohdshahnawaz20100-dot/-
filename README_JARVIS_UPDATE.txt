JARVIS UPDATE — bot.py + bot3.py

FILES
- bot.py: updated main bot file. It loads bot3.py and enables group question auto-replies.
- bot3.py: JARVIS AI, owner /reply, natural Hinglish style, English explanations, advanced inline-button group quiz.

KEEP
- Keep your existing bot_ai.py unchanged in the SAME root folder as bot.py and bot3.py.
- Keep requirements.txt and your existing Render environment variables unchanged.
- Do not upload these files inside a subfolder. The filenames must be exactly bot.py and bot3.py (lowercase), side by side.

NEW FEATURES
- Owner can reply to a member's message with /reply (or .reply); JARVIS answers that message.
- Owner can also use /reply <question> without replying to a message.
- The previous reply-raid command is preserved as .replyraid so it does not conflict with JARVIS /reply.
- JARVIS answers group questions, mentions and replies to its/owner's messages. It uses a casual Hinglish style and adds a simple English explanation for factual/academic questions.
- Group quiz uses tappable A/B/C/D inline buttons, original moderately advanced questions, per-member scoring, English explanations and Telegram sticker/emoji reactions.
- DM quiz is not included. Direct-message auto-replies remain controlled by the existing globaldm setting (OFF by default).

QUIZ COMMANDS (owner)
/quizstart [neet|jee|cuet|mixed]
/quizstop
/quizstatus
/quizscore

OWNER REPLY EXAMPLES
1. Reply to someone's message and send: /reply
2. Or send: /reply Explain photosynthesis in simple words

DEPLOY
1. Replace bot.py and bot3.py in the repository root with these exact files.
2. Keep bot_ai.py unchanged beside them.
3. Commit/push to GitHub and redeploy Render.

NOTE
For the bot to read ordinary group questions, disable Group Privacy for the bot in BotFather or give it the required group permissions. Otherwise Telegram may only deliver commands, mentions and replies.
AI replies depend on a working GROQ_API_KEY or OPENROUTER_API_KEY. JARVIS cannot guarantee a correct answer to every possible question; it is instructed to say when it is unsure.
