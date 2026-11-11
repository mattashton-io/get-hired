# Use official Python 3.12 slim image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies for Playwright headless Chromium and compiling documents
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium headless browser binary
RUN playwright install chromium

# Copy application source code, skills, and canonical persona
COPY src/ ./src/
COPY skills/ ./skills/
COPY persona.md ./persona.md
COPY pyproject.toml ./pyproject.toml

# Copy immutable seed JSON files into /app/seed_data (preserved even when /app/data is mounted via GCS FUSE)
RUN mkdir -p /app/seed_data /app/data/compiled_resumes /app/data/screenshots /app/data/outbox /app/data/session_states /app/config
COPY data/master_resume.json /app/seed_data/master_resume.json
COPY data/target_companies.json /app/seed_data/target_companies.json
COPY data/master_resume.json /app/data/master_resume.json
COPY data/target_companies.json /app/data/target_companies.json

# Set default entrypoint
ENTRYPOINT ["python", "-m", "src.cli"]
CMD ["verify-schedules"]
