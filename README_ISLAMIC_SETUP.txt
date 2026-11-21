JARVIS Islamic module setup

Files:
- bot.py: your uploaded main bot.py with one registration added
- bot_islamic.py: separate Islamic command module
- islamic_media/voice/: put authorized audio files here
- islamic_media/video/: put authorized video files here

Commands:
  /islamic
  /shayari
  /iqbal
  /quran
  /hadith
  /dua
  /voice peer
  /voice iqbal
  /voice salman
  /video peer
  /video iqbal
  /video salman

Direct media:
The bot sends a media file directly in Telegram (not a URL) only when you supply
an authorized recording/video in the matching folder. This package does not
contain recordings of real scholars or claim to reproduce their voices.

Filename examples:
  islamic_media/voice/peer_ajmal.mp3
  islamic_media/voice/allama_iqbal.mp3
  islamic_media/voice/mufti_salman_azhari.mp3
  islamic_media/video/peer_ajmal.mp4
  islamic_media/video/allama_iqbal.mp4
  islamic_media/video/mufti_salman_azhari.mp4

Do not upload recordings unless you have permission or they are lawfully
available for redistribution. Do not use AI voice cloning to impersonate
real people.

The included poetry is original except the short public-domain Iqbal verse.
Quran and hadith examples are only a small starter selection, not the complete
Quran or all hadith. Verify translations and references against trusted editions.
