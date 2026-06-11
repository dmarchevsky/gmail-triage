import { useCallback, useEffect, useState } from "react";
import { get, post, StatusResponse, ApiError } from "./api";

function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    try {
      await post("/auth/login", { password });
      onLogin();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Login failed");
    }
  };

  return (
    <div className="login-page">
      <form onSubmit={submit} className="login-form">
        <h1>MailTriage</h1>
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoFocus
        />
        <button type="submit">Log in</button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}

function StatusBadge({ label, value }: { label: string; value: string }) {
  const okStates = ["ok", "connected", "running"];
  const cls = okStates.includes(value) ? "badge ok" : "badge warn";
  return (
    <span className={cls}>
      {label}: {value}
    </span>
  );
}

function Dashboard() {
  const [status, setStatus] = useState<StatusResponse | null>(null);

  const refresh = useCallback(async () => {
    setStatus(await get<StatusResponse>("/status"));
  }, []);

  useEffect(() => {
    refresh();
    const id = setInterval(refresh, 10000);
    return () => clearInterval(id);
  }, [refresh]);

  if (!status) return <p>Loading…</p>;

  return (
    <div className="dashboard">
      <header>
        <h1>MailTriage</h1>
        {status.dry_run && <span className="badge dry-run">DRY RUN</span>}
      </header>
      <div className="status-bar">
        <StatusBadge label="Gmail" value={status.gmail.connected ? "connected" : "not connected"} />
        <StatusBadge label="LLM" value={status.llm.status} />
        <StatusBadge label="Telegram" value={status.telegram.status} />
        <StatusBadge label="Poller" value={status.poller.status} />
      </div>
      <p className="hint">
        UI shell (M0). Full dashboard, emails, categories, rules, digests and
        feedback views arrive in later milestones.
      </p>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);

  useEffect(() => {
    get<{ authenticated: boolean }>("/auth/session")
      .then((s) => setAuthed(s.authenticated))
      .catch(() => setAuthed(false));
  }, []);

  if (authed === null) return <p>Loading…</p>;
  if (!authed) return <Login onLogin={() => setAuthed(true)} />;
  return <Dashboard />;
}
