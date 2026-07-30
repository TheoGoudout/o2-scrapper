FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # How often to re-sync. Override it in Coolify (or `docker run -e`).
    O2_SYNC_INTERVAL=6h \
    O2_HEARTBEAT_FILE=/tmp/o2sync-heartbeat.json

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY o2sync ./o2sync

# Nothing is written to disk at runtime except the heartbeat: credentials come
# from the environment (O2_EMAIL, O2_PASSWORD, O2_GOOGLE_CLIENT_ID,
# O2_GOOGLE_CLIENT_SECRET, O2_GOOGLE_REFRESH_TOKEN).
RUN useradd --create-home --uid 10001 o2 && chown -R o2:o2 /app
USER o2

# The container stays alive and syncs on a schedule. It deliberately does NOT run
# once and exit: Coolify forces `restart: unless-stopped` on applications, so an
# exiting container is treated as a crash and restarted in a loop.
ENTRYPOINT ["python", "-m", "o2sync"]
CMD ["sync"]

# Liveness only — reports whether the sync loop is still ticking. A failing O2 or
# Google API is logged but does not mark the container unhealthy, because a
# restart could not fix it anyway.
HEALTHCHECK --interval=5m --timeout=15s --start-period=2m --retries=3 \
    CMD ["python", "-m", "o2sync", "healthcheck"]
