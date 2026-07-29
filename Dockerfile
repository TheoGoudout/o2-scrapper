# Single-shot container: runs one sync and exits. Schedule it (Coolify scheduled
# task, cron, n8n) rather than running it as a long-lived service.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY o2sync ./o2sync

# Nothing is written to disk at runtime: credentials come from the environment
# (O2_EMAIL, O2_PASSWORD, O2_GOOGLE_CLIENT_ID, O2_GOOGLE_CLIENT_SECRET,
# O2_GOOGLE_REFRESH_TOKEN), so this can run read-only and unprivileged.
RUN useradd --create-home --uid 10001 o2 && chown -R o2:o2 /app
USER o2

ENTRYPOINT ["python", "-m", "o2sync"]
CMD ["sync"]
