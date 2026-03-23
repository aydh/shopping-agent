FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ src/

# Install app
RUN pip install --no-cache-dir -e .

# Create runtime directories
RUN mkdir -p /app/data /app/logs

# Run as non-root user
RUN useradd -m -u 1000 app && chown -R app:app /app
USER app

EXPOSE 8000

CMD ["uvicorn", "shopping_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
