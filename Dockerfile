# AI Brain: one image, runs anywhere (x86_64 and arm64).
FROM python:3.12-slim

# Set WITH_CLIS=false for a smaller image without the subscription CLIs (Claude Code, Codex).
ARG WITH_CLIS=true

RUN apt-get update \
 && apt-get install -y --no-install-recommends git git-lfs ca-certificates tini \
 && if [ "$WITH_CLIS" = "true" ]; then \
      apt-get install -y --no-install-recommends nodejs npm \
      && npm install -g --omit=dev @anthropic-ai/claude-code @openai/codex \
      && npm cache clean --force; \
    fi \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*' \
 && useradd --create-home --uid 1000 --shell /bin/bash brain

WORKDIR /app
COPY pyproject.toml README.md ./
COPY aibrain ./aibrain
RUN pip install --no-cache-dir ".[providers]"

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV BRAIN_VAULT=/vault \
    BRAIN_CONFIG=/config/config.yaml \
    BRAIN_HOST=0.0.0.0 \
    BRAIN_PORT=8765 \
    HOME=/home/brain

VOLUME ["/vault", "/config", "/home/brain"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"BRAIN_PORT\"]}/health')"

ENTRYPOINT ["tini", "--", "entrypoint.sh"]
CMD ["brain", "serve-http"]
