FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tiktokbot ./tiktokbot
RUN pip install --no-cache-dir ".[ai,vision,subtitles]"
ENV HF_HOME=/app/data/.cache/huggingface
VOLUME /app/data
EXPOSE 8080
CMD ["tiktokbot", "run", "--web"]
