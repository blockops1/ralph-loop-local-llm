FROM python:3.11-slim

WORKDIR /app

# Essential tools for a general-purpose coding agent
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    findutils \
    coreutils \
    grep \
    sed \
    gawk \
    bc \
    unzip \
    wget \
    vim-tiny \
    ca-certificates \
    gnupg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Node.js 20 LTS for Biome/JS/TS linting and formatting
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @biomejs/biome \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python3", "pipeline_runner.py"]
