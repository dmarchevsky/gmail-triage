import { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  HashRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";
import { ApiError, Settings, StatusResponse, get, post } from "./api";
import { ToastProvider } from "./toast";
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

function SidebarStatus() {
  const { status } = useApp();
  if (!status) return null;
  const items: { dot: string; text: string; title: string }[] = [
    {
      dot: !status.gmail.connected
        ? "warn"
        : status.gmail.status === "auth_error"
          ? "error"
          : "ok",
      text: status.gmail.connected
        ? `Gmail: ${status.gmail.email ?? "connected"}`
        : "Gmail: not connected",
      title: status.gmail.status === "auth_error" ? "Gmail auth error" : "Gmail",
    },
    {
      dot: status.llm.status === "ok"
        ? "ok"
        : status.llm.status === "unreachable"
          ? "error"
          : "neutral",
      text: `LLM: ${status.llm.status}`,
      title: "LLM endpoint",
    },
    {
      dot: status.telegram.status === "ok" || status.telegram.status === "configured"
        ? "ok"
        : status.telegram.status === "error"
          ? "error"
          : "neutral",
      text: `Telegram: ${status.telegram.status}`,
      title: "Telegram bot",
    },
    {
      dot: status.poller.paused
        ? "warn"
        : status.poller.status === "running"
          ? "ok"
          : "warn",
      text: `Poller: ${status.poller.paused ? "paused" : status.poller.status}`,
      title: status.poller.last_error
        ? `Last error: ${status.poller.last_error}`
        : "Poller",
    },
  ];
  const rules = status.rules_mode;
  return (
    <div className="sidebar-status">
      {items.map((item) => (
        <NavLink to="/settings" key={item.text} title={item.title}>
          <span className={`status-dot ${item.dot}`} />
          <span className="label">{item.text}</span>
        </NavLink>
      ))}
      <NavLink to="/rules" title="Rules execution mode">
        <span
          className={`status-dot ${rules.dry > 0 ? "warn" : rules.live > 0 ? "ok" : "neutral"}`}
        />
        <span className="label">
          Rules: {rules.live} live · {rules.dry} dry
        </span>
      </NavLink>
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
          <SidebarStatus />
        </div>
      </aside>
      <main className="content">
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
      <ToastProvider>
        <HashRouter>
          <Shell />
        </HashRouter>
      </ToastProvider>
    </AppContext.Provider>
  );
}
