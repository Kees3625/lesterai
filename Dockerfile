# Lester — Capital Vacations IT Assistant (app container)
FROM python:3.12-slim

WORKDIR /srv

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app ./app
COPY sops ./sops
COPY reload_sops.py ./

# Knowledge base lives on a mounted volume so it survives rebuilds/moves
ENV DATABASE_PATH=/data/knowledge.db

EXPOSE 8000

# Caddy terminates HTTPS and proxies to this; trust forwarded headers from it.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
