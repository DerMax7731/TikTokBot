FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tiktokbot ./tiktokbot
RUN pip install --no-cache-dir ".[ai]"
# Mount your config.yaml and a data volume:
#   docker run -d --restart unless-stopped -v $PWD/config.yaml:/app/config.yaml -v tiktokbot-data:/app/data \
#     -e ANTHROPIC_API_KEY=... tiktokbot
VOLUME /app/data
CMD ["tiktokbot", "run"]
