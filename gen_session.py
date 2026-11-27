# Apne phone (Termux) ya PC par chalao, Render par nahi.
# pip install telethon
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

API_ID = int(input("API_ID: ").strip())
API_HASH = input("API_HASH: ").strip()

with TelegramClient(StringSession(), API_ID, API_HASH,
                    device_model="Samsung Galaxy S23",
                    system_version="Android 14",
                    app_version="10.14.5") as client:
    print("\nSESSION STRING (kisi ko mat dena):\n")
    print(client.session.save())
