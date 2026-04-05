FROM python:3.11-slim

WORKDIR /app

# Install git (Ralph tools: git_status, git_commit)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python3", "pipeline_runner.py"]
