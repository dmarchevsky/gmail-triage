// Human-friendly rendering of audit-log events for the Dashboard "Recent
// activity" feed. The backend emits ~38 distinct event_types (see
// app/services/audit.py callers); each carries a small JSON payload. We map
// the event_type to a Title Case label and summarize the payload as prose —
// never raw JSON.

import { actionLabel } from "./components";

export const EVENT_LABELS: Record<string, string> = {
  // auth
  password_changed: "Password changed",
  auth_disabled: "Auth disabled",
  auth_enabled: "Auth enabled",
  // gmail
  gmail_connected: "Gmail connected",
  gmail_connect_failed: "Gmail connect failed",
  gmail_disconnected: "Gmail disconnected",
  // categories
  category_created: "Category created",
  category_updated: "Category updated",
  category_deleted: "Category deleted",
  categories_bulk_updated: "Categories updated",
  quick_label_created: "Quick label created",
  // labels
  label_created: "Label created",
  label_updated: "Label updated",
  label_deleted: "Label deleted",
  // rules
  rule_created: "Rule created",
  rule_updated: "Rule updated",
  rule_deleted: "Rule deleted",
  rules_bulk_updated: "Rules updated",
  rules_reordered: "Rules reordered",
  planned_actions_applied: "Planned actions applied",
  // digests
  digest_created: "Digest created",
  digest_updated: "Digest updated",
  digest_deleted: "Digest deleted",
  digests_bulk_updated: "Digests updated",
  digest_run: "Digest sent",
  // feedback / criteria
  feedback_created: "Feedback added",
  feedback_dismissed: "Feedback dismissed",
  criteria_proposal_generated: "Criteria proposal",
  criteria_proposal_approved: "Criteria proposal approved",
  criteria_proposal_rejected: "Criteria proposal rejected",
  // settings / admin
  settings_updated: "Settings updated",
  settings_imported: "Settings imported",
  data_purged: "Data purged",
  // poller / classifier
  poll_run_now: "Poll (manual)",
  poll_completed: "Poll completed",
  poller_paused: "Poller paused",
  poller_resumed: "Poller resumed",
  actions_executed: "Actions applied",
  actions_planned: "Actions planned (dry-run)",
  actions_failed: "Actions failed",
};

// Turn a snake_case event_type into a readable title when not in EVENT_LABELS.
export function humanize(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : key;
}

export function eventLabel(eventType: string): string {
  return EVENT_LABELS[eventType] ?? humanize(eventType);
}

type Payload = Record<string, unknown>;

const asObj = (p: unknown): Payload =>
  p && typeof p === "object" && !Array.isArray(p) ? (p as Payload) : {};

const joinActions = (v: unknown): string =>
  Array.isArray(v) ? v.map((a) => actionLabel(String(a))).join(", ") : "";

// Human reference to the email/rule a payload points at. The backend enriches
// recent-activity payloads with email_from/email_subject/rule_name; fall back
// to the id when an enriched field is absent (e.g. the row was purged).
const emailRef = (p: Payload): string => {
  const parts = [p.email_subject, p.email_from].filter(Boolean).map(String);
  if (parts.length) return parts.join(" · ");
  return p.email_id != null ? `email #${p.email_id}` : "";
};

const ruleRef = (p: Payload): string => {
  if (typeof p.rule_name === "string") return `“${p.rule_name}”`;
  const id = p.rule_id ?? p.id;
  return id != null ? `rule #${id}` : "rule";
};

// Per-event detail formatters for the information-rich events.
const FORMATTERS: Record<string, (p: Payload) => string> = {
  poll_completed: (p) =>
    `${p.new_emails ?? 0} new email(s)${p.mode ? ` (${p.mode})` : ""}`,
  poll_run_now: (p) => `${p.new_emails ?? 0} new email(s)`,
  actions_executed: (p) => {
    const live = joinActions(p.live_actions);
    return `${live || "no actions"} — ${emailRef(p)}`;
  },
  actions_planned: (p) => {
    const planned = joinActions(p.planned_actions);
    return `${planned || "no actions"} (dry-run) — ${emailRef(p)}`;
  },
  actions_failed: (p) => `${emailRef(p)} failed: ${p.error ?? "unknown error"}`,
  feedback_created: (p) => `Re: ${emailRef(p)}`,
  quick_label_created: (p) => `Quick label via ${ruleRef(p)}`,
  rule_updated: (p) => ruleRef(p),
  planned_actions_applied: (p) =>
    `${ruleRef(p)}: applied ${p.applied ?? 0}, failed ${p.failed ?? 0} across ${p.emails ?? 0} email(s)`,
  digest_run: (p) =>
    `Digest #${p.digest_id} — ${p.status ?? "?"}, ${p.email_count ?? 0} email(s)`,
  settings_updated: (p) =>
    Array.isArray(p.keys) && p.keys.length ? `Changed: ${p.keys.join(", ")}` : "",
  settings_imported: (p) =>
    Array.isArray(p.keys) && p.keys.length ? `Imported: ${p.keys.join(", ")}` : "",
  criteria_proposal_generated: (p) => `Covers ${p.covers ?? 0} example(s)`,
  criteria_proposal_approved: (p) =>
    `v${p.new_version}${
      Array.isArray(p.covered) ? `, covered ${p.covered.length} feedback item(s)` : ""
    }${p.edited ? " (edited)" : ""}`,
  data_purged: (p) => {
    const deleted = asObj(p.deleted);
    return Object.entries(deleted)
      .map(([k, v]) => `${k}: ${v}`)
      .join(", ");
  },
  gmail_connected: (p) => String(p.email ?? ""),
  gmail_connect_failed: (p) => String(p.error ?? ""),
  rules_reordered: (p) =>
    Array.isArray(p.order) ? `${p.order.length} rule(s) reordered` : "",
};

// Generic fallback: prefer a human name, otherwise a compact "key: value"
// summary of scalar fields. Never JSON.stringify; empty payload -> "".
function genericDetail(p: Payload): string {
  if (typeof p.name === "string") return p.name;
  const parts: string[] = [];
  for (const [k, v] of Object.entries(p)) {
    if (v == null || typeof v === "object") continue;
    parts.push(`${k}: ${v}`);
  }
  return parts.join(", ");
}

export function describeActivity(eventType: string, payload: unknown): string {
  const p = asObj(payload);
  const fmt = FORMATTERS[eventType];
  return fmt ? fmt(p) : genericDetail(p);
}
