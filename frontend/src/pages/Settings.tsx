import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { GmailLabel, Settings, del, errMsg, get, post, put } from "../api";
import { AsyncButton, Badge, ConfirmDialog, ErrorNote } from "../components";
import { useToast } from "../toast";
import { useApp } from "../App";
import { PushState, disablePush, enablePush, pushState } from "../push";

/** Shared state handed to the prop-driven section components. */
interface IngestionProps {
  settings: Settings;
  draft: Record<string, string>;
  setDraft: (d: Record<string, string>) => void;
  saveValues: (values: Record<string, unknown>) => Promise<void>;
}

function MailboxScope({
  settings,
  onSave,
}: {
  settings: Settings;
  onSave: (values: Record<string, unknown>) => Promise<void>;
}) {
  const toast = useToast();
  const [labels, setLabels] = useState<GmailLabel[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>(settings.poll_scope_labels);
  const [ignore, setIgnore] = useState<string>(settings.ignore_senders.join("\n"));

  // Re-sync from the prop when the persisted value actually changes (e.g. after
  // a save/import elsewhere) — keyed on the joined value so an unrelated parent
  // re-render doesn't reset an in-progress edit.
  useEffect(() => {
    setSelected(settings.poll_scope_labels);
  }, [settings.poll_scope_labels.join(",")]);
  useEffect(() => {
    setIgnore(settings.ignore_senders.join("\n"));
  }, [settings.ignore_senders.join("\n")]);

  useEffect(() => {
    get<GmailLabel[]>("/gmail/labels")
      .then(setLabels)
      .catch((e) => setError(errMsg(e)));
  }, []);

  const toggle = (id: string) =>
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );

  return (
    <div className="settings-section">
      <h3>Mailbox scope</h3>
      <p className="sub">
        Which Gmail categories/labels MailTriage polls and triages. Promotions,
        Updates, etc. often skip the inbox, so include them here to triage them.
        Sent, Drafts, Spam and Trash are always excluded. Changes affect future
        polls; to pull in past mail, raise the initial lookback and re-poll.
      </p>
      {error && (
        <p className="sub">
          Connect Gmail to choose categories. ({error})
        </p>
      )}
      {labels && (
        <>
          <div className="head-actions" style={{ flexDirection: "column", alignItems: "flex-start" }}>
            {labels.map((lb) => (
              <label key={lb.id} className="checkbox">
                <input
                  type="checkbox"
                  checked={selected.includes(lb.id)}
                  onChange={() => toggle(lb.id)}
                />
                {lb.display_name}
                {lb.type === "user" && <span className="sub"> (label)</span>}
              </label>
            ))}
          </div>
          <button
            className="primary"
            onClick={async () => {
              if (selected.length === 0)
                toast.error("Nothing selected — MailTriage will ingest no mail");
              await onSave({ poll_scope_labels: selected });
            }}
          >
            Save mailbox scope
          </button>
        </>
      )}
      <div className="form-grid" style={{ marginTop: "1rem" }}>
        <label className="span2">
          Ignored senders (one glob/regex per line; skipped before the LLM)
          <textarea
            rows={3}
            value={ignore}
            onChange={(e) => setIgnore(e.target.value)}
          />
        </label>
      </div>
      <button
        className="primary"
        onClick={() =>
          onSave({
            ignore_senders: ignore
              .split("\n")
              .map((s) => s.trim())
              .filter(Boolean),
          })
        }
      >
        Save ignored senders
      </button>
    </div>
  );
}

/** Ingestion mode + store-bodies + mode-conditional config, shown inside the
 *  Gmail card once connected. */
