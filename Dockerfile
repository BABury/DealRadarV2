FROM python:3.12-slim-bookworm

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Echte Chromium + virtueel scherm (Xvfb). Funda blokkeert headless browsers,
# dus draaien we 'headed' op een virtueel scherm — zonder dat iemand iets ziet.
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb xauth \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Hele app op het virtuele scherm, zodat de scraper een 'echte' browser kan starten.
CMD xvfb-run -a --server-args="-screen 0 1440x900x24" \
    uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
