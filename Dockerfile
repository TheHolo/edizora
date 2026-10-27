FROM python:3.12-slim

LABEL org.opencontainers.image.title="Edizora" \
      org.opencontainers.image.version="0.1" \
      org.opencontainers.image.description="Artwork and news publishing for Telegram"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN useradd --create-home --uid 10001 bot
COPY pyproject.toml README.md bot-settings.toml ./
COPY app ./app
RUN pip install --no-cache-dir .
RUN mkdir -p /app/data /app/storage && chown -R bot:bot /app
USER bot
CMD ["python", "-m", "app.main"]
