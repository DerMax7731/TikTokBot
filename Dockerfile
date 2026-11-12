FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY tiktokbot ./tiktokbot
RUN pip install --no-cache-dir ".[ai,vision]"
# Mount your config.yaml and a data volume:
#   docker run -d --restart unless-stopped -v $PWD/config.yaml:/app/config.yaml -v tiktokbot-data:/app/data \
#     -e ANTHROPIC_API_KEY=... -p 8080:8080 tiktokbot
# For the dashboard inside Docker set web.host: "0.0.0.0" and a web.token in config.yaml.
VOLUME /app/data
EXPOSE 8080
CMD ["tiktokbot", "run", "--web"]
