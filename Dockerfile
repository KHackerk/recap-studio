FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg espeak-ng fonts-noto-core curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p models/faster-whisper-tiny models/faster-whisper-base && \
    for m in tiny base; do for f in config.json model.bin tokenizer.json vocabulary.txt; do \
    curl -sSL -o models/faster-whisper-$m/$f https://huggingface.co/Systran/faster-whisper-$m/resolve/main/$f; done; done
ENV PYTHONUNBUFFERED=1
CMD gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 600
