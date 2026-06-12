// Typed fetch wrapper + API types for MailTriage.

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T = unknown>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const resp = await fetch(`/api/v1${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    credentials: "same-origin",
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      if (body.detail)
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep statusText */
    }
    throw new ApiError(resp.status, detail);
  }
  return resp.json() as Promise<T>;
}

export const get = <T>(path: string) => api<T>(path);
export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const del = <T>(path: string) => api<T>(path, { method: "DELETE" });

export interface StatusResponse {
  ok: boolean;
  version: string;
  gmail: { connected: boolean; email: string | null; status: string };
  llm: { status: string };
  telegram: { status: string };
  poller: {
    status: string;
    last_run_at: string | null;
    last_error: string | null;
    paused: boolean;
  };
  rules_mode: { live: number; dry: number };
}

export interface Category {
  id: number;
  name: string;
  description: string | null;
  gmail_label_name: string;
  criteria_md: string;
  criteria_version: number;
  enabled: boolean;
  created_at: string | null;
  updated_at: string | null;
}

export interface CriteriaVersion {
  version: number;
  criteria_md: string;
  source: string;
  feedback_ids: number[] | null;
  created_at: string | null;
}

export interface RuleAction {
  type: "add_label" | "remove_label" | "mark_read" | "archive" | "trash";
  category_id?: number;
  label_name?: string;
}

export interface Rule {
  id: number;
  name: string;
  enabled: boolean;
  priority: number;
  match_category_id: number | null;
  match_min_confidence: number;
  match_sender_pattern: string | null;
  actions: RuleAction[];
  stop_processing: boolean;
  dry_run: boolean;
  pending_planned: number;
}

export interface EmailAction {
  id: number;
  rule_id: number | null;
  action_type: string;
  action_params: Record<string, unknown> | null;
  executed: boolean;
  dry_run: boolean;
  executed_at: string | null;
  error: string | null;
}

export interface EmailRow {
  id: number;
  gmail_message_id: string;
  received_at: string | null;
  sender: string | null;
  subject: string | null;
  snippet: string | null;
  classification_id: number | null;
  classification: string | null;
  confidence: number | null;
  status: string;
  dry_run: boolean;
  actions: EmailAction[];
  rationale?: string | null;
  llm_model?: string | null;
  classified_at?: string | null;
  error?: string | null;
}

export interface EmailList {
  total: number;
  page: number;
  page_size: number;
  items: EmailRow[];
}

export interface Stats {
  today: { processed: number; actions_executed: number; actions_planned_dry_run: number };
  week: { processed: number; actions_executed: number; actions_planned_dry_run: number };
  by_category: { category: string; count: number }[];
  recent_activity: { ts: string | null; actor: string; event_type: string; payload: unknown }[];
  category_precision: {
    category_id: number;
    category: string;
    classified_total: number;
    flagged_wrong: number;
    precision: number | null;
  }[];
}

export interface Settings {
  poll_interval_seconds: number;
  initial_lookback_hours: number;
  store_bodies: boolean;
  classify_body_max_chars: number;
  digest_body_max_chars: number;
  llm_base_url: string;
  llm_model: string;
  llm_classify_timeout_seconds: number;
  llm_digest_timeout_seconds: number;
  llm_max_concurrency: number;
  telegram_bot_token_configured: boolean;
  telegram_default_chat_id: string;
  gmail_client_secret_json_configured: boolean;
  ui_password_hash_configured: boolean;
  ignore_senders: string[];
  poller_paused: boolean;
  first_run_complete: boolean;
}

export interface Digest {
  id: number;
  name: string;
  enabled: boolean;
  category_ids: number[];
  cron_times: string[];
  timezone: string;
  min_confidence: number;
  telegram_chat_id: string | null;
  include_links: boolean;
  include_metadata: boolean;
  max_emails: number;
  send_no_news: boolean;
}

export interface DigestRun {
  id: number;
  started_at: string | null;
  finished_at: string | null;
  status: string;
  email_ids: number[];
  summary_text: string | null;
  error: string | null;
}

export interface FeedbackItem {
  id: number;
  email_id: number;
  email_subject: string | null;
  email_sender: string | null;
  original_category: string | null;
  correct_category_id: number | null;
  correct_category: string | null;
  user_note: string | null;
  status: string;
  proposed_criteria_md: string | null;
  proposal_explanation: string | null;
  proposal_status: string;
  created_at: string | null;
}
