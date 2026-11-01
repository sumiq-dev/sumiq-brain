"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  Item,
  ProviderInfo,
  Recipe,
  RunEvent,
  runEvents,
  setToken,
  Thread,
  ThreadOptions,
  Unauthorized,
} from "@/lib/api";
import { ChatControls } from "./Controls";
import { Conversation } from "./Messages";

function applyEvent(items: Item[], e: RunEvent): Item[] {
  const next = items.slice();
  const last = next[next.length - 1];
  switch (e.type) {
    case "run_started":
      if (e.text) next.push({ kind: "user", text: e.text });
      return next;
    case "text":
      if (last?.kind === "assistant") next[next.length - 1] = { ...last, text: (last.text || "") + (e.delta || "") };
      else next.push({ kind: "assistant", text: e.delta || "" });
      return next;
    case "tool_call":
    case "tool_result":
    case "error":
    case "notice":
      next.push({ ...e, kind: e.type } as Item);
      return next;
    case "approval_request":
      next.push({ kind: "approval", approval_id: e.approval_id, tool: e.tool, reason: e.reason, args: e.args });
      return next;
    case "approval_resolved":
      return next.map((i) =>
        i.kind === "approval" && i.approval_id === e.approval_id ? { ...i, resolved: true, approved: e.approved } : i,
      );
    case "done":
      if (e.usage && (Object.keys(e.usage).length || e.model)) {
        next.push({ kind: "usage", status: e.status, model: e.model, ...e.usage });
      }
      return next;
    default:
      return items;
  }
}

function Login({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="login">
      <form
        onSubmit={(ev) => {
          ev.preventDefault();
          setToken(value.trim());
          onDone();
        }}
      >
        <h1>AI Brain</h1>
        <p>Enter the access token (BRAIN_TOKEN) to open your brain.</p>
        {/* A fixed username lets password managers (e.g. KeePassXC-Browser) match and fill the token. */}
        <input type="text" name="username" autoComplete="username" value="ai-brain" readOnly hidden />
        <input type="password" name="password" autoComplete="current-password" autoFocus value={value} onChange={(e) => setValue(e.target.value)} placeholder="Token" />
        <button className="btn primary" type="submit">
          Open
        </button>
      </form>
    </div>
  );
}

function ToolBadges({ recipe }: { recipe?: Recipe }) {
  if (!recipe) return null;
  const tools = Object.entries(recipe.tools).filter(([, on]) => on).map(([name]) => name);
  return (
    <span className="badges">
      {tools.map((t) => (
        <span key={t} className="badge">
          {t}
        </span>
      ))}
    </span>
  );
}

