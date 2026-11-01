"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Account, api, Login } from "@/lib/api";

function LoginFlow({ login, onUpdate }: { login: Login; onUpdate: (l: Login) => void }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async () => {
    setError(null);
    try {
      onUpdate(await api.loginCode(login.id, code));
      setCode("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  if (login.status === "starting") return <p className="muted">Starting sign-in…</p>;
  if (login.status === "verifying") return <p className="muted">Checking the code…</p>;
  if (login.status === "failed" || login.status === "cancelled") {
    return <p className={login.status === "failed" ? "err" : "muted"}>{login.message}</p>;
  }
  if (login.status === "done") return <p className="ok">{login.message}</p>;

  return login.kind === "codex" ? (
    <ol className="steps">
      <li>
        Open OpenAI&apos;s sign-in page:{" "}
        <a href={login.url ?? "#"} target="_blank" rel="noopener noreferrer">
          {login.url}
        </a>
      </li>
      <li>
        Enter this code there: <code className="user-code">{login.user_code}</code>{" "}
        <button className="btn small" onClick={() => navigator.clipboard?.writeText(login.user_code ?? "")}>
          Copy
        </button>
      </li>
      <li className="muted">This window notices by itself when you&apos;re done.</li>
    </ol>
  ) : (
    <ol className="steps">
      <li>
        <a className="btn primary small" href={login.url ?? "#"} target="_blank" rel="noopener noreferrer">
          Open Anthropic&apos;s sign-in page
        </a>
      </li>
      <li>
        After signing in, Anthropic shows a code. Paste it here:
        <div className="code-row">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && code.trim() && submit()}
            placeholder="Code from Anthropic"
            autoComplete="off"
            spellCheck={false}
          />
          <button className="btn primary" disabled={!code.trim()} onClick={submit}>
            Sign in
          </button>
        </div>
        {(error || login.message) && <p className="err">{error || login.message}</p>}
      </li>
    </ol>
  );
}

export function AccountsDialog({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [accounts, setAccounts] = useState<Account[] | null>(null);
  const [logins, setLogins] = useState<Record<string, Login>>({});
  const [error, setError] = useState<string | null>(null);
  const polling = useRef<Record<string, boolean>>({});

  const refresh = useCallback(() => {
    api.accounts().then(setAccounts).catch((e) => setError(String(e.message ?? e)));
  }, []);
  useEffect(refresh, [refresh]);

  // Follow a running sign-in until it ends.
  const follow = useCallback(
    async (id: string, kind: string) => {
      if (polling.current[id]) return;
      polling.current[id] = true;
      try {
        while (true) {
          await new Promise((r) => setTimeout(r, 1500));
          const l = await api.login(id);
          setLogins((prev) => ({ ...prev, [kind]: l }));
          if (["done", "failed", "cancelled"].includes(l.status)) break;
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        polling.current[id] = false;
        refresh();
        onChanged();
      }
    },
    [refresh, onChanged],
  );

  const start = async (kind: string) => {
    setError(null);
    try {
      const l = await api.startLogin(kind);
      setLogins((prev) => ({ ...prev, [kind]: l }));
      follow(l.id, kind);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const signOut = async (kind: string) => {
    if (!confirm("Sign out? Agents using this subscription will stop working until you sign in again.")) return;
    try {
      await api.logout(kind);
      setLogins((prev) => {
        const next = { ...prev };
        delete next[kind];
        return next;
      });
      refresh();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label="Accounts">
        <div className="modal-head">
          <h2>Accounts</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <p className="muted">
          Sign in with your own subscriptions. Sign-in happens on Anthropic&apos;s and OpenAI&apos;s own pages; this app
          never stores your credentials.
        </p>
        {error && <p className="err">{error}</p>}
        {!accounts && <p className="muted">Checking…</p>}
        {accounts?.map((a) => {
          const login = logins[a.kind];
          const active = login && ["starting", "waiting", "verifying"].includes(login.status);
          return (
            <section key={a.kind} className="account">
              <div className="account-head">
                <span className={`dot ${a.signed_in ? "on" : ""}`} aria-hidden />
                <div className="account-name">
                  {a.label}
                  <small>
                    {!a.installed
                      ? "CLI not installed"
                      : a.signed_in
                        ? `Signed in${a.detail ? ` · ${a.detail}` : ""}`
                        : "Not signed in"}
                  </small>
                </div>
                {a.installed &&
                  (active ? (
                    <button className="btn" onClick={() => api.cancelLogin(login.id).then((l) => setLogins((p) => ({ ...p, [a.kind]: l })))}>
                      Cancel
                    </button>
                  ) : a.signed_in ? (
                    <button className="btn" onClick={() => signOut(a.kind)}>
                      Sign out
                    </button>
                  ) : (
                    <button className="btn primary" onClick={() => start(a.kind)}>
                      Sign in
                    </button>
                  ))}
              </div>
              {login && <LoginFlow login={login} onUpdate={(l) => setLogins((p) => ({ ...p, [a.kind]: l }))} />}
            </section>
          );
        })}
      </div>
    </div>
  );
}
