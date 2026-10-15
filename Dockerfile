FROM python:3.11-slim

# --- 1. Environment variables ---
ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# --- 2. Install Chromium system dependencies ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget gnupg curl \
    libnss3 libatk-bridge2.0-0 libxkbcommon0 \
    libgtk-3-0 libasound2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libcups2 \
    libatspi2.0-0 libpango-1.0-0 libcairo2 \
    fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

# --- 3. Install Python dependencies ---
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# --- 4. Install Playwright Chromium ---
RUN python -m playwright install --with-deps chromium

# --- 5. Copy bot code ---
COPY . .

# --- 6. Start the bot ---
CMD ["python", "bot.py"]