export default function App() {
  const [authNeeded, setAuthNeeded] = useState(false);
  const [recipes, setRecipes] = useState<Recipe[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [options, setOptions] = useState<ThreadOptions | null>(null);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [current, setCurrent] = useState<string | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [newRecipe, setNewRecipe] = useState("standard");
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const streamAbort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const fail = useCallback((e: unknown) => {
    if (e instanceof Unauthorized) setAuthNeeded(true);
    else setError(e instanceof Error ? e.message : String(e));
  }, []);

  const refreshThreads = useCallback(() => api.threads().then(setThreads).catch(fail), [fail]);

  const load = useCallback(async () => {
    try {
      setRecipes(await api.recipes());
      setAuthNeeded(false);
      api.providers().then(setProviders).catch(fail);
      await refreshThreads();
      const t = new URLSearchParams(window.location.search).get("t");
      if (t) setCurrent(t);
    } catch (e) {
      fail(e);
    }
  }, [fail, refreshThreads]);

  useEffect(() => {
    load();
  }, [load]);

  const follow = useCallback(
    async (rid: string) => {
      streamAbort.current?.abort();
      const ctrl = new AbortController();
      streamAbort.current = ctrl;
      setRunId(rid);
      try {
        for await (const e of runEvents(rid, ctrl.signal)) {
          setItems((prev) => applyEvent(prev, e));
        }
      } catch (e) {
        fail(e);
      } finally {
        if (streamAbort.current === ctrl) {
          setRunId(null);
          refreshThreads();
        }
      }
    },
    [fail, refreshThreads],
  );

  // Load the selected conversation (and attach to it if it is still answering).
  useEffect(() => {
    streamAbort.current?.abort();
    setRunId(null);
    const url = new URL(window.location.href);
    if (current) url.searchParams.set("t", current);
    else url.searchParams.delete("t");
    window.history.replaceState(null, "", url);
    if (!current) {
      setItems([]);
      return;
    }
    api
      .thread(current)
      .then((t) => {
        setItems(t.items);
        setOptions(t.options);
        if (t.active_run) follow(t.active_run);
      })
      .catch(fail);
  }, [current, follow, fail]);

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [items]);

  const send = async () => {
    const text = draft.trim();
    if (!text || runId) return;
    setError(null);
    try {
      let tid = current;
      if (!tid) {
        tid = (await api.newThread(newRecipe, options ?? undefined)).id;
        setItems([]);
        streamAbort.current?.abort();
        setCurrent(tid);
      }
      setDraft("");
      const { run_id } = await api.send(tid, text);
      follow(run_id);
    } catch (e) {
      fail(e);
    }
  };

  const thread = threads.find((t) => t.id === current);
  const recipe = recipes.find((r) => r.id === (thread?.recipe || newRecipe));

  // A new chat starts from its recipe's defaults; an open conversation keeps its own options.
  useEffect(() => {
    if (current) return;
    const r = recipes.find((x) => x.id === newRecipe);
    if (r) setOptions(r.defaults);
  }, [current, newRecipe, recipes]);

  const changeOptions = async (update: Partial<ThreadOptions>) => {
    if (!options) return;
    if (!current) {
      setOptions({ ...options, ...update, ...(update.provider ? { effort: update.effort ?? null } : {}) });
      return;
    }
    try {
      setOptions((await api.setOptions(current, update)).options);
    } catch (e) {
      fail(e);
    }
  };

  if (authNeeded) return <Login onDone={load} />;

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">AI Brain</div>
        <div className="new-chat">
          <select value={newRecipe} onChange={(e) => setNewRecipe(e.target.value)} aria-label="Agent recipe">
            {recipes.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </select>
          <button className="btn primary" onClick={() => setCurrent(null)}>
            New chat
          </button>
        </div>
        <nav className="threads">
          {threads.map((t) => (
            <div key={t.id} className={`thread ${t.id === current ? "active" : ""}`}>
              <button className="thread-title" onClick={() => setCurrent(t.id)} title={t.title}>
                {t.title}
                <span className="thread-meta">
                  {recipes.find((r) => r.id === t.recipe)?.name || t.recipe} · {new Date(t.updated).toLocaleDateString()}
                </span>
              </button>
              <button
                className="thread-delete"
                aria-label="Delete conversation"
                onClick={async () => {
                  if (!confirm(`Delete "${t.title}"?`)) return;
                  try {
                    await api.deleteThread(t.id);
                    if (current === t.id) setCurrent(null);
                    refreshThreads();
                  } catch (e) {
                    fail(e);
                  }
                }}
              >
                ×
              </button>
            </div>
          ))}
        </nav>
      </aside>

      <main className="main">
        <header className="topbar">
          <span className="title">{thread?.title || "New chat"}</span>
          <span className="recipe-name">{recipe?.name}</span>
          <ToolBadges recipe={recipe} />
        </header>

        <section className="conversation">
          {items.length === 0 && !runId && (
            <div className="empty">
              <h2>{recipe?.name || "Standard"}</h2>
              <p>{recipe?.description || "Ask anything; your brain is the agent's memory."}</p>
            </div>
          )}
          <Conversation
            items={items}
            running={!!runId}
            onDecide={(id, ok) => api.decide(id, ok).catch(fail)}
          />
          {runId && <div className="thinking">Working…</div>}
          <div ref={bottom} />
        </section>

        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button onClick={() => setError(null)} aria-label="Dismiss">
              ×
            </button>
          </div>
        )}

        <footer className="composer">
          <div className="composer-box">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={runId ? "Answering…" : "Message (Enter to send, Shift+Enter for a new line)"}
            rows={Math.min(8, Math.max(2, draft.split("\n").length))}
          />
          <ChatControls providers={providers} options={options} recipe={recipe} onChange={changeOptions} />
          </div>
          {runId ? (
            <button className="btn" onClick={() => api.cancel(runId).catch(fail)}>
              Stop
            </button>
          ) : (
            <button className="btn primary" onClick={send} disabled={!draft.trim()}>
              Send
            </button>
          )}
        </footer>
      </main>
    </div>
  );
}
