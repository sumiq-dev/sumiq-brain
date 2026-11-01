"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Item } from "@/lib/api";

// [[note-name]] and [[path|Alias]] become chips; the brain browser will make them clickable.
function withNoteLinks(text: string): string {
  return text.replace(/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]/g, (_m, target: string, alias?: string) => {
    const label = (alias || target).trim().replace(/[[\]]/g, "");
    return `[${label}](#note:${encodeURIComponent(target.trim())})`;
  });
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a({ href, children }) {
            if (href?.startsWith("#note:")) {
              return <span className="note-chip" title={decodeURIComponent(href.slice(6))}>{children}</span>;
            }
            return (
              <a href={href} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {withNoteLinks(text)}
      </ReactMarkdown>
    </div>
  );
}

const TOOL_LABELS: Record<string, string> = {
  read_index: "Read index",
  read_note: "Read note",
  read_source: "Read source",
  follow_links: "Follow links",
  search: "Search brain",
  write_note: "Write note",
  edit_note: "Edit note",
  create_folder: "Create folder",
  link: "Link notes",
  move_note: "Move note",
  mark_outdated: "Mark outdated",
  conventions: "Read conventions",
  fs_list: "List files",
  fs_read: "Read file",
  fs_search: "Search files",
  fs_write: "Write file",
  fs_edit: "Edit file",
  run_command: "Run command",
  web_fetch: "Fetch page",
  web_search: "Search web",
  WebSearch: "Search web",
  WebFetch: "Fetch page",
};

function argSummary(args: Record<string, unknown> = {}): string {
  for (const key of ["command", "path", "name", "query", "url", "pattern", "file", "source"]) {
    if (typeof args[key] === "string" && args[key]) return String(args[key]);
  }
  const first = Object.values(args).find((v) => typeof v === "string");
  return typeof first === "string" ? first : "";
}

export function ToolCard({ call, result, running }: { call: Item; result?: Item; running: boolean }) {
  const [open, setOpen] = useState(false);
  const label = TOOL_LABELS[call.name || ""] || call.name;
  const state = result ? (result.is_error ? "error" : "ok") : running ? "running" : "unknown";
  return (
    <div className={`tool-card ${state}`}>
      <button className="tool-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="tool-dot" aria-hidden />
        <span className="tool-name">{label}</span>
        <span className="tool-arg">{argSummary(call.args)}</span>
        <span className="tool-toggle">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="tool-body">
          {call.args && Object.keys(call.args).length > 0 && (
            <pre className="tool-pre">{JSON.stringify(call.args, null, 2)}</pre>
          )}
          {result?.output && <pre className="tool-pre output">{result.output}</pre>}
        </div>
      )}
    </div>
  );
}

export function ApprovalCard({ item, onDecide }: { item: Item; onDecide: (approved: boolean) => void }) {
  const [busy, setBusy] = useState(false);
  const decide = (approved: boolean) => {
    setBusy(true);
    onDecide(approved);
  };
  return (
    <div className={`approval ${item.resolved ? (item.approved ? "approved" : "denied") : "pending"}`}>
      <div className="approval-title">
        {item.resolved ? (item.approved ? "Approved" : "Denied") : "Approval needed"} ·{" "}
        {TOOL_LABELS[item.tool || ""] || item.tool}
      </div>
      <pre className="approval-reason">{item.reason}</pre>
      {!item.resolved && (
        <div className="approval-actions">
          <button className="btn primary" disabled={busy} onClick={() => decide(true)}>
            Approve
          </button>
          <button className="btn" disabled={busy} onClick={() => decide(false)}>
            Deny
          </button>
        </div>
      )}
    </div>
  );
}

function tokens(n = 0): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function UsageLine({ item }: { item: Item }) {
  const input = (item.input_tokens || 0) + (item.cache_read_tokens || 0) + (item.cache_write_tokens || 0);
  return (
    <div className="usage">
      {item.model || "model"} · {tokens(input)} in / {tokens(item.output_tokens)} out
      {item.cost_usd !== undefined && ` · $${item.cost_usd.toFixed(3)} list price`}
      {item.status && item.status !== "done" && ` · ${item.status}`}
    </div>
  );
}

export function Conversation({
  items,
  running,
  onDecide,
}: {
  items: Item[];
  running: boolean;
  onDecide: (approvalId: string, approved: boolean) => void;
}) {
  const results = new Map(items.filter((i) => i.kind === "tool_result" && i.id).map((i) => [i.id, i]));
  return (
    <>
      {items.map((item, n) => {
        switch (item.kind) {
          case "user":
            return (
              <div key={n} className="msg user">
                <div className="bubble">{item.text}</div>
              </div>
            );
          case "assistant":
            return (
              <div key={n} className="msg assistant">
                <Markdown text={item.text || ""} />
              </div>
            );
          case "tool_call":
            return <ToolCard key={n} call={item} result={results.get(item.id)} running={running} />;
          case "approval":
            return <ApprovalCard key={n} item={item} onDecide={(ok) => onDecide(item.approval_id!, ok)} />;
          case "error":
            return (
              <div key={n} className="error-box">
                {item.message}
              </div>
            );
          case "usage":
            return <UsageLine key={n} item={item} />;
          case "notice":
            return (
              <div key={n} className="notice">
                {item.text}
              </div>
            );
          default:
            return null;
        }
      })}
    </>
  );
}
