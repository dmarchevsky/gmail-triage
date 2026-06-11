import { useEffect, useState } from "react";
import { Settings, del, get, post, put } from "../api";
import { AsyncButton, Badge, ConfirmDialog, ErrorNote } from "../components";
import { useApp } from "../App";

export function GmailConnect({ onChange }: { onChange?: () => void }) {
  const { status, refresh } = useApp();
  const [credsJson, setCredsJson] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState(false);
  const connected = status?.gmail.connected;

  const start = async () => {
    setError(null);
    try {
      const r = await post<{ auth_url: string }>("/gmail/oauth/start", {
        client_secret_json: credsJson || null,
      });
      window.location.href = r.auth_url;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
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
          <ErrorNote error={error} />
          <button className="primary" onClick={start}>
            Connect Gmail
          </button>
        </>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const { refresh } = useApp();
  const [settings, setSettings] = useState<Settings | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [telegramToken, setTelegramToken] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => get<Settings>("/settings").then(setSettings);
  useEffect(() => {
    load();
  }, []);

  if (!settings) return <p>Loading…</p>;

  const num = (key: keyof Settings) =>
    draft[key] !== undefined ? draft[key] : String(settings[key] ?? "");

  const saveValues = async (values: Record<string, unknown>) => {
    setError(null);
    setNote(null);
    try {
      setSettings(await put<Settings>("/settings", values));
      setDraft({});
      setNote("Saved.");
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
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
      {note && <p className="note">{note}</p>}
      <ErrorNote error={error} />

      <GmailConnect onChange={load} />

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
              setNote(
                r.ok
                  ? `LLM OK — models: ${r.models?.join(", ")}`
                  : `LLM unreachable: ${r.error}`,
              );
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
                setNote(r.ok ? "Telegram test message sent ✓" : `Telegram failed: ${r.error}`);
              } catch (e) {
                setNote(`Telegram failed: ${e instanceof Error ? e.message : e}`);
              }
            }}
          >
            Send test message
          </AsyncButton>
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={settings.dry_run_telegram_prefix}
            onChange={(e) => saveValues({ dry_run_telegram_prefix: e.target.checked })}
          />
          In dry-run, still send digests with a [DRY RUN] prefix (default: don't send)
        </label>
      </div>

      <div className="settings-section">
        <h3>Config export</h3>
        <p className="sub">Full configuration as JSON, excluding secrets.</p>
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
      </div>
    </div>
  );
}