function IngestionControls({ settings, draft, setDraft, saveValues }: IngestionProps) {
  const { status } = useApp();
  const mode = settings.gmail_ingest_mode;
  const val = (key: keyof Settings) =>
    draft[key] !== undefined ? draft[key] : String(settings[key] ?? "");

  return (
    <div className="ingestion-config">
      <div className="form-grid">
        <label className="checkbox span2" style={{ marginBottom: "0.75rem" }}>
          <input
            type="checkbox"
            checked={settings.store_bodies}
            onChange={(e) => saveValues({ store_bodies: e.target.checked })}
          />
          Store email bodies in DB (default off — bodies are re-fetched from Gmail when needed)
        </label>
        <label>
          Ingestion mode
          <select
            value={mode}
            onChange={(e) => saveValues({ gmail_ingest_mode: e.target.value })}
          >
            <option value="poll">Polling</option>
            <option value="push">Push (Pub/Sub pull)</option>
          </select>
        </label>
      </div>

      {mode === "poll" ? (
        <>
          <div className="form-grid">
            <label>
              Polling interval (seconds, min 60)
              <input
                type="number"
                placeholder="300"
                value={val("poll_interval_seconds")}
                onChange={(e) =>
                  setDraft({ ...draft, poll_interval_seconds: e.target.value })
                }
              />
            </label>
            <label>
              Initial lookback (hours; 0 = only new mail)
              <input
                type="number"
                placeholder="24"
                value={val("initial_lookback_hours")}
                onChange={(e) =>
                  setDraft({ ...draft, initial_lookback_hours: e.target.value })
                }
              />
            </label>
          </div>
          <button
            className="primary"
            onClick={() => {
              const values: Record<string, unknown> = {};
              if (draft.poll_interval_seconds !== undefined)
                values.poll_interval_seconds = Number(draft.poll_interval_seconds);
              if (draft.initial_lookback_hours !== undefined)
                values.initial_lookback_hours = Number(draft.initial_lookback_hours);
              saveValues(values);
            }}
          >
            Save polling settings
          </button>
        </>
      ) : (
        <>
          <p className="sub">
            Real-time via Gmail <code>watch</code> + a Cloud Pub/Sub <b>pull</b>
            {" "}subscription — outbound-only, no inbound endpoint. Polling keeps
            running as a safety net. See the README for the one-time Google Cloud
            setup (topic, pull subscription, IAM). Consumer:{" "}
            <Badge
              tone={
                status?.ingest.pubsub_status === "running"
                  ? "ok"
                  : status?.ingest.pubsub_status === "error"
                    ? "error"
                    : "neutral"
              }
            >
              {status?.ingest.pubsub_status ?? "stopped"}
            </Badge>
          </p>
          <div className="form-grid">
            <label className="span2">
              Pub/Sub topic (full resource name)
              <input
                placeholder="projects/my-project/topics/gmail-triage"
                value={val("gmail_pubsub_topic")}
                onChange={(e) => setDraft({ ...draft, gmail_pubsub_topic: e.target.value })}
              />
            </label>
            <label className="span2">
              Pub/Sub pull subscription (full resource name)
              <input
                placeholder="projects/my-project/subscriptions/gmail-triage-sub"
                value={val("gmail_pubsub_subscription")}
                onChange={(e) =>
                  setDraft({ ...draft, gmail_pubsub_subscription: e.target.value })
                }
              />
            </label>
          </div>
          <p className="sub">
            After saving, <b>reconnect Gmail</b> above so consent also grants the
            (non-send) <code>pubsub</code> scope.
          </p>
          <button
            className="primary"
            onClick={() => {
              const values: Record<string, unknown> = {};
              if (draft.gmail_pubsub_topic !== undefined)
                values.gmail_pubsub_topic = draft.gmail_pubsub_topic.trim();
              if (draft.gmail_pubsub_subscription !== undefined)
                values.gmail_pubsub_subscription = draft.gmail_pubsub_subscription.trim();
              saveValues(values);
            }}
          >
            Save Pub/Sub settings
          </button>
        </>
      )}
    </div>
  );
}

