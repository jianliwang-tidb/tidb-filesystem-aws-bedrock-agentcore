FROM --platform=linux/arm64 python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    HOME=/home/agent \
    DRIVE9_CLI_LOG_ENABLED=true

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    fuse3 \
    git \
    jq \
    libcap2-bin \
    procps \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL https://drive9.ai/releases/drive9-linux-arm64 \
    -o /usr/local/bin/drive9 \
    && chmod 0755 /usr/local/bin/drive9

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./

RUN mkdir -p /home/agent/.drive9 /home/agent/.cache/drive9 /mnt/drive9 /mnt/scratch /tmp/drive9-test \
    && chmod -R 0700 /home/agent

# The image deliberately runs as root for the FUSE capability probe.
# AgentCore may still withhold /dev/fuse or mount capabilities; that result is
# reported as PLATFORM_BLOCKED rather than hidden by a non-root user failure.
EXPOSE 8080
CMD ["python", "app.py"]
