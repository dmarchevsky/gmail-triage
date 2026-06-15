import { useEffect, useState } from "react";
import { GmailLabel, Settings, del, get, post, put } from "../api";
import { AsyncButton, Badge, ConfirmDialog } from "../components";
import { useToast } from "../toast";
import { useApp } from "../App";

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

  useEffect(() => {
    get<GmailLabel[]>("/gmail/labels")
      .then(setLabels)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
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
    </div>
  );
}

export function GmailConnect({ onChange }: { onChange?: () => void }) {
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
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="settings-section">
      <h3>Gmail</h3>
      {connected ? (
        <>
          <p>
            Connected as <b>{status?.gmail.email}</b>{" "}
            <Badge tone={status?.gmail.status === "auth_error" ? "error" : "ok"}>
              {status?.gmail.status}
            </Badge>
          </p>
          <p className="sub">
            Scope: <code>gmail.modify</code> only — MailTriage cannot send email.
          </p>
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

function AuthSection({
  settings,
  onChange,
}: {
  settings: Settings;
  onChange: () => Promise<void> | void;
}) {
  const toast = useToast();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [disablePw, setDisablePw] = useState("");
  const [confirmingDisable, setConfirmingDisable] = useState(false);
  const authActive = !settings.auth_disabled;

  const changePassword = async () => {
    if (!next.trim()) {
      toast.error("New password must not be empty");
      return;
    }
    if (next !== confirm) {
      toast.error("New passwords do not match");
      return;
    }
    try {
      await put("/auth/password", { current_password: current, new_password: next });
      toast.success("Password changed");
      setCurrent("");
      setNext("");
      setConfirm("");
      await onChange();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const enableAuth = async () => {
    try {
      await post("/auth/enable");
      toast.success("Password authentication enabled");
      await onChange();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="settings-section">
      <h3>
        Authentication{" "}
        {authActive ? (
          <Badge tone="ok">password required</Badge>
        ) : (
          <Badge tone="warn">password disabled</Badge>
        )}
      </h3>
      {authActive ? (
        <>
          <p className="sub">
            Change the web UI password. Stored encrypted in the database — no restart
            needed. {settings.ui_password_hash_configured
              ? "A custom password is set."
              : "Currently using the UI_PASSWORD from the environment."}
          </p>
          <div className="form-grid">
            <label>
              Current password
              <input
                type="password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
              />
            </label>
            <span />
            <label>
              New password
              <input
                type="password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
              />
            </label>
            <label>
              Confirm new password
              <input
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
              />
            </label>
          </div>
          <div className="head-actions">
            <button className="primary" onClick={changePassword}>
              Change password
            </button>
            <button className="danger" onClick={() => setConfirmingDisable(true)}>
              Disable password
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="sub">
            Password authentication is <b>disabled</b> — anyone who can reach this app
            has full access. Re-enable it to require the password again.
          </p>
          <button className="primary" onClick={enableAuth}>
            Enable password
          </button>
        </>
      )}

      {confirmingDisable && (
        <ConfirmDialog
          title="Disable password authentication?"
          danger
          confirmLabel="Disable password"
          message={
            <>
              <p>
                Anyone who can reach this app will have full access with no login. Enter
                your current password to confirm.
              </p>
              <input
                type="password"
                placeholder="Current password"
                value={disablePw}
                onChange={(e) => setDisablePw(e.target.value)}
                autoFocus
              />
            </>
          }
          onConfirm={async () => {
            try {
              await post("/auth/disable", { current_password: disablePw });
              setConfirmingDisable(false);
              setDisablePw("");
              toast.success("Password authentication disabled");
              await onChange();
            } catch (e) {
              toast.error(e instanceof Error ? e.message : String(e));
            }
          }}
          onCancel={() => {
            setConfirmingDisable(false);
            setDisablePw("");
          }}
        />
      )}
    </div>
  );
}

export default function SettingsPage() {
  const { refresh } = useApp();
  const toast = useToast();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [telegramToken, setTelegramToken] = useState("");
  const [confirmingPurge, setConfirmingPurge] = useState(false);
  const [confirmingReset, setConfirmingReset] = useState(false);

  const load = () => get<Settings>("/settings").then(setSettings);
  useEffect(() => {
    load();
  }, []);

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
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const numberFields: [keyof Settings, string, string][] = [
    ["poll_interval_seconds", "Polling interval (seconds, min 60)", "300"],
    ["initial_lookback_hours", "Initial lookback (hours; 0 = only new mail)", "24"],
    ["classify_body_max_chars", "Classification body budget (chars)", "2000"],
    ["digest_body_max_chars", "Digest body budget (chars/email)", "6000"],
    ["llm_classify_timeout_seconds", "LLM classify timeout (s)", "120"],
    ["llm_digest_timeout_seconds", "LLM digest timeout (s)", "300"],
    ["llm_max_concurrency", "LLM max in-flight requests", "1"],
  ];

  return (
    <div>
      <header className="page-head">
        <h2>Settings</h2>
      </header>
      <GmailConnect onChange={load} />

      <MailboxScope settings={settings} onSave={saveValues} />

      <div className="settings-section">
        <h3>Polling & processing</h3>
        <div className="form-grid">
          {numberFields.map(([key, label, placeholder]) => (
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
          <label className="checkbox span2">
            <input
              type="checkbox"
              checked={settings.store_bodies}
              onChange={(e) => saveValues({ store_bodies: e.target.checked })}
            />
            Store email bodies in DB (default off — bodies are re-fetched from Gmail
            when needed)
          </label>
          <label className="span2">
            Ignored senders (one glob/regex per line; skipped before the LLM)
            <textarea
              rows={3}
              value={
                draft.ignore_senders !== undefined
                  ? draft.ignore_senders
                  : settings.ignore_senders.join("\n")
              }
              onChange={(e) => setDraft({ ...draft, ignore_senders: e.target.value })}
            />
          </label>
        </div>
        <button
          className="primary"
          onClick={() => {
            const values: Record<string, unknown> = {};
            for (const [key] of numberFields)
              if (draft[key] !== undefined) values[key] = Number(draft[key]);
            if (draft.ignore_senders !== undefined)
              values.ignore_senders = draft.ignore_senders
                .split("\n")
                .map((s) => s.trim())
                .filter(Boolean);
            saveValues(values);
          }}
        >
          Save processing settings
        </button>
      </div>

      <div className="settings-section">
        <h3>LLM endpoint</h3>
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
            Model name (ignored by single-model llama.cpp)
            <input
              value={draft.llm_model !== undefined ? draft.llm_model : settings.llm_model}
              onChange={(e) => setDraft({ ...draft, llm_model: e.target.value })}
            />
          </label>
        </div>
        <div className="head-actions">
          <button
            className="primary"
            onClick={() =>
              saveValues({
                ...(draft.llm_base_url !== undefined && { llm_base_url: draft.llm_base_url }),
                ...(draft.llm_model !== undefined && { llm_model: draft.llm_model }),
              })
            }
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
        <h3>Telegram</h3>
        <div className="form-grid">
          <label>
            Bot token{" "}
            {settings.telegram_bot_token_configured && <Badge tone="ok">configured ✓</Badge>}
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
        </div>
        <div className="head-actions">
          <button
            className="primary"
            onClick={() => {
              const values: Record<string, unknown> = {};
              if (telegramToken) values.telegram_bot_token = telegramToken;
              if (draft.telegram_default_chat_id !== undefined)
                values.telegram_default_chat_id = draft.telegram_default_chat_id;
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

      <AuthSection
        settings={settings}
        onChange={async () => {
          await load();
          await refresh();
        }}
      />

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
              toast.error(e instanceof Error ? e.message : String(e));
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
              toast.error(e instanceof Error ? e.message : String(e));
            }
          }}
          onCancel={() => setConfirmingReset(false)}
        />
      )}
    </div>
  );
}
