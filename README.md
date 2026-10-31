# RecapStudio (working name) — Movie Recap Website

Original-design movie recap tool built 2026-10-31. NOT a copy of any other site.

## Functions
- Register / login (first account becomes admin), 100 signup credits
- Projects: paste a video link (yt-dlp) or upload a file (up to 1 GB)
- Pipeline: ffmpeg audio extract -> local Whisper transcription (tiny/base) ->
  recap script -> optional translation (Burmese/English) -> AI voiceover
  (Edge voices EN + MY, offline espeak-ng backup voice) -> timed subtitles ->
  9:16 subtitled MP4 render with title/description/hashtags
- Credits: 10 per processed minute, auto-refund when a job fails
- Admin: add credits, choose Whisper model, optional OpenAI-compatible API key
  for real AI recap rewriting (without a key: free basic timeline recap mode)

## Run locally
    python3 -m venv venv && venv/bin/pip install -r requirements.txt
    venv/bin/python app.py            # http://127.0.0.1:5100
    # optional backup voice: apt-get install espeak-ng ; ffmpeg required

## Deploy (free, when ready)
Same pattern as the shop bot: push to GitHub, Render Web Service with
DATABASE on disk or Postgres. Note: transcription + rendering are CPU-heavy,
so Render free tier will be slow; a small VPS is better once selling.

## Honest limits of v1
- Voice cloning, TikTok/YouTube auto-posting/scheduling: not built yet (phase 2).
- Subtitle burn-in needs an ffmpeg with libass (present here; check on deploy host).
- Only recap content you have the right to use.

## Build notes (verified 2026-10-31)
- End-to-end test passed: 34s sample speech clip -> transcript -> recap script ->
  backup-voice narration -> subtitled 720x1280 MP4 in under a minute.
- Whisper model is vendored at models/faster-whisper-tiny (75 MB) because runtime
  downloads from Hugging Face were unreliable here; drop in a "base" folder the
  same way for better accuracy.
- Transcription feeds Whisper a numpy array decoded with the stdlib wave module,
  because the installed PyAV (av 19) removed the metadata_errors argument that
  faster-whisper 1.2.1 passes to av.open().
- Backup voice: espeak-ng is vendored under tools/ (extracted .debs + wrapper)
  because system packages do not survive VM replacement on this machine.
  Edge-TTS remains the primary voice on normal hosts (this sandbox's proxy
  blocks its WebSocket handshake).
- Base model added 2026-10-31 (models/faster-whisper-base, 142 MB) and set as the
  site default in Admin settings. On the Sintel test scene it transcribes the
  opening lines exactly ("This blade has a dark past. It has shed much innocent
  blood.") where tiny garbled them; ~14s load+transcribe per minute of audio.
