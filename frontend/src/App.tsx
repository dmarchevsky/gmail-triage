import { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  HashRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";
import { ApiError, Settings, StatusResponse, get, post, put } from "./api";
import { Badge, ConfirmDialog } from "./components";
import Dashboard from "./pages/Dashboard";
import Emails from "./pages/Emails";
import Categories from "./pages/Categories";
import Rules from "./pages/Rules";
import Digests from "./pages/Digests";
import FeedbackQueue from "./pages/FeedbackQueue";
import SettingsPage from "./pages/Settings";
import Wizard from "./pages/Wizard";

interface AppContextValue {
  status: StatusResponse | null;
  settings: Settings | null;
  refresh: () => Promise<void>;
}

const AppContext = createContext<AppContextValue>({
  status: null,
  settings: null,
  refresh: async () => {},
});

export const useApp = () => useContext(AppContext);

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
        <button type="submit" className="primary">
          Log in
        </button>
        {error && <p className="error">{error}</p>}
      </form>
    </div>
  );
}

function DryRunToggle() {
  const { status, refresh } = useApp();
  const [confirming, setConfirming] = useState(false);
  if (!status) return null;
  const dryRun = status.dry_run;

  const toggle = async () => {
    await put("/dry-run", { enabled: !dryRun });
    await refresh();
  };

  return (
    <>
      <button
        className={`dry-run-toggle ${dryRun ? "on" : "off"}`}
        title={dryRun ? "Dry-run is ON: no Gmail changes are made" : "LIVE: actions execute"}
        onClick={() => (dryRun ? setConfirming(true) : toggle())}
      >
        {dryRun ? "DRY RUN" : "LIVE"}
      </button>
      {confirming && (
        <ConfirmDialog
          title="Disable dry-run?"
          danger
          confirmLabel="Go live"
          message={
            <p>
              Turning dry-run <b>off</b> means rules will <b>really</b> modify your
              Gmail: labels, mark-read, archive and trash actions will execute.
              Previously planned (dry-run) actions are <b>not</b> executed
              retroactively.
            </p>
          }
          onConfirm={async () => {
            setConfirming(false);
            await toggle();
          }}
          onCancel={() => setConfirming(false)}
        />
      )}
    </>
  );
}

function StatusStrip() {
  const { status } = useApp();
  if (!status) return null;
  const tone = (ok: boolean) => (ok ? "ok" : "warn");
  return (
    <div className="status-strip">
      <Badge tone={tone(status.gmail.connected && status.gmail.status !== "auth_error")}>
        Gmail{status.gmail.email ? `: ${status.gmail.email}` : ""}
        {status.gmail.status === "auth_error" ? " (auth error)" : ""}
      </Badge>
      <Badge tone={tone(status.llm.status === "ok")}>LLM: {status.llm.status}</Badge>
      <Badge tone={status.telegram.status === "ok" ? "ok" : "neutral"}>
        Telegram: {status.telegram.status}
      </Badge>
      <Badge tone={status.poller.paused ? "warn" : tone(status.poller.status === "running")}>
        Poller: {status.poller.paused ? "paused" : status.poller.status}
      </Badge>
    </div>
  );
}

function Shell() {
  const { settings } = useApp();
  if (settings && !settings.first_run_complete) {
    return (
      <Routes>
        <Route path="/wizard" element={<Wizard />} />
        <Route path="*" element={<Navigate to="/wizard" replace />} />
      </Routes>
    );
  }
  return (
    <div className="shell">
      <aside className="sidebar">
        <h1 className="logo">MailTriage</h1>
        <nav>
          <NavLink to="/">Dashboard</NavLink>
          <NavLink to="/emails">Emails</NavLink>
          <NavLink to="/categories">Categories</NavLink>
          <NavLink to="/rules">Rules</NavLink>
          <NavLink to="/digests">Digests</NavLink>
          <NavLink to="/feedback">Feedback</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
        <div className="sidebar-foot">
          <DryRunToggle />
        </div>
      </aside>
      <main className="content">
        <StatusStrip />
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/emails" element={<Emails />} />
          <Route path="/categories" element={<Categories />} />
          <Route path="/rules" element={<Rules />} />
          <Route path="/digests" element={<Digests />} />
          <Route path="/feedback" element={<FeedbackQueue />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/wizard" element={<Wizard />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean | null>(null);
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);

  const refresh = useCallback(async () => {
    const s = await get<StatusResponse>("/status");
    setStatus(s);
    try {
      setSettings(await get<Settings>("/settings"));
    } catch {
      /* not authed yet */
    }
  }, []);

  useEffect(() => {
    get<{ authenticated: boolean }>("/auth/session")
      .then((s) => setAuthed(s.authenticated))
      .catch(() => setAuthed(false));
  }, []);

  useEffect(() => {
    if (!authed) return;
    refresh();
    const id = setInterval(refresh, 15000);
    return () => clearInterval(id);
  }, [authed, refresh]);

  if (authed === null) return <p className="center-note">Loading…</p>;
  if (!authed) return <Login onLogin={() => setAuthed(true)} />;

  return (
    <AppContext.Provider value={{ status, settings, refresh }}>
      <HashRouter>
        <Shell />
      </HashRouter>
    </AppContext.Provider>
  );
}
