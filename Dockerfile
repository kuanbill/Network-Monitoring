FROM python:3.14-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends iputils-ping ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 netword \
    && useradd --uid 10001 --gid netword --no-create-home netword

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY server.py ./
COPY static/ ./static/
RUN mkdir /app/data && chown netword:netword /app/data
USER netword
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/state', timeout=3).read()"
CMD ["python", "server.py", "--bind", "0.0.0.0", "--port", "8080", "--data-dir", "/app/data"]
