"""Sign in to the subscription CLIs (Claude Code, Codex) from the web UI.

The CLIs run their own, unmodified sign-in flows; this module only starts them and shows the user
what they print. Codex uses device sign-in (open OpenAI's page, enter a code there); nothing passes
through this app. Claude shows Anthropic's sign-in page, and the code Anthropic displays afterwards
is handed straight to the waiting CLI's input. It is never stored or logged. The CLIs keep their
credentials in their own config folders, as they always do.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
URL_RE = re.compile(r"https://\S+")
DEVICE_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")
LOGIN_TIMEOUT = 15 * 60
STATUS_TTL = 20.0

ACCOUNTS = {
    "claude": {"label": "Claude subscription", "binary": "claude", "provider_kind": "claude_code"},
    "codex": {"label": "ChatGPT subscription", "binary": "codex", "provider_kind": "codex"},
}


class AccountError(ValueError):
    pass


def _stop(proc: subprocess.Popen) -> None:
    """Stop a sign-in and any helper processes it started (the codex launcher runs a native binary)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)


@dataclass
class Login:
    id: str
    kind: str
    status: str = "starting"      # starting | waiting | verifying | done | failed | cancelled
    url: str | None = None        # the vendor's sign-in page
    user_code: str | None = None  # Codex: code to enter on OpenAI's page
    needs_code: bool = False      # Claude: paste Anthropic's code back here
    message: str = ""
    started: float = field(default_factory=time.monotonic)
    proc: subprocess.Popen | None = None
    output: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {"id": self.id, "kind": self.kind, "status": self.status, "url": self.url,
                "user_code": self.user_code, "needs_code": self.needs_code, "message": self.message}


