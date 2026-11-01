import stat
import sys
import time

import pytest

from aibrain.harness.accounts import AccountError, Accounts

FAKE_CLAUDE = """#!{py}
import json, os, sys
marker = {marker!r}
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": os.path.exists(marker), "email": "me@example.com", "subscriptionType": "max"}}))
elif args[:2] == ["auth", "login"]:
    print("Opening browser to sign in…")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=abc")
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    code = sys.stdin.readline().strip()
    while code != "good-code":
        if code == "give-up":
            print("OAuth error: too many attempts"); sys.exit(1)
        sys.stdout.write("Invalid code. Please make sure the full code was copied."); sys.stdout.flush()
        code = sys.stdin.readline().strip()
    open(marker, "w").close(); print("Login successful."); sys.exit(0)
elif args[:2] == ["auth", "logout"]:
    os.path.exists(marker) and os.remove(marker)
"""

FAKE_CODEX = """#!{py}
import os, sys, time
marker = {marker!r}
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT" if os.path.exists(marker) else "Not logged in")
    sys.exit(0 if os.path.exists(marker) else 1)
elif args[:2] == ["login", "--device-auth"]:
    print("1. Open this link in your browser and sign in to your account")
    print("   \\x1b[94mhttps://auth.openai.com/codex/device\\x1b[0m")
    print("2. Enter this one-time code (expires in 15 minutes)")
    print("   \\x1b[94mABCD-EFGH1\\x1b[0m", flush=True)
    time.sleep(1.5)
    open(marker, "w").close(); print("Successfully logged in"); sys.exit(0)
elif args[:1] == ["logout"]:
    os.path.exists(marker) and os.remove(marker)
"""


def fake(tmp_path, name, template):
    path = tmp_path / name
    path.write_text(template.format(py=sys.executable, marker=str(tmp_path / f"{name}.signed-in")))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def accounts(tmp_path):
    return Accounts({"claude": fake(tmp_path, "claude", FAKE_CLAUDE), "codex": fake(tmp_path, "codex", FAKE_CODEX)})


def wait_for(login, statuses, timeout=10):
    deadline = time.time() + timeout
    while login.status not in statuses and time.time() < deadline:
        time.sleep(0.05)
    assert login.status in statuses, (login.status, login.output)


def test_status(accounts):
    a = {x["kind"]: x for x in accounts.all()}
    assert a["claude"]["installed"] and not a["claude"]["signed_in"]
    assert a["codex"]["installed"] and not a["codex"]["signed_in"]


def test_claude_login_with_code(accounts):
    login = accounts.start_login("claude")
    wait_for(login, {"waiting"})
    assert login.url.startswith("https://claude.com/cai/oauth/authorize") and login.needs_code
    assert accounts.start_login("claude") is login  # one sign-in per account at a time
    accounts.submit_code(login.id, "good-code")
    wait_for(login, {"done"})
    status = accounts.status("claude")
    assert status["signed_in"] and status["detail"] == "me@example.com · max"
    accounts.logout("claude")
    assert not accounts.status("claude")["signed_in"]


def test_claude_wrong_code_then_retry(accounts):
    login = accounts.start_login("claude")
    wait_for(login, {"waiting"})
    accounts.submit_code(login.id, "bad")
    wait_for(login, {"verifying"})
    deadline = time.time() + 10
    while "Invalid code" not in login.message and time.time() < deadline:
        time.sleep(0.05)
    assert login.status == "waiting" and "Invalid code" in login.message  # shown, and you can try again
    accounts.submit_code(login.id, "good-code")
    wait_for(login, {"done"})


def test_claude_gives_up(accounts):
    login = accounts.start_login("claude")
    wait_for(login, {"waiting"})
    accounts.submit_code(login.id, "give-up")
    wait_for(login, {"failed"})
    assert "too many attempts" in login.message
    with pytest.raises(AccountError):
        accounts.submit_code(login.id, "again")


def test_codex_device_login(accounts):
    login = accounts.start_login("codex")
    wait_for(login, {"waiting"})
    assert login.url == "https://auth.openai.com/codex/device" and login.user_code == "ABCD-EFGH1"
    with pytest.raises(AccountError):
        accounts.submit_code(login.id, "x")  # nothing to paste for device sign-in
    wait_for(login, {"done"})
    assert accounts.status("codex")["signed_in"]


def test_cancel(accounts):
    login = accounts.start_login("claude")
    wait_for(login, {"waiting"})
    accounts.cancel(login.id)
    assert login.status == "cancelled"
    time.sleep(0.3)
    assert login.status == "cancelled"  # the follower thread doesn't overwrite it


def test_missing_cli(tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    a = Accounts({})
    assert a.status("claude")["installed"] is False
    with pytest.raises(AccountError, match="not installed"):
        a.start_login("claude")
    with pytest.raises(AccountError):
        a.status("gemini")


def test_accounts_api(vault, tmp_path):
    from fastapi.testclient import TestClient
    from aibrain.harness.server import create_app
    from test_harness import make_runtime
    rt = make_runtime(vault, tmp_path, {"kind": "claude_code", "command": fake(tmp_path, "claude", FAKE_CLAUDE)})
    client = TestClient(create_app(rt))
    claude = next(a for a in client.get("/api/accounts").json() if a["kind"] == "claude")
    assert claude["signed_in"] is False
    provider = client.get("/api/providers").json()[0]
    assert provider["available"] is False and "not signed in" in provider["reason"]
    login = client.post("/api/accounts/claude/login").json()
    for _ in range(100):
        login = client.get(f"/api/accounts/logins/{login['id']}").json()
        if login["status"] == "waiting":
            break
        time.sleep(0.05)
    assert login["needs_code"] and login["url"].startswith("https://claude.com/")
    client.post(f"/api/accounts/logins/{login['id']}/code", json={"code": "good-code"})
    for _ in range(100):
        if client.get(f"/api/accounts/logins/{login['id']}").json()["status"] == "done":
            break
        time.sleep(0.05)
    assert client.get("/api/providers").json()[0]["available"] is True
    assert client.post("/api/accounts/gemini/login").status_code == 400


def test_cancel_stops_helper_processes(tmp_path):
    import os
    helper_pid = tmp_path / "helper.pid"
    script = tmp_path / "codex"
    script.write_text(f"""#!{sys.executable}
import subprocess, sys, time
if sys.argv[1:3] == ["login", "status"]:
    sys.exit(1)
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({str(helper_pid)!r}, "w").write(str(child.pid))
print("https://auth.openai.com/codex/device"); print("WXYZ-12345", flush=True)
child.wait()
""")
    script.chmod(0o755)
    accounts = Accounts({"codex": str(script)})
    login = accounts.start_login("codex")
    wait_for(login, {"waiting"})
    pid = int(helper_pid.read_text())
    accounts.cancel(login.id)
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # the helper is gone too
