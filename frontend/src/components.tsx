import { ReactNode, useEffect, useState } from "react";

const ACTION_LABELS: Record<string, string> = {
  add_label: "Add label",
  remove_label: "Remove label",
  mark_read: "Mark read",
  archive: "Archive",
  trash: "Trash",
};

export const actionLabel = (type: string): string => ACTION_LABELS[type] ?? type;

export interface ColorChoice {
  text: string | null;
  background: string | null;
}

export function SwatchPicker({
  palette,
  selected,
  onPick,
}: {
  palette: { background: string; text: string }[];
  selected: ColorChoice;
  onPick: (s: ColorChoice) => void;
}) {
  return (
    <div className="swatches">
      <button
        type="button"
        className={`swatch none ${selected.background == null ? "selected" : ""}`}
        title="No color"
        onClick={() => onPick({ text: null, background: null })}
      >
        ∅
      </button>
      {palette.map((s) => (
        <button
          type="button"
          key={s.background + s.text}
          className={`swatch ${selected.background === s.background ? "selected" : ""}`}
          style={{ background: s.background, color: s.text }}
          title={s.background}
          onClick={() => onPick({ text: s.text, background: s.background })}
        >
          A
        </button>
      ))}
    </div>
  );
}

export function LabelPill({
  name,
  textColor,
  backgroundColor,
}: {
  name: string;
  textColor?: string | null;
  backgroundColor?: string | null;
}) {
  return (
    <span
      className="label-pill"
      style={{
        background: backgroundColor ?? "rgba(127,127,127,0.18)",
        color: textColor ?? "inherit",
      }}
    >
      {name}
    </span>
  );
}

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "ok" | "warn" | "error" | "neutral" | "dry" | "info";
}) {
  return <span className={`badge ${tone}`}>{children}</span>;
}

// Non-label action → badge tone. Labels are rendered as colored LabelPills;
// every other action gets a distinct, tone-coded badge.
const ACTION_TONE: Record<string, "ok" | "warn" | "error" | "neutral" | "info"> = {
  archive: "info",
  mark_read: "neutral",
  trash: "error",
  remove_label: "warn",
};

// Render an email's actions as badges: add_label → colored LabelPill, all other
// actions → tone-coded Badge. Deduped by type+label so an email actioned by
// several rules doesn't show repeated identical badges.
interface ActionItem {
  action_type: string;
  action_params?: Record<string, unknown> | null;
}

export function ActionBadges({ actions }: { actions: ActionItem[] }) {
  if (actions.length === 0) return <>—</>;
  const seen = new Set<string>();
  const pills: ReactNode[] = [];
  const badges: ReactNode[] = [];
  for (const a of actions) {
    const lname = a.action_params?.label_name as string | undefined;
    const key = `${a.action_type}:${lname ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (a.action_type === "add_label" && lname) {
      pills.push(
        <LabelPill
          key={key}
          name={lname}
          textColor={a.action_params?.text_color as string | null}
          backgroundColor={a.action_params?.background_color as string | null}
        />,
      );
    } else if (a.action_type === "remove_label" && lname) {
      badges.push(<Badge key={key} tone="warn">Remove: {lname}</Badge>);
    } else {
      badges.push(
        <Badge key={key} tone={ACTION_TONE[a.action_type] ?? "neutral"}>
          {actionLabel(a.action_type)}
        </Badge>,
      );
    }
  }
  return <span className="action-badges">{pills}{badges}</span>;
}

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className={`modal ${wide ? "modal-wide" : ""}`}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3>{title}</h3>
          <button className="icon-btn" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel = "Confirm",
  danger,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="confirm-message">{message}</div>
      <div className="modal-actions">
        <button onClick={onCancel}>Cancel</button>
        <button className={danger ? "danger" : "primary"} onClick={onConfirm} autoFocus>
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <p className="error">{error}</p>;
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function conf(x: number | null | undefined): string {
  // classification confidence as a 0–1 number, one decimal (e.g. 0.8)
  return x == null ? "—" : x.toFixed(1);
}

export function pct(x: number | null | undefined): string {
  return x == null ? "—" : `${Math.round(x * 100)}%`;
}

// ── Simple line diff (LCS) for criteria version history ─────────────────────

type DiffLine = { kind: "same" | "add" | "del"; text: string };

export function lineDiff(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split("\n");
  const b = newText.split("\n");
  const m = a.length;
  const n = b.length;
  const lcs: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = m - 1; i >= 0; i--)
    for (let j = n - 1; j >= 0; j--)
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < m && j < n) {
    if (a[i] === b[j]) {
      out.push({ kind: "same", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "del", text: a[i++] });
    } else {
      out.push({ kind: "add", text: b[j++] });
    }
  }
  while (i < m) out.push({ kind: "del", text: a[i++] });
  while (j < n) out.push({ kind: "add", text: b[j++] });
  return out;
}

export function DiffView({ oldText, newText }: { oldText: string; newText: string }) {
  const lines = lineDiff(oldText, newText);
  return (
    <pre className="diff">
      {lines.map((l, idx) => (
        <div key={idx} className={`diff-line ${l.kind}`}>
          {l.kind === "add" ? "+ " : l.kind === "del" ? "− " : "  "}
          {l.text}
        </div>
      ))}
    </pre>
  );
}

// ── Bulk action bar ─────────────────────────────────────────────────────────

export function BulkActionBar({
  count,
  actions,
  onClear,
}: {
  count: number;
  actions: { label: string; mobileLabel?: string; onClick: () => Promise<void>; danger?: boolean }[];
  onClear: () => void;
}) {
  const [busy, setBusy] = useState(false);
  if (count === 0) return null;
  return (
    <div className="bulk-bar">
      <span className="bulk-count">{count} selected</span>
      {actions.map((a) => (
        <button
          key={a.label}
          className={a.danger ? "danger" : ""}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              await a.onClick();
            } finally {
              setBusy(false);
            }
          }}
        >
          <span className="label-full">{a.label}</span>
          <span className="label-mobile">{a.mobileLabel ?? a.label}</span>
        </button>
      ))}
      <button className="icon-btn" disabled={busy} onClick={onClear}>
        <span className="label-full">✕ Clear</span>
        <span className="label-mobile">✕</span>
      </button>
    </div>
  );
}

// ── Toggle (pill switch) ─────────────────────────────────────────────────────

export function Toggle({
  checked,
  onChange,
  disabled,
  title,
  className,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  title?: string;
  className?: string;
}) {
  return (
    <label className={`toggle${disabled ? " disabled" : ""}${className ? ` ${className}` : ""}`} title={title}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="toggle-track">
        <span className="toggle-thumb" />
      </span>
    </label>
  );
}

// ── Small async-action button with busy/error state ─────────────────────────

export function AsyncButton({
  onClick,
  children,
  className,
  disabled,
  title,
}: {
  onClick: () => Promise<void>;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  title?: string;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <button
      className={className}
      disabled={disabled || busy}
      title={title}
      onClick={async () => {
        setBusy(true);
        try {
          await onClick();
        } finally {
          setBusy(false);
        }
      }}
    >
      {busy ? "…" : children}
    </button>
  );
}
