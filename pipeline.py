"""Recap pipeline: download -> transcribe -> recap script -> translate -> TTS -> render.
Free/local by default. If an OpenAI-compatible API key is saved in settings, it is used
for a real AI recap rewrite + translation; otherwise a timeline extractive recap is used.
"""
import json, math, os, re, subprocess, threading, time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
_lock = threading.Lock()
_whisper = {"model": None, "name": None}

def _run(cmd, timeout=1800):
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "command failed")[-1500:])
    return r

def _probe_duration(path):
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", path])
    return float(json.loads(r.stdout)["format"]["duration"])

def _get_whisper(name):
    from faster_whisper import WhisperModel
    local = os.path.join(BASE, "models", f"faster-whisper-{name}")
    source = local if os.path.exists(os.path.join(local, "model.bin")) else name
    with _lock:
        if _whisper["model"] is None or _whisper["name"] != source:
            _whisper["model"] = WhisperModel(source, device="cpu", compute_type="int8")
            _whisper["name"] = source
        return _whisper["model"]

def _srt_time(sec):
    h = int(sec // 3600); m = int((sec % 3600) // 60); s = int(sec % 60); ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def write_srt(segments, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, sg in enumerate(segments, 1):
            f.write(f"{i}\n{_srt_time(sg['start'])} --> {_srt_time(sg['end'])}\n{sg['text'].strip()}\n\n")

def extractive_recap(segments, target_words=220):
    """Pick representative sentences spread across the timeline."""
    sents = []
    for sg in segments:
        for part in re.split(r"(?<=[.!?])\s+", sg["text"].strip()):
            if len(part.split()) >= 4:
                sents.append((sg["start"], part.strip()))
    if not sents:
        return ""
    sents.sort()
    total = sum(len(t.split()) for _, t in sents)
    if total <= target_words:
        return " ".join(t for _, t in sents)
    n = len(sents); picked = []
    step = max(1, n // 14)
    for i in range(0, n, step):
        picked.append(sents[i][1])
        if sum(len(t.split()) for t in picked) >= target_words:
            break
    return " ".join(picked)

def ai_rewrite(text, lang_name, api_base, api_key, model):
    """OpenAI-compatible chat completion. Returns None on failure."""
    prompt = ("Rewrite the following video transcript as an engaging movie recap narration script "
              f"in {lang_name}, about 200-260 words, present tense, no scene directions, no markdown. "
              "Keep the story order and the most dramatic moments.\n\nTRANSCRIPT:\n" + text[:12000])
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0.7}).encode()
    req = urllib.request.Request(api_base.rstrip("/") + "/chat/completions", data=body,
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = json.loads(r.read())
    return data["choices"][0]["message"]["content"].strip()

def translate_text(text, dest):
    try:
        from deep_translator import GoogleTranslator
        out = []
        for i in range(0, len(text), 4000):
            out.append(GoogleTranslator(source="auto", target=dest).translate(text[i:i+4000]) or "")
        result = " ".join(out).strip()
        if result:
            return result
        raise RuntimeError("empty translation")
    except Exception:
        # Fallback provider: MyMemory free API (chunked to its 500-char query limit)
        import requests
        out = []
        for i in range(0, len(text), 450):
            r = requests.get("https://api.mymemory.translated.net/get",
                             params={"q": text[i:i+450], "langpair": f"en|{dest}"}, timeout=30)
            data = r.json()
            translated = (data.get("responseData") or {}).get("translatedText") or ""
            if "MYMEMORY WARNING" in translated or "QUERY LENGTH LIMIT" in translated:
                raise RuntimeError("MyMemory quota exhausted")
            out.append(translated)
        result = " ".join(out).strip()
        if not result:
            raise RuntimeError("translation failed on all providers")
        return result

VOICES = {
    "en-male": "en-US-GuyNeural", "en-female": "en-US-JennyNeural",
    "my-male": "my-MM-ThihaNeural", "my-female": "my-MM-NilarNeural",
}

def run_pipeline(job, settings, log):
    """job: dict with id, source_type, source_value, workdir, voice, target_lang, max_minutes"""
    wd = job["workdir"]; os.makedirs(wd, exist_ok=True)
    src = os.path.join(wd, "source.mp4")
    if job["source_type"] == "url":
        log("Downloading video from link...")
        from yt_dlp import YoutubeDL
        with YoutubeDL({"outtmpl": src, "format": "mp4/best[height<=720]/best", "quiet": True, "noplaylist": True}) as ydl:
            ydl.download([job["source_value"]])
    else:
        src = job["source_value"]
    dur = _probe_duration(src)
    use_dur = min(dur, job["max_minutes"] * 60)
    log(f"Video ready ({dur:.0f}s). Extracting audio...")
    audio = os.path.join(wd, "audio.wav")
    _run(["ffmpeg", "-y", "-i", src, "-t", str(use_dur), "-vn", "-ar", "16000", "-ac", "1", audio])
    log("Transcribing speech (local Whisper)...")
    model = _get_whisper(settings.get("whisper_model", "tiny"))
    import wave as _wave, numpy as _np
    with _wave.open(audio, "rb") as wf:  # our ffmpeg output: 16kHz mono 16-bit PCM
        pcm = _np.frombuffer(wf.readframes(wf.getnframes()), dtype=_np.int16).astype(_np.float32) / 32768.0
    segs, info = model.transcribe(pcm, beam_size=1)
    segments = [{"start": s.start, "end": s.end, "text": s.text} for s in segs]
    del pcm
    with _lock:  # free the Whisper model before the memory-hungry render step
        _whisper["model"] = None; _whisper["name"] = None
    import gc; gc.collect()
    transcript = " ".join(s["text"] for s in segments).strip()
    if not transcript:
        raise RuntimeError("No speech was detected in this video. Try a clip with clear dialogue or narration.")
    write_srt(segments, os.path.join(wd, "transcript.srt"))
    with open(os.path.join(wd, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write(transcript)
    log(f"Transcript done ({len(transcript.split())} words, language: {info.language}). Writing recap script...")
    lang_names = {"en": "English", "my": "Burmese (Myanmar)"}
    script = None; mode = "basic"
    base = settings.get("ai_api_base"); key = settings.get("ai_api_key")
    if base and key:
        try:
            script = ai_rewrite(transcript, lang_names.get(job["target_lang"], "English"),
                                base, key, settings.get("ai_model", "gpt-4o-mini"))
            if script: mode = "ai"
        except Exception as e:
            log(f"AI rewrite failed ({type(e).__name__}); using basic recap instead.")
    if not script:
        script = extractive_recap(segments, 240)
        src_lang = info.language
        if job["target_lang"] != src_lang:
            try:
                script = translate_text(script, job["target_lang"]); mode = "basic+translate"
            except Exception:
                log("Translation failed; keeping the original language script.")
    with open(os.path.join(wd, "script.txt"), "w", encoding="utf-8") as f:
        f.write(script)
    log(f"Recap script ready ({mode} mode, {len(script.split())} words). Generating voiceover...")
    import asyncio, edge_tts
    voice = VOICES.get(job["voice"], "en-US-GuyNeural")
    narration = os.path.join(wd, "narration.mp3")
    ESPEAK_VOICES = {"en-male": "en+m3", "en-female": "en+f3", "my-male": "my", "my-female": "my+f3"}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None
    async def _tts():
        kw = {}
        if proxy:
            import ssl as _ssl, aiohttp, edge_tts.communicate as _ec
            kw["proxy"] = proxy
            kw["connector"] = aiohttp.TCPConnector(ssl=_ssl.create_default_context())
            try: _ec._SSL_CTX = _ssl.create_default_context()
            except Exception: pass
        await edge_tts.Communicate(script, voice, **kw).save(narration)
    try:
        asyncio.run(_tts())
    except Exception as e:
        import shutil
        espeak_bin = (os.environ.get("ESPEAK_NG_BIN") or shutil.which("espeak-ng")
                      or os.path.join(BASE, "tools", "espeak-ng"))
        if not (espeak_bin and os.path.exists(espeak_bin)):
            raise
        log(f"Online voice unavailable ({type(e).__name__}); using the offline backup voice.")
        tmp_wav = os.path.join(wd, "narration_espeak.wav")
        with open(os.path.join(wd, "script_for_tts.txt"), "w", encoding="utf-8") as f:
            f.write(script)
        _run([espeak_bin, "-v", ESPEAK_VOICES.get(job["voice"], "en"), "-s", "165",
              "-f", os.path.join(wd, "script_for_tts.txt"), "-w", tmp_wav])
        _run(["ffmpeg", "-y", "-i", tmp_wav, narration])
    ndur = _probe_duration(narration)
    log(f"Voiceover ready ({ndur:.0f}s). Building subtitles and rendering video...")
    words = script.split()
    chunks = [" ".join(words[i:i+9]) for i in range(0, len(words), 9)] or [script]
    step = ndur / len(chunks)
    nsegs = [{"start": i * step, "end": (i + 1) * step, "text": c} for i, c in enumerate(chunks)]
    write_srt(nsegs, os.path.join(wd, "subtitles.srt"))
    out = os.path.join(wd, "recap.mp4")
    sub_path = os.path.join(wd, "subtitles.srt").replace("\\", "/").replace(":", "\\:")
    font_opt = ",FontName=Noto Sans Myanmar" if job["target_lang"] == "my" else ""
    vf = ("scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,"
          f"subtitles='{sub_path}':force_style='FontSize=17{font_opt},PrimaryColour=&HFFFFFF,OutlineColour=&H80000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=90'")
    _run(["ffmpeg", "-y", "-ss", "0", "-i", src, "-i", narration,
          "-filter_complex", f"[0:v]{vf}[v];[0:a]volume=0.12[bg];[1:a]volume=1.0[vo];[bg][vo]amix=inputs=2:duration=first[a]",
          "-map", "[v]", "-map", "[a]", "-t", str(ndur), "-c:v", "libx264", "-preset", "veryfast",
          "-c:a", "aac", "-pix_fmt", "yuv420p", out], timeout=3600)
    words_count = {}
    for w in re.findall(r"[A-Za-z][a-z]{3,}", transcript.lower()):
        words_count[w] = words_count.get(w, 0) + 1
    top = [w for w, _ in sorted(words_count.items(), key=lambda kv: -kv[1])[:6]
           if w not in {"that", "this", "with", "from", "they", "them", "then", "what", "when", "have", "will", "just", "like"}]
    meta = {"title": (job["title"] or "Movie Recap") + " | Full Story Recap",
            "description": "Watch the full story recap. " + script[:220] + "...",
            "hashtags": " ".join(["#movierecap", "#filmrecap", "#storyrecap"] + [f"#{w}" for w in top[:3]])}
    with open(os.path.join(wd, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return {"duration": use_dur, "narration_duration": ndur, "mode": mode, "meta": meta, "output": out}
