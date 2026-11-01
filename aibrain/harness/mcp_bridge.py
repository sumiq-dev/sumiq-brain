"""Bridge a stdio MCP client (e.g. Codex) to a conversation's HTTP MCP endpoint, so it uses the
same toolbox, and the same approvals, as every other model.

    python -m aibrain.harness.mcp_bridge <url>      # token in $BRAIN_MCP_TOKEN
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    url, token = sys.argv[1], os.environ.get("BRAIN_MCP_TOKEN", "")
    for line in sys.stdin:
        if not line.strip():
            continue
        msg_id = None
        try:
            msg_id = json.loads(line).get("id")
            req = urllib.request.Request(url, data=line.encode(), method="POST", headers={
                "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=1200) as resp:  # approvals can take a while
                body = resp.read()
        except (urllib.error.URLError, OSError, ValueError) as e:
            if msg_id is None:
                continue
            body = json.dumps({"jsonrpc": "2.0", "id": msg_id,
                               "error": {"code": -32000, "message": f"tool bridge: {e}"}}).encode()
        if body:  # notifications get an empty 202
            sys.stdout.write(body.decode() + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
