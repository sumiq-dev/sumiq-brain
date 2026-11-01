"use client";

import { useEffect, useRef, useState } from "react";
import type { Access, ProviderInfo, Recipe, ThreadOptions } from "@/lib/api";

const EFFORT_LABELS: Record<string, string> = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max",
};

const ACCESS: { id: Access; label: string; hint: string }[] = [
  { id: "read", label: "Read only", hint: "Reads the brain, files and web; changes nothing" },
  { id: "ask", label: "Ask before changes", hint: "Writes and commands wait for your approval" },
  { id: "auto", label: "Full auto", hint: "No approvals: writes files and runs commands on its own" },
];

function ModelPicker({
  providers,
  options,
  onChange,
}: {
  providers: ProviderInfo[];
  options: ThreadOptions;
  onChange: (update: Partial<ThreadOptions>) => void;
}) {
  const value = `${options.provider}::${options.model ?? ""}`;
  return (
    <select
      className="control"
      aria-label="Model"
      value={value}
      onChange={(e) => {
        const [provider, ...rest] = e.target.value.split("::");
        const model = rest.join("::") || null;
        onChange(provider === options.provider ? { model } : { provider, model });
      }}
    >
      {providers
        .filter((p) => p.chat)
        .map((p) => (
          <optgroup key={p.name} label={p.available ? p.label : `${p.label} (${p.reason})`}>
            <option value={`${p.name}::`} disabled={!p.available}>
              {p.name} · default model
            </option>
            {p.models.map((m) => (
              <option key={m.id} value={`${p.name}::${m.id}`} disabled={!p.available}>
                {m.label}
              </option>
            ))}
          </optgroup>
        ))}
      {!providers.some((p) => p.name === options.provider) && (
        <option value={value}>{options.provider}</option>
      )}
    </select>
  );
}

function AccessMenu({
  options,
  recipe,
  onChange,
}: {
  options: ThreadOptions;
  recipe?: Recipe;
  onChange: (update: Partial<ThreadOptions>) => void;
}) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);
  const mode = ACCESS.find((a) => a.id === options.access) ?? ACCESS[1];
  const noFolders = !recipe?.workspaces.length;
  const tools = [
    { key: "web" as const, label: "Web", disabled: false, hint: "Search and read web pages" },
    {
      key: "files" as const,
      label: "Files",
      disabled: noFolders,
      hint: noFolders ? "This recipe has no project folders" : recipe!.workspaces.join(", "),
    },
    {
      key: "shell" as const,
      label: "Shell",
      disabled: noFolders,
      hint: noFolders ? "This recipe has no project folders" : "Run commands in the project folders",
    },
  ];
  const active = ["brain", ...tools.filter((t) => options[t.key]).map((t) => t.label.toLowerCase())];
  return (
    <div className="access" ref={box}>
      <button
        className={`control access-button ${options.access}`}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        title={`${mode.label}: ${active.join(", ")}`}
      >
        {mode.label} · {active.join(", ")}
      </button>
      {open && (
        <div className="access-panel" role="dialog" aria-label="Access">
          <div className="panel-title">Access</div>
          {ACCESS.map((a) => (
            <label key={a.id} className="panel-row">
              <input type="radio" name="access" checked={options.access === a.id} onChange={() => onChange({ access: a.id })} />
              <span>
                {a.label}
                <small>{a.hint}</small>
              </span>
            </label>
          ))}
          <div className="panel-title">Tools</div>
          <label className="panel-row disabled">
            <input type="checkbox" checked disabled />
            <span>
              Brain<small>Always on: every agent has the full brain</small>
            </span>
          </label>
          {tools.map((t) => (
            <label key={t.key} className={`panel-row ${t.disabled ? "disabled" : ""}`}>
              <input
                type="checkbox"
                checked={options[t.key]}
                disabled={t.disabled}
                onChange={(e) => onChange({ [t.key]: e.target.checked })}
              />
              <span>
                {t.label}
                <small>{t.hint}</small>
              </span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

export function ChatControls({
  providers,
  options,
  recipe,
  onChange,
}: {
  providers: ProviderInfo[];
  options: ThreadOptions | null;
  recipe?: Recipe;
  onChange: (update: Partial<ThreadOptions>) => void;
}) {
  if (!options) return null;
  const provider = providers.find((p) => p.name === options.provider);
  const model = provider?.models.find((m) => m.id === options.model);
  const efforts = model?.efforts ?? provider?.efforts ?? [];
  return (
    <div className="controls">
      <ModelPicker providers={providers} options={options} onChange={onChange} />
      {efforts.length > 0 && (
        <select
          className="control"
          aria-label="Thinking"
          value={options.effort ?? ""}
          onChange={(e) => onChange({ effort: e.target.value || null })}
        >
          <option value="">Thinking: default</option>
          {efforts.map((e) => (
            <option key={e} value={e}>
              Thinking: {EFFORT_LABELS[e] ?? e}
            </option>
          ))}
        </select>
      )}
      <AccessMenu options={options} recipe={recipe} onChange={onChange} />
    </div>
  );
}
