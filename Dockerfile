FROM python:3.11-slim

WORKDIR /app

# Install system dependencies for Chromium
RUN apt-get update && apt-get install -y \
    libglib2.0-0 libnss3 libnspr4 libatk-bridge2.0-0 libdrm2 \
    libxkbcommon0 libgbm1 libasound2 libatspi2.0-0 \
    libxcomposite1 libxdamage1 libxrandr2 libpango-1.0-0 \
    libcairo2 libcups2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Scrapling's patchright (Playwright fork)
RUN python -m patchright install chromium

COPY . .

CMD python -m uvicorn main:app --host 0.0.0.0 --port $PORT
