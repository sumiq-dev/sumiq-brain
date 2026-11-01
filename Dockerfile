# AI Brain: one image, runs anywhere (x86_64 and arm64).

# --- Web UI: built once, served as static files by the Python harness --------------------
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN NEXT_TELEMETRY_DISABLED=1 npm run build

# --- Subscription CLIs (Claude Code, Codex), installed here so npm stays out of the runtime -
FROM node:24-slim AS clis
# Set WITH_CLIS=false for a smaller image without them.
ARG WITH_CLIS=true
RUN mkdir -p /opt/clis/bin \
 && if [ "$WITH_CLIS" = "true" ]; then \
      npm install -g --prefix /opt/clis --omit=dev --no-audit --no-fund @anthropic-ai/claude-code @openai/codex; \
    fi

# --- Runtime ---------------------------------------------------------------------------
FROM python:3.13-slim

RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends git git-lfs ca-certificates tini \
 && rm -rf /var/lib/apt/lists/* \
 && git config --system safe.directory '*' \
 && useradd --create-home --uid 1000 --shell /bin/bash brain

# Node.js 24 runtime only (no npm): the CLIs need it, nothing installs packages at runtime.
COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis /opt/clis /opt/clis

WORKDIR /app
COPY pyproject.toml README.md ./
COPY aibrain ./aibrain
# pip is only needed to install; removing it also removes its vendored libraries from the image.
RUN pip install --no-cache-dir ".[providers,harness]" \
 && pip uninstall -y pip \
 && rm -rf /usr/local/lib/python3.13/ensurepip
COPY --from=web /web/out /app/web

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV BRAIN_VAULT=/vault \
    BRAIN_CONFIG=/config/config.yaml \
    BRAIN_RECIPES=/config/recipes \
    BRAIN_WEB_DIR=/app/web \
    BRAIN_HOST=0.0.0.0 \
    BRAIN_PORT=8765 \
    BRAIN_HARNESS_HOST=0.0.0.0 \
    BRAIN_HARNESS_PORT=8780 \
    HOME=/home/brain \
    PATH=/opt/clis/bin:$PATH

VOLUME ["/vault", "/config", "/home/brain"]
EXPOSE 8765 8780
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"BRAIN_PORT\"]}/health')"

# No USER line on purpose: entrypoint.sh starts as root only to align the user's UID/GID with the
# host (so mounted files stay editable there), then drops privileges for good with setpriv.
ENTRYPOINT ["tini", "--", "entrypoint.sh"]
CMD ["brain", "serve-http"]
