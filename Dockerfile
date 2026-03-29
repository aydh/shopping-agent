FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (including Playwright/Chromium OS deps)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    # Chromium runtime dependencies
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ src/

# Install app (includes playwright Python package)
RUN pip install --no-cache-dir -e .

# Download Playwright's Chromium browser (must run before switching to non-root)
RUN playwright install chromium

# Create runtime directories
RUN mkdir -p /app/data /app/logs

# Run as non-root user
RUN useradd -m -u 1000 app && chown -R app:app /app

# Make the Playwright browser cache accessible to the app user
RUN cp -r /root/.cache/ms-playwright /home/app/.cache/ && \
    chown -R app:app /home/app/.cache/ms-playwright

USER app

EXPOSE 8000

CMD ["uvicorn", "shopping_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
