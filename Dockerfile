# syntax=docker/dockerfile:1
#
# Bedrock Chat image. Contains no AWS credentials: at runtime boto3 uses its
# default credential chain, which finds a mounted ~/.aws folder locally and
# the instance's IAM role on EC2.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivileged user. UID 1000 matches the first user on most Linux hosts, so a
# read-only bind mount of ~/.aws stays readable during local development.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown app:app /data

COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code is owned by root, so the app user can't modify it.
COPY app.py database.py ./
COPY templates ./templates
COPY static ./static

# SQLite database lives on a volume so conversations survive container restarts.
ENV DATABASE_PATH=/data/chat.db
VOLUME ["/data"]

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# One worker process with many threads: the app tracks in-progress replies (so
# Stop works) in memory, so every request must reach the same process. Threads
# handle concurrent streams, since Bedrock calls spend their time waiting on I/O.
CMD ["gunicorn", "app:app", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "1", \
     "--worker-class", "gthread", \
     "--threads", "16", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-"]
