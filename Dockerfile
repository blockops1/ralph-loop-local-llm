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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python3", "pipeline_runner.py"]
