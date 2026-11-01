#!/usr/bin/env bash
# Start (or restart) AI Brain with BRAIN_TOKEN taken from KeePassXC. The token goes straight from
# the database into Docker; it is never written to .env or anywhere else on disk.
#
#   ./docker/up.sh            start/recreate the stack
#   ./docker/up.sh --check    only check that the token can be read (prints its length)
#
# Settings (in .env or the environment):
#   KEEPASS_DB       path to your .kdbx database (required)
#   KEEPASS_ENTRY    entry title or path in the database (default: "AI Brain")
#   KEEPASS_KEYFILE  key file, if your database uses one (optional)
set -euo pipefail
cd "$(dirname "$0")/.."

# Read KEEPASS_* from .env without sourcing it (it may contain other values we shouldn't execute).
if [[ -f .env ]]; then
  while IFS='=' read -r key value; do
    case "$key" in
      KEEPASS_DB|KEEPASS_ENTRY|KEEPASS_KEYFILE) [[ -z "${!key:-}" ]] && export "$key=$value" ;;
    esac
  done < <(grep -E '^KEEPASS_(DB|ENTRY|KEYFILE)=' .env || true)
fi

: "${KEEPASS_DB:?set KEEPASS_DB (in .env or the environment) to your KeePassXC database}"
KEEPASS_ENTRY="${KEEPASS_ENTRY:-AI Brain}"
KEEPASS_DB="${KEEPASS_DB/#\~/$HOME}"

if ! command -v keepassxc-cli >/dev/null; then
  echo "error: keepassxc-cli not found (it comes with KeePassXC)" >&2
  exit 1
fi
if grep -qE '^BRAIN_TOKEN=.+' .env 2>/dev/null; then
  echo "note: .env still contains BRAIN_TOKEN; the KeePassXC token takes precedence. Remove it from .env." >&2
fi

args=(show --quiet --show-protected --attributes Password)
if [[ -n "${KEEPASS_KEYFILE:-}" ]]; then
  args+=(--key-file "${KEEPASS_KEYFILE/#\~/$HOME}")
fi

echo "Unlocking $KEEPASS_DB (entry \"$KEEPASS_ENTRY\")" >&2
printf 'Master password: ' >&2
if ! BRAIN_TOKEN="$(keepassxc-cli "${args[@]}" "$KEEPASS_DB" "$KEEPASS_ENTRY")"; then
  echo >&2
  echo "error: could not read the token (wrong master password or key file, or no entry \"$KEEPASS_ENTRY\")" >&2
  exit 1
fi
echo >&2
if [[ -z "$BRAIN_TOKEN" ]]; then
  echo "error: the entry \"$KEEPASS_ENTRY\" has an empty password" >&2
  exit 1
fi
if (( ${#BRAIN_TOKEN} < 24 )); then
  echo "warning: the token is only ${#BRAIN_TOKEN} characters; use a long random one (KeePassXC can generate it)" >&2
fi

if [[ "${1:-}" == "--check" ]]; then
  echo "ok: token read from KeePassXC (${#BRAIN_TOKEN} characters); nothing started" >&2
  exit 0
fi

export BRAIN_TOKEN
exec docker compose up -d "$@"
