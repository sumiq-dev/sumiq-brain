#!/bin/sh
# Prepares the container on start, then runs the given command as the unprivileged "brain" user.
set -e

if [ "$(id -u)" = "0" ]; then
  # Match the host user that owns the mounted folders (PUID/PGID), so files stay editable on the host.
  PUID="${PUID:-1000}"
  PGID="${PGID:-1000}"
  [ "$(id -g brain)" = "$PGID" ] || groupmod -o -g "$PGID" brain
  [ "$(id -u brain)" = "$PUID" ] || usermod -o -u "$PUID" brain
  mkdir -p /vault /config /home/brain
  [ "$(stat -c %u /vault)" = "$PUID" ] || chown "$PUID:$PGID" /vault
  # Small folders: repair fully, e.g. after a `docker compose exec` that ran as root.
  chown -R "$PUID:$PGID" /config /home/brain
  exec setpriv --reuid="$PUID" --regid="$PGID" --init-groups "$0" "$@"
fi

if [ ! -f "$BRAIN_CONFIG" ]; then
  brain config-template > "$BRAIN_CONFIG"
  echo "created default settings at $BRAIN_CONFIG" >&2
fi

if [ ! -f "$BRAIN_VAULT/brain.yaml" ]; then
  if [ -z "$(ls -A "$BRAIN_VAULT" 2>/dev/null)" ]; then
    brain init "$BRAIN_VAULT" --name "${BRAIN_NAME:-brain}"
  else
    echo "error: $BRAIN_VAULT is not empty but is not a brain vault" >&2
    exit 1
  fi
fi

exec "$@"
