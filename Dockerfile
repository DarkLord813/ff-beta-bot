FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget gnupg curl \
    libnss3 libatk-bridge2.0-0 libxkbcommon0 \
    libgtk-3-0 libasound2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libcups2 \
    libatspi2.0-0 libpango-1.0-0 libcairo2 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Install BOTH the headless shell and full Chromium
RUN python -m playwright install --with-deps chromium

COPY . .

CMD ["python", "bot.py"]