export function GmailConnect({
  onChange,
  ingestion,
}: {
  onChange?: () => void;
  ingestion?: IngestionProps;
}) {
  const { status, refresh } = useApp();
  const toast = useToast();
  const [credsJson, setCredsJson] = useState("");
  const [disconnecting, setDisconnecting] = useState(false);
  const connected = status?.gmail.connected;

  const start = async () => {
    try {
      const r = await post<{ auth_url: string }>("/gmail/oauth/start", {
        client_secret_json: credsJson || null,
      });
      window.location.href = r.auth_url;
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  return (
    <div className="settings-section">
      <h3>
        Gmail{" "}
        {connected ? (
          <Badge
            tone={
              status?.gmail.status === "auth_error" || status?.gmail.status === "error"
                ? "error"
                : "ok"
            }
          >
            {status?.gmail.status === "auth_error"
              ? "auth error"
              : status?.gmail.status === "error"
                ? "error"
                : "connected"}
          </Badge>
        ) : (
          <Badge tone="warn">not connected</Badge>
        )}
      </h3>
      {connected ? (
        status?.gmail.status === "auth_error" ? (
          <>
            <ErrorNote
              error={status.poller.last_error ?? "Token expired or revoked."}
            />
            <p>
              Was connected as <b>{status.gmail.email}</b>
            </p>
            <AsyncButton className="primary" onClick={start}>
              Reconnect
            </AsyncButton>{" "}
            <button className="danger" onClick={() => setDisconnecting(true)}>
              Disconnect
            </button>
            {disconnecting && (
              <ConfirmDialog
                title="Disconnect Gmail?"
                danger
                confirmLabel="Disconnect"
                message={<p>The token is revoked at Google and removed locally.</p>}
                onConfirm={async () => {
                  await del("/gmail/auth");
                  setDisconnecting(false);
                  await refresh();
                  onChange?.();
                }}
                onCancel={() => setDisconnecting(false)}
              />
            )}
          </>
        ) : (
          <>
            <p>
              Connected as <b>{status?.gmail.email}</b>{" "}
              <Badge
                tone={status?.gmail.status === "error" ? "error" : "ok"}
              >
                {status?.gmail.status}
              </Badge>
            </p>
            <p className="sub">
              Scope: <code>gmail.modify</code> only — MailTriage cannot send email.
            </p>
            {ingestion && <IngestionControls {...ingestion} />}
            <button
              className="danger"
              onClick={() => setDisconnecting(true)}
            >
              Disconnect
            </button>
            {disconnecting && (
              <ConfirmDialog
                title="Disconnect Gmail?"
                danger
                confirmLabel="Disconnect"
                message={<p>The token is revoked at Google and removed locally.</p>}
                onConfirm={async () => {
                  await del("/gmail/auth");
                  setDisconnecting(false);
                  await refresh();
                  onChange?.();
                }}
                onCancel={() => setDisconnecting(false)}
              />
            )}
          </>
        )
      ) : (
        <>
          <p className="sub">
            Paste the OAuth client credentials JSON from Google Cloud Console
            (Desktop or Web application client). MailTriage requests the{" "}
            <code>gmail.modify</code> scope only — it cannot send email.
          </p>
          <textarea
            rows={4}
            placeholder='{"installed": {"client_id": "...", "client_secret": "..."}}'
            value={credsJson}
            onChange={(e) => setCredsJson(e.target.value)}
          />
          <button className="primary" onClick={start}>
            Connect Gmail
          </button>
        </>
      )}
    </div>
  );
}

const PUSH_STATE_TEXT: Record<PushState, string> = {
  on: "On for this device.",
  off: "Off for this device.",
  denied:
    "Notifications are blocked for this site. Allow them in the browser / system " +
    "settings, then reload.",
  "needs-install":
    "On iPhone and iPad, notifications work only in the installed app: tap Share → " +
    "Add to Home Screen, open MailTriage from the Home Screen, then turn them on here.",
  unsupported: "This browser doesn't support push notifications.",
};

/** Web Push for the current browser / installed app (per device). */
function DevicePushSection() {
  const toast = useToast();
  const [state, setState] = useState<PushState | null>(null);
  const reload = () => pushState().then(setState, () => setState("unsupported"));
  useEffect(() => {
    reload();
  }, []);

  const run = async (fn: () => Promise<void>, ok: string) => {
    try {
      await fn();
      toast.success(ok);
    } catch (e) {
      toast.error(errMsg(e));
    }
    await reload();
  };

  return (
    <div className="settings-section">
      <h3>
        Notifications on this device{" "}
        {state && (
          <Badge tone={state === "on" ? "ok" : state === "denied" ? "error" : "neutral"}>
            {state === "needs-install" ? "install app" : state}
          </Badge>
        )}
      </h3>
      <p className="sub">
        Digests with <b>In-app</b> delivery show up in the Digests inbox and are pushed as a
        system notification to every device turned on here. Tapping it opens the digest.
      </p>
      {state && <p>{PUSH_STATE_TEXT[state]}</p>}
      <div className="head-actions">
        {state === "off" && (
          <AsyncButton
            className="primary"
            onClick={() => run(enablePush, "Notifications turned on")}
          >
            Turn on
          </AsyncButton>
        )}
        {state === "on" && (
          <>
            <AsyncButton
              onClick={async () => {
                try {
                  const r = await post<{ sent: number; removed: number; errors: string[] }>(
                    "/push/test",
                  );
                  if (r.sent > 0) toast.success(`Test sent to ${r.sent} device(s)`);
                  else toast.error(r.errors[0] ?? "No device accepted the notification");
                } catch (e) {
                  toast.error(errMsg(e));
                }
              }}
            >
              Send test notification
            </AsyncButton>
            <AsyncButton onClick={() => run(disablePush, "Notifications turned off")}>
              Turn off
            </AsyncButton>
          </>
        )}
      </div>
    </div>
  );
}

function AccessSection() {
  const { session } = useApp();
  const toast = useToast();
  const signOut = async () => {
    try {
      const r = await post<{ logout_url: string | null }>("/auth/logout");
      if (r.logout_url) window.location.href = r.logout_url;
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const dev = session?.mode === "dev";
  return (
    <div className="settings-section">
      <h3>
        Authentication{" "}
        {dev ? (
          <Badge tone="warn">disabled (DEV_AUTH)</Badge>
        ) : (
          <Badge tone="ok">Cloudflare Access</Badge>
        )}
      </h3>
      {dev ? (
        <p className="sub">
          <code>DEV_AUTH</code> is on: every request is let through without sign-in. It is
          for local development only — production uses Cloudflare Access.
        </p>
      ) : (
        <>
          <p className="sub">
            Signed in with Google as <b>{session?.email}</b>. Who may sign in is set by the
            Cloudflare Access policy and the <code>CF_ACCESS_ALLOWED_EMAILS</code>{" "}
            environment variable.
          </p>
          <div className="head-actions">
            <button onClick={signOut}>Sign out</button>
          </div>
        </>
      )}
    </div>
  );
}

/** Classifier backend switch: generative LLM vs. System One decision model. */
function ClassifierBackend({ settings, saveValues }: Omit<IngestionProps, "draft" | "setDraft">) {
  const toast = useToast();
  const [url, setUrl] = useState(settings.decider_base_url);
  const [model, setModel] = useState(settings.decider_model);
  const [timeout, setTimeoutS] = useState(String(settings.decider_timeout_seconds));
  const [models, setModels] = useState<string[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const decider = settings.classifier_backend === "decider";

  const loadModels = async () => {
    try {
      const r = await post<{ ok: boolean; models?: string[]; error?: string }>(
        "/llm/decider/models",
        { base_url: url },
      );
      setModels(r.models ?? []);
      setModelsError(r.ok ? null : (r.error ?? "unknown error"));
    } catch (e) {
      setModels([]);
      setModelsError(errMsg(e));
    }
  };
  useEffect(() => {
    if (decider) loadModels();
  }, [decider]);

  const modelOptions = model && !models.includes(model) ? [model, ...models] : models;

  return (
    <div className="settings-section">
      <h3>
        Classifier backend{" "}
        <Badge tone={decider ? "info" : "neutral"}>
          {decider ? `decider · ${settings.decider_model}` : "generative LLM"}
        </Badge>
      </h3>
      <p className="sub">
        <b>LLM</b> classifies and summarizes each email in one generative call.{" "}
        <b>Decider</b> picks the category with a decision model (System One{" "}
        <code>/systemone</code>, e.g. d3-nano): fast and deterministic, returns real
        probabilities, but writes no text — summaries, digest synthesis and criteria
        suggestions still use the LLM below. Decider probabilities are calibrated differently
        from LLM confidence, so review rule confidence thresholds after switching.
      </p>
      <div className="form-grid">
        <label>
          Backend
          <select
            value={settings.classifier_backend}
            onChange={(e) => saveValues({ classifier_backend: e.target.value })}
          >
            <option value="llm">Generative LLM</option>
            <option value="decider">Decider (System One)</option>
          </select>
        </label>
        {decider && (
          <>
            <label>
              Summaries in decider mode
              <select
                value={settings.decider_summary_source}
                onChange={(e) => saveValues({ decider_summary_source: e.target.value })}
              >
                <option value="llm">Separate LLM call (falls back to snippet on failure)</option>
                <option value="snippet">Gmail snippet only (no LLM)</option>
              </select>
            </label>
            <label>
              Decider base URL (empty = same as LLM)
              <input
                placeholder="http://192.168.6.11:8731/v1"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
              />
            </label>
            <label>
              Decision model
              <span className="input-action">
                <select value={model} onChange={(e) => setModel(e.target.value)}>
                  {modelOptions.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  title="Reload decision models (uses the base URL above)"
                  aria-label="Reload decision models"
                  onClick={loadModels}
                >
                  ↻
                </button>
              </span>
              {modelsError && <span className="sub">Couldn't load models: {modelsError}</span>}
            </label>
            <label>
              Decider timeout (s)
              <input
                type="number"
                min="1"
                value={timeout}
                onChange={(e) => setTimeoutS(e.target.value)}
              />
            </label>
          </>
        )}
      </div>
      {decider && (
        <div className="head-actions">
          <button
            className="primary"
            onClick={() =>
              saveValues({
                decider_base_url: url.trim(),
                decider_model: model,
                decider_timeout_seconds: Number(timeout) || 30,
              })
            }
          >
            Save decider settings
          </button>
          <AsyncButton
            onClick={async () => {
              const r = await post<{
                ok: boolean;
                error?: string;
                model?: string;
                latency_ms?: number;
              }>("/llm/decider/test", { base_url: url, model });
              if (r.ok) toast.success(`Decider OK — ${r.model}, ${r.latency_ms} ms`);
              else toast.error(`Decider unreachable: ${r.error}`);
            }}
          >
            Test decider
          </AsyncButton>
        </div>
      )}
    </div>
  );
}

const TABS: { id: string; label: string }[] = [
  { id: "mailbox", label: "Mailbox" },
  { id: "processing", label: "Processing" },
  { id: "notifications", label: "Notifications" },
  { id: "security", label: "Security" },
  { id: "data", label: "Data" },
];

export default function SettingsPage() {
  const { refresh, status, session } = useApp();
  const toast = useToast();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [telegramToken, setTelegramToken] = useState("");
  const [llmApiKey, setLlmApiKey] = useState("");
  const [llmModels, setLlmModels] = useState<string[]>([]);
  const [llmModelsLoading, setLlmModelsLoading] = useState(false);
  const [llmModelsError, setLlmModelsError] = useState<string | null>(null);
  const [confirmingPurge, setConfirmingPurge] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);
  const [detectedContext, setDetectedContext] = useState<number | null>(null);
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") ?? "mailbox";

  const load = () => get<Settings>("/settings").then(setSettings);
  useEffect(() => {
    load();
    get<{ detected: number | null }>("/llm/context")
      .then((r) => setDetectedContext(r.detected))
      .catch(() => setDetectedContext(null));
  }, []);

  // Model picker options from the endpoint's GET /models. Unsaved draft
  // URL/key are sent as overrides so the list reflects what's being edited.
  const loadModels = async (baseUrl = "", apiKey = "") => {
    setLlmModelsLoading(true);
    try {
      const r = await post<{ ok: boolean; models?: string[]; error?: string }>(
        "/llm/models",
        { base_url: baseUrl, api_key: apiKey },
      );
      setLlmModels(r.models ?? []);
      setLlmModelsError(r.ok ? null : (r.error ?? "unknown error"));
    } catch (e) {
      setLlmModels([]);
      setLlmModelsError(errMsg(e));
    } finally {
      setLlmModelsLoading(false);
    }
  };
  useEffect(() => {
    if (tab === "processing") loadModels();
  }, [tab]);

  if (!settings) return <p>Loading…</p>;

  const num = (key: keyof Settings) =>
    draft[key] !== undefined ? draft[key] : String(settings[key] ?? "");

  const saveValues = async (values: Record<string, unknown>) => {
    try {
      setSettings(await put<Settings>("/settings", values));
      setDraft({});
      toast.success("Settings saved");
      await refresh();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  // Per-tab traffic-light dot: green = healthy, amber = needs attention/setup,
  // red = error, grey = no live status. Every tab always shows one. Within a tab,
  // errors are checked before warnings so a red state is never masked by an amber one.
  type DotTone = "ok" | "warn" | "error" | "neutral";
  const tabStatus = (id: string): { tone: DotTone; label: string } | null => {
    if (!status) return { tone: "neutral", label: "Loading…" };
    switch (id) {
      case "mailbox":
        if (status.gmail.status === "auth_error" || status.gmail.status === "error")
          return { tone: "error", label: "Gmail authentication error" };
        if (status.ingest.mode === "push" && status.ingest.pubsub_status === "error")
          return { tone: "error", label: "Push ingestion error" };
        if (!status.gmail.connected) return { tone: "warn", label: "Gmail not connected" };
        if (status.poller.paused) return { tone: "warn", label: "Polling paused" };
        return { tone: "ok", label: "Connected" };
      case "processing":
        if (status.llm.status === "unreachable")
          return { tone: "error", label: "LLM unreachable" };
        if (status.llm.status === "ok") return { tone: "ok", label: "LLM reachable" };
        return { tone: "neutral", label: "LLM status unknown" };
      case "notifications":
        if (status.telegram.status === "error")
          return { tone: "error", label: "Telegram error" };
        if (status.telegram.status === "unconfigured")
          return { tone: "warn", label: "Telegram not configured" };
        return { tone: "ok", label: "Telegram ready" };
      case "security":
        return session?.mode === "dev"
          ? { tone: "warn", label: "Authentication disabled (DEV_AUTH)" }
          : { tone: "ok", label: "Cloudflare Access" };
      default:
        return null;
    }
  };

  // Keep the saved model selectable even if the endpoint no longer lists it.
  const modelValue = draft.llm_model !== undefined ? draft.llm_model : settings.llm_model;
  const modelOptions =
    modelValue && !llmModels.includes(modelValue) ? [modelValue, ...llmModels] : llmModels;

  const llmFields: [keyof Settings, string, string][] = [
    ["classify_body_max_chars", "Body budget (chars; classify + summarize)", "2000"],
    ["llm_classify_timeout_seconds", "LLM classify timeout (s)", "120"],
    ["llm_digest_timeout_seconds", "LLM digest timeout (s)", "300"],
    ["llm_max_concurrency", "LLM max in-flight requests", "1"],
    [
      "llm_max_context_tokens",
      detectedContext
        ? `LLM max context (tokens; 0 = auto, detected ${detectedContext})`
        : "LLM max context (tokens; 0 = auto)",
      "0",
    ],
  ];

  const promptFields: [keyof Settings, string][] = [
    ["prompt_classification_system", "Email classification — system prompt"],
    ["prompt_summary_concise", "Email summary — concise depth"],
    ["prompt_summary_default", "Email summary — default depth"],
    ["prompt_summary_extended", "Email summary — extended depth"],
    [
      "prompt_digest_synthesis",
      "Digest synthesis (used when digest mode = synthesize; {max_chars} is substituted)",
    ],
  ];

  return (
    <div>
      <header className="page-head">
        <h2>Settings</h2>
      </header>

      <nav className="settings-tabs">
        {TABS.map((t) => {
          const ts = tabStatus(t.id);
          return (
            <button
              key={t.id}
              className={t.id === tab ? "active" : ""}
              onClick={() => setParams({ tab: t.id }, { replace: true })}
            >
              {ts && (
                <span
                  className={`status-dot ${ts.tone}`}
                  title={ts.label}
                  aria-label={ts.label}
                  role="img"
                />
              )}
              {t.label}
            </button>
          );
        })}
      </nav>

      {tab === "mailbox" && (
        <>
          <GmailConnect
            onChange={load}
            ingestion={{ settings, draft, setDraft, saveValues }}
          />
          <MailboxScope settings={settings} onSave={saveValues} />
        </>
      )}

      {tab === "processing" && (
        <>
          <ClassifierBackend settings={settings} saveValues={saveValues} />
          <div className="settings-section">
            <h3>
              LLM Processing{" "}
              <Badge
                tone={
                  status?.llm.status === "ok"
                    ? "ok"
                    : status?.llm.status === "unreachable"
                      ? "error"
                      : "warn"
                }
              >
                {status?.llm.status === "ok" ? "reachable" : (status?.llm.status ?? "unknown")}
              </Badge>
            </h3>
            <div className="form-grid">
              <label>
                Base URL (empty = LLM_BASE_URL env)
                <input
                  placeholder="http://host.docker.internal:8081/v1"
                  value={draft.llm_base_url !== undefined ? draft.llm_base_url : settings.llm_base_url}
                  onChange={(e) => setDraft({ ...draft, llm_base_url: e.target.value })}
                />
              </label>
              <label>
                API key (optional; empty for keyless local servers)
                <input
                  type="password"
                  autoComplete="off"
                  placeholder={
                    settings.llm_api_key_configured ? "configured — leave blank to keep" : ""
                  }
                  value={llmApiKey}
                  onChange={(e) => setLlmApiKey(e.target.value)}
                />
                {settings.llm_api_key_configured && (
                  <span className="sub">
                    Key saved.{" "}
                    <button
                      type="button"
                      className="link-btn"
                      onClick={() => saveValues({ llm_api_key: "" })}
                    >
                      Clear
                    </button>
                  </span>
                )}
              </label>
              <label>
                Model
                <span className="input-action">
                  <select
                    disabled={llmModelsLoading}
                    value={modelValue}
                    onChange={(e) => setDraft({ ...draft, llm_model: e.target.value })}
                  >
                    <option value="">(server default)</option>
                    {modelOptions.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    title="Reload models (uses the base URL and API key above)"
                    aria-label="Reload models"
                    disabled={llmModelsLoading}
                    onClick={() =>
                      loadModels(
                        draft.llm_base_url !== undefined
                          ? draft.llm_base_url
                          : settings.llm_base_url,
                        llmApiKey,
                      )
                    }
                  >
                    ↻
                  </button>
                </span>
                {llmModelsLoading ? (
                  <span className="sub">Loading models…</span>
                ) : llmModelsError ? (
                  <span className="sub">Couldn't load models: {llmModelsError}</span>
                ) : null}
              </label>
              {llmFields.map(([key, label, placeholder]) => (
                <label key={key}>
                  {label}
                  <input
                    type="number"
                    placeholder={placeholder}
                    value={num(key)}
                    onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                  />
                </label>
              ))}
              <label>
                Synthesis temperature (0 = deterministic)
                <input
                  type="number"
                  step="0.05"
                  min="0"
                  max="2"
                  placeholder="0"
                  value={num("llm_synthesis_temperature")}
                  onChange={(e) =>
                    setDraft({ ...draft, llm_synthesis_temperature: e.target.value })
                  }
                />
              </label>
              <label>
                Synthesis token budget (0 = auto ~939)
                <input
                  type="number"
                  min="0"
                  placeholder="0"
                  value={num("llm_synthesis_max_tokens")}
                  onChange={(e) =>
                    setDraft({ ...draft, llm_synthesis_max_tokens: e.target.value })
                  }
                />
              </label>
              <label className="checkbox span2">
                <input
                  type="checkbox"
                  checked={settings.llm_synthesis_enable_thinking}
                  onChange={(e) =>
                    saveValues({ llm_synthesis_enable_thinking: e.target.checked })
                  }
                />
                Enable thinking (for reasoning models; off suppresses chain-of-thought
                via chat_template_kwargs)
              </label>
              <label>
                Summarization depth (applied to newly-classified emails)
                <select
                  value={settings.summarization_depth}
                  onChange={(e) => saveValues({ summarization_depth: e.target.value })}
                >
                  <option value="concise">Concise — one short line</option>
                  <option value="default">Default — 1-2 sentences</option>
                  <option value="extended">Extended — short paragraph</option>
                </select>
              </label>
            </div>
            <div className="head-actions">
              <button
                className="primary"
                onClick={() => {
                  const values: Record<string, unknown> = {};
                  if (draft.llm_base_url !== undefined) values.llm_base_url = draft.llm_base_url;
                  if (draft.llm_model !== undefined) values.llm_model = draft.llm_model;
                  if (llmApiKey) values.llm_api_key = llmApiKey;
                  for (const [key] of llmFields)
                    if (draft[key] !== undefined) values[key] = Number(draft[key]);
                  if (draft.llm_synthesis_temperature !== undefined)
                    values.llm_synthesis_temperature = Number(draft.llm_synthesis_temperature);
                  if (draft.llm_synthesis_max_tokens !== undefined)
                    values.llm_synthesis_max_tokens = Number(draft.llm_synthesis_max_tokens);
                  saveValues(values).then(() => setLlmApiKey(""));
                }}
              >
                Save LLM settings
              </button>
              <AsyncButton
                onClick={async () => {
                  const r = await post<{ ok: boolean; error?: string; models?: string[] }>(
                    "/llm/test",
                  );
                  if (r.ok) toast.success(`LLM OK — models: ${r.models?.join(", ")}`);
                  else toast.error(`LLM unreachable: ${r.error}`);
                }}
              >
                Test LLM connection
              </AsyncButton>
            </div>
          </div>

          <div className="settings-section">
            <h3>LLM Prompts</h3>
            <div className="form-grid">
              {promptFields.map(([key, label]) => (
                <label key={key} className="span2">
                  {label}
                  <textarea
                    rows={5}
                    value={num(key)}
                    onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                  />
                </label>
              ))}
            </div>
            <button
              className="primary"
              onClick={() => {
                const values: Record<string, unknown> = {};
                for (const [key] of promptFields)
                  if (draft[key] !== undefined) values[key] = draft[key];
                saveValues(values);
              }}
            >
              Save prompts
            </button>
          </div>
        </>
      )}

      {tab === "notifications" && (
        <div className="settings-section">
          <h3>
            Telegram{" "}
            {!settings.telegram_bot_token_configured ? (
              <Badge tone="warn">not configured</Badge>
            ) : (
              <Badge tone={status?.telegram.status === "error" ? "error" : "ok"}>
                {status?.telegram.status ?? "configured"}
              </Badge>
            )}
          </h3>
          <div className="form-grid">
            <label>
              Bot token
              <input
                type="password"
                placeholder="123456:ABC-DEF…"
                value={telegramToken}
                onChange={(e) => setTelegramToken(e.target.value)}
              />
            </label>
            <label>
              Default chat id
              <input
                value={
                  draft.telegram_default_chat_id !== undefined
                    ? draft.telegram_default_chat_id
                    : settings.telegram_default_chat_id
                }
                onChange={(e) =>
                  setDraft({ ...draft, telegram_default_chat_id: e.target.value })
                }
              />
            </label>
            <label className="span2">
              Public base URL (optional)
              <input
                placeholder="https://mail.example.com"
                value={
                  draft.public_base_url !== undefined
                    ? draft.public_base_url
                    : settings.public_base_url
                }
                onChange={(e) =>
                  setDraft({ ...draft, public_base_url: e.target.value })
                }
              />
              <span className="sub">
                The address MailTriage is served at (the Cloudflare tunnel
                hostname). Used for the Gmail OAuth redirect, the sign-out
                return address, and the reconnect link in Telegram auth-error
                alerts. Add{" "}
                <code>&lt;this-url&gt;/api/v1/gmail/oauth/callback</code> as
                an authorized redirect URI in your Google Cloud OAuth client,
                or reconnecting Gmail will fail.
              </span>
            </label>
          </div>
          <div className="head-actions">
            <button
              className="primary"
              onClick={() => {
                const values: Record<string, unknown> = {};
                if (telegramToken) values.telegram_bot_token = telegramToken;
                if (draft.telegram_default_chat_id !== undefined)
                  values.telegram_default_chat_id = draft.telegram_default_chat_id;
                if (draft.public_base_url !== undefined)
                  values.public_base_url = draft.public_base_url.trim();
                saveValues(values).then(() => setTelegramToken(""));
              }}
            >
              Save Telegram settings
            </button>
            <AsyncButton
              onClick={async () => {
                try {
                  const r = await post<{ ok: boolean; error?: string }>("/telegram/test");
                  if (r.ok) toast.success("Telegram test message sent");
                  else toast.error(`Telegram failed: ${r.error}`);
                } catch (e) {
                  toast.error(`Telegram failed: ${e instanceof Error ? e.message : e}`);
                }
                await refresh();
              }}
            >
              Send test message
            </AsyncButton>
          </div>
        </div>
      )}
      {tab === "notifications" && <DevicePushSection />}

      {tab === "security" && <AccessSection />}

      {tab === "data" && (
        <>
          <div className="settings-section">
            <h3>Retention</h3>
            <p className="sub">
              Emails older than this are permanently deleted once per day.
              Set to 0 to keep emails forever.
            </p>
            <div className="form-grid">
              <label>
                Retention period (days; 0 = keep forever)
                <input
                  type="number"
                  min="0"
                  placeholder="90"
                  value={num("retention_days")}
                  onChange={(e) =>
                    setDraft({ ...draft, retention_days: e.target.value })
                  }
                />
              </label>
            </div>
            <button
              className="primary"
              onClick={() => {
                if (draft.retention_days !== undefined)
                  saveValues({ retention_days: Number(draft.retention_days) });
                setDraft({});
              }}
            >
              Save
            </button>
          </div>

          <div className="settings-section">
            <h3>Config export / import</h3>
            <p className="sub">Full configuration as JSON, excluding secrets.</p>
            <div className="head-actions">
              <AsyncButton
                onClick={async () => {
                  const data = await get<Record<string, unknown>>("/settings");
                  const blob = new Blob([JSON.stringify(data, null, 2)], {
                    type: "application/json",
                  });
                  const a = document.createElement("a");
                  a.href = URL.createObjectURL(blob);
                  a.download = "mailtriage-settings.json";
                  a.click();
                }}
              >
                Export settings JSON
              </AsyncButton>
              <label className="checkbox">
                Import:
                <input
                  type="file"
                  accept="application/json"
                  onChange={async (e) => {
                    const file = e.target.files?.[0];
                    if (!file) return;
                    try {
                      const parsed = JSON.parse(await file.text());
                      const r = await post<{ imported: string[] }>(
                        "/settings/import",
                        parsed,
                      );
                      toast.success(`Imported: ${r.imported.join(", ")}`);
                      load();
                    } catch (err) {
                      toast.error(err instanceof Error ? err.message : String(err));
                    }
                    e.target.value = "";
                  }}
                />
              </label>
            </div>
          </div>

          <div className="settings-section danger-zone">
            <h3>Danger zone</h3>
            <p className="sub">
              Both operations only touch MailTriage's local database — nothing in your
              Gmail changes (labels already applied stay).
            </p>
            <div className="head-actions">
              <button className="danger" onClick={() => setConfirmingPurge(true)}>
                Purge processing data
              </button>
              <button className="danger" onClick={() => setConfirmingReset(true)}>
                Factory reset
              </button>
            </div>
          </div>

          {confirmingPurge && (
            <ConfirmDialog
              title="Purge processing data?"
              danger
              confirmLabel="Purge data"
              message={
                <>
                  <p>
                    <b>Deletes:</b> all ingested emails and their classifications,
                    planned/executed action records, digest run history, feedback, and
                    the audit log.
                  </p>
                  <p>
                    <b>Keeps:</b> Gmail connection, categories, rules, digests, and
                    settings.
                  </p>
                  <p>
                    The sync watermark is reset, so the next poll re-ingests and
                    re-classifies the initial-lookback window like a first run.
                  </p>
                </>
              }
              onConfirm={async () => {
                setConfirmingPurge(false);
                try {
                  const r = await post<{ deleted: Record<string, number> }>(
                    "/admin/purge-data",
                  );
                  toast.success(
                    `Purged: ${Object.entries(r.deleted)
                      .map(([table, n]) => `${table} ${n}`)
                      .join(", ")}`,
                  );
                  await refresh();
                } catch (e) {
                  toast.error(errMsg(e));
                }
              }}
              onCancel={() => setConfirmingPurge(false)}
            />
          )}
          {confirmingReset && (
            <ConfirmDialog
              title="Factory reset?"
              danger
              confirmLabel="Reset everything"
              message={
                <p>
                  <b>Everything</b> is deleted: the Gmail connection is revoked and
                  removed, and all emails, categories (with criteria history), rules,
                  digests, feedback and settings are wiped. You'll be taken back to the
                  first-run wizard. This cannot be undone.
                </p>
              }
              onConfirm={async () => {
                setConfirmingReset(false);
                try {
                  await post("/admin/factory-reset");
                  toast.success("Factory reset complete");
                  await refresh();
                } catch (e) {
                  toast.error(errMsg(e));
                }
              }}
              onCancel={() => setConfirmingReset(false)}
            />
          )}
        </>
      )}
    </div>
  );
}