class Accounts:
    def __init__(self, commands: dict[str, str] | None = None):
        self.commands = commands or {}  # kind -> CLI path override (from provider settings)
        self.logins: dict[str, Login] = {}
        self._status: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def cli(self, kind: str) -> str | None:
        if kind not in ACCOUNTS:
            raise AccountError(f"unknown account '{kind}'")
        return self.commands.get(kind) or shutil.which(ACCOUNTS[kind]["binary"])

    # ----------------------------------------------------------------- status

    def status(self, kind: str, fresh: bool = False) -> dict:
        """Signed in or not (cached briefly: it runs the CLI)."""
        cli = self.cli(kind)  # also rejects unknown account names
        with self._lock:
            hit = self._status.get(kind)
        if hit and not fresh and time.monotonic() - hit[0] < STATUS_TTL:
            return hit[1]
        info = {"kind": kind, "label": ACCOUNTS[kind]["label"], "installed": False, "signed_in": False,
                "detail": None}
        if cli:
            info["installed"] = True
            try:
                if kind == "claude":
                    p = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=20)
                    data = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
                    info["signed_in"] = bool(data.get("loggedIn"))
                    if info["signed_in"]:
                        parts = [data.get("email"), data.get("orgName"), data.get("subscriptionType")]
                        info["detail"] = " · ".join(str(x) for x in parts if x)
                else:
                    p = subprocess.run([cli, "login", "status"], capture_output=True, text=True, timeout=20)
                    text = ANSI_RE.sub("", p.stdout + p.stderr).strip().splitlines()
                    info["signed_in"] = p.returncode == 0
                    info["detail"] = text[-1] if info["signed_in"] and text else None
            except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
                info["detail"] = f"couldn't check: {e}"
        with self._lock:
            self._status[kind] = (time.monotonic(), info)
        return info

    def all(self) -> list[dict]:
        return [self.status(k) for k in ACCOUNTS]

    # ------------------------------------------------------------------ login

    def start_login(self, kind: str) -> Login:
        cli = self.cli(kind)
        if not cli:
            raise AccountError(f"the {ACCOUNTS[kind]['binary']} CLI is not installed")
        with self._lock:
            for login in self.logins.values():
                if login.kind == kind and login.status in ("starting", "waiting", "verifying"):
                    return login  # one sign-in per account at a time
            login = Login(uuid.uuid4().hex[:12], kind)
            self.logins[login.id] = login
        cmd = [cli, "auth", "login", "--claudeai"] if kind == "claude" else [cli, "login", "--device-auth"]
        login.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                      text=True, env={**os.environ, "BROWSER": "true", "NO_COLOR": "1"},
                                      start_new_session=True)  # own process group: stop helpers too
        threading.Thread(target=self._follow, args=(login,), daemon=True, name=f"login-{kind}").start()
        threading.Thread(target=self._expire, args=(login,), daemon=True).start()
        return login

    def _follow(self, login: Login) -> None:
        """Read what the CLI prints: find the sign-in link (and Codex's code), then wait for it to finish."""
        proc = login.proc
        assert proc and proc.stdout
        fd, buffer = proc.stdout.fileno(), ""

        def flush(text: str) -> None:
            line = ANSI_RE.sub("", text).strip()
            if line:
                login.output.append(line)
                self._parse(login, line)

        while True:
            # Prompts and messages don't always end with a newline: treat a pause as the end of a line.
            ready, _, _ = select.select([fd], [], [], 0.3)
            if not ready:
                if buffer:
                    flush(buffer)
                    buffer = ""
                if proc.poll() is not None:
                    break
                continue
            data = os.read(fd, 4096).decode("utf-8", errors="replace")
            if not data:
                break
            buffer += data
            *lines, buffer = buffer.split("\n")
            for line in lines:
                flush(line)
        if buffer:
            flush(buffer)
        proc.wait()
        if login.status == "cancelled":
            return
        self.status(login.kind, fresh=True)
        signed_in = self._status[login.kind][1]["signed_in"]
        if proc.returncode == 0 and signed_in:
            login.status, login.message = "done", "Signed in."
        else:
            login.status = "failed"
            hint = next((l for l in reversed(login.output) if "error" in l.lower() or "invalid" in l.lower()), "")
            login.message = hint or "Sign-in didn't complete. Start again if needed."

    @staticmethod
    def _parse(login: Login, line: str) -> None:
        if login.status in ("done", "failed", "cancelled"):
            return  # finished: late output must not reopen it
        url = URL_RE.search(line)
        if url and not login.url:
            login.url = url.group(0).rstrip(".,)")
        if login.kind == "codex":
            code = DEVICE_CODE_RE.search(line)
            if code and login.url and not login.user_code:
                login.user_code = code.group(0)
            if login.url and login.user_code:
                login.status = "waiting"
        elif "invalid code" in line.lower():  # the CLI keeps waiting for another try
            login.message = line
            login.status = "waiting"
        elif "paste code" in line.lower() or (login.url and login.status == "starting"):
            login.needs_code = True
            login.status = "waiting"

    def _expire(self, login: Login) -> None:
        while login.proc and login.proc.poll() is None:
            if time.monotonic() - login.started > LOGIN_TIMEOUT:
                login.status, login.message = "failed", "Sign-in timed out."
                _stop(login.proc)
                return
            time.sleep(2)

    def submit_code(self, login_id: str, code: str) -> Login:
        login = self.get(login_id)
        if login.kind != "claude" or not login.needs_code or login.status != "waiting":
            raise AccountError("this sign-in isn't waiting for a code")
        code = code.strip()
        if not code or "\n" in code or len(code) > 512:
            raise AccountError("paste the code exactly as Anthropic shows it")
        assert login.proc and login.proc.stdin
        login.status, login.message = "verifying", ""
        login.proc.stdin.write(code + "\n")  # handed straight to the CLI; never stored
        login.proc.stdin.flush()
        return login

    def cancel(self, login_id: str) -> Login:
        login = self.get(login_id)
        if login.proc and login.proc.poll() is None:
            login.status, login.message = "cancelled", "Sign-in cancelled."
            _stop(login.proc)
        return login

    def get(self, login_id: str) -> Login:
        login = self.logins.get(login_id)
        if login is None:
            raise AccountError("unknown sign-in")
        return login

    def logout(self, kind: str) -> dict:
        cli = self.cli(kind)
        if not cli:
            raise AccountError(f"the {ACCOUNTS[kind]['binary']} CLI is not installed")
        cmd = [cli, "auth", "logout"] if kind == "claude" else [cli, "logout"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return self.status(kind, fresh=True)
