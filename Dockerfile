FROM python:3.11-slim

# --- 1. Create non-root user (required by Hugging Face Spaces) ---
RUN useradd -m -u 1000 user

# --- 2. Set environment variables ---
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/home/user/.cache/ms-playwright \
    PYTHONUNBUFFERED=1

WORKDIR /home/user/app

# --- 3. Install Chromium system dependencies (as root) ---
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    wget gnupg curl \
    libnss3 libatk-bridge2.0-0 libxkbcommon0 \
    libgtk-3-0 libasound2 libxcomposite1 \
    libxdamage1 libxrandr2 libgbm1 libcups2 \
    libatspi2.0-0 libpango-1.0-0 libcairo2 \
    fonts-liberation && \
    rm -rf /var/lib/apt/lists/*

# --- 4. Install Python dependencies ---
COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# --- 5. Install Playwright Chromium (with system deps) ---
RUN python -m playwright install --with-deps chromium

# --- 6. Switch to non-root user ---
USER user

# --- 7. Copy bot code ---
COPY --chown=user . .

# --- 8. Expose Hugging Face's expected port ---
EXPOSE 7860

# --- 9. Start the bot ---
CMD ["python", "bot.py"]