// Client for the harness HTTP API. The token is kept in localStorage and sent as a bearer
// header; events are read with fetch streaming (EventSource can't send headers).

export type Recipe = {
  id: string;
  name: string;
  description: string;
  provider: string | null;
  model: string | null;
  tools: { brain: boolean; files: boolean; shell: boolean; web: boolean };
  workspaces: string[];
  defaults: ThreadOptions;
};

export type Access = "read" | "ask" | "auto";

export type ThreadOptions = {
  provider: string;
  model: string | null;
  effort: string | null;
  web: boolean;
  files: boolean;
  shell: boolean;
  access: Access;
};

export type ProviderInfo = {
  name: string;
  kind: string;
  label: string;
  chat: boolean;
  available: boolean;
  reason: string | null;
  models: { id: string; label: string; efforts?: string[] }[];
  efforts: string[];
};

export type Thread = {
  id: string;
  title: string;
  recipe: string;
  created: string;
  updated: string;
  saved_to_brain: string | null;
};

export type Item = {
  kind: "user" | "assistant" | "tool_call" | "tool_result" | "error" | "usage" | "approval" | "notice";
  text?: string;
  id?: string;
  name?: string;
  args?: Record<string, unknown>;
  output?: string;
  is_error?: boolean;
  server?: boolean;
  message?: string;
  status?: string;
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  cache_read_tokens?: number;
  cache_write_tokens?: number;
  cost_usd?: number;
  approval_id?: string;
  tool?: string;
  reason?: string;
  resolved?: boolean;
  approved?: boolean;
};

export type RunEvent = Item & { type: string; seq: number; delta?: string; usage?: Partial<Item> };

export type Account = {
  kind: "claude" | "codex";
  label: string;
  installed: boolean;
  signed_in: boolean;
  detail: string | null;
};

export type Login = {
  id: string;
  kind: string;
  status: "starting" | "waiting" | "verifying" | "done" | "failed" | "cancelled";
  url: string | null;
  user_code: string | null;
  needs_code: boolean;
  message: string;
};

export class Unauthorized extends Error {}

const TOKEN_KEY = "ai-brain-token";

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token);
}

function headers(): Record<string, string> {
  const token = typeof window === "undefined" ? "" : localStorage.getItem(TOKEN_KEY) || "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...headers(), ...(init.headers || {}) },
  });
  if (res.status === 401) throw new Unauthorized("unauthorized");
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  recipes: () => request<Recipe[]>("/api/recipes"),
  providers: () => request<ProviderInfo[]>("/api/providers"),
  threads: () => request<Thread[]>("/api/threads"),
  thread: (id: string) =>
    request<Thread & { items: Item[]; active_run: string | null; options: ThreadOptions | null }>(`/api/threads/${id}`),
  newThread: (recipe: string, options?: Partial<ThreadOptions>) =>
    request<Thread & { options: ThreadOptions }>("/api/threads", {
      method: "POST",
      body: JSON.stringify({ recipe, options }),
    }),
  setOptions: (id: string, options: Partial<ThreadOptions>) =>
    request<{ options: ThreadOptions }>(`/api/threads/${id}`, { method: "PATCH", body: JSON.stringify({ options }) }),
  deleteThread: (id: string) => request<{ ok: boolean }>(`/api/threads/${id}`, { method: "DELETE" }),
  send: (id: string, text: string) =>
    request<{ run_id: string }>(`/api/threads/${id}/messages`, { method: "POST", body: JSON.stringify({ text }) }),
  cancel: (runId: string) => request<{ cancelled: boolean }>(`/api/runs/${runId}/cancel`, { method: "POST" }),
  accounts: () => request<Account[]>("/api/accounts"),
  startLogin: (kind: string) => request<Login>(`/api/accounts/${kind}/login`, { method: "POST" }),
  login: (id: string) => request<Login>(`/api/accounts/logins/${id}`),
  loginCode: (id: string, code: string) =>
    request<Login>(`/api/accounts/logins/${id}/code`, { method: "POST", body: JSON.stringify({ code }) }),
  cancelLogin: (id: string) => request<Login>(`/api/accounts/logins/${id}`, { method: "DELETE" }),
  logout: (kind: string) => request<Account>(`/api/accounts/${kind}/logout`, { method: "POST" }),
  decide: (approvalId: string, approved: boolean) =>
    request<{ ok: boolean }>(`/api/approvals/${approvalId}`, { method: "POST", body: JSON.stringify({ approved }) }),
};

/** Follow a run's events from `after`, reconnecting on network hiccups, until it is done. */
export async function* runEvents(runId: string, signal: AbortSignal): AsyncGenerator<RunEvent> {
  let after = 0;
  for (let attempt = 0; attempt < 20 && !signal.aborted; attempt++) {
    let res: Response;
    try {
      res = await fetch(`/api/runs/${runId}/events?after=${after}`, { headers: headers(), signal });
    } catch {
      if (signal.aborted) return;
      await new Promise((r) => setTimeout(r, 1000));
      continue;
    }
    if (res.status === 401) throw new Unauthorized("unauthorized");
    if (!res.ok || !res.body) return;
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let cut: number;
        while ((cut = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, cut);
          buffer = buffer.slice(cut + 2);
          const data = block.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("\n");
          if (!data) continue;
          const event = JSON.parse(data) as RunEvent;
          after = event.seq + 1;
          yield event;
          if (event.type === "done") return;
        }
      }
    } catch {
      if (signal.aborted) return;
    }
    await new Promise((r) => setTimeout(r, 500)); // stream ended early: reconnect from `after`
  }
}
