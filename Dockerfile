# AIfred : l'agent LiveKit et le serveur web dans une seule image (Bunny Magic Containers).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.10.0 /uv /bin/uv
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
# Modèles téléchargés à la construction, pas au démarrage (VAD Silero…)
RUN python agent.py download-files

# La base SQLite et les photos : monter le disque persistant de Bunny sur /data
ENV AIFRED_DATA_DIR=/data PORT=8080
EXPOSE 8080
CMD ["./start.sh"]
