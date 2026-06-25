import { createContext, useCallback, useContext, useEffect, useState } from "react";
import {
  HashRouter,
  NavLink,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import {
  Filter,
  LayoutDashboard,
  Mail,
  MessageCircle,
  Send,
  Settings as SettingsIcon,
  Tag,
  Tags,
} from "lucide-react";
import { ApiError, Settings, StatusResponse, get, post } from "./api";
import { ToastProvider, useToast } from "./toast";
import Dashboard from "./pages/Dashboard";
import Emails from "./pages/Emails";
import Categories from "./pages/Categories";
import Labels from "./pages/Labels";
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
  const items: { dot: string; text: string; title: string; to: string }[] = [
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
      to: "/settings?tab=mailbox",
    },
    {
      dot: status.llm.status === "ok"
        ? "ok"
        : status.llm.status === "unreachable"
          ? "error"
          : "neutral",
      text: `LLM: ${status.llm.status}`,
      title: "LLM endpoint",
      to: "/settings?tab=processing",
    },
    {
      dot: status.telegram.status === "ok" || status.telegram.status === "configured"
        ? "ok"
        : status.telegram.status === "error"
          ? "error"
          : "neutral",
      text: `Telegram: ${status.telegram.status}`,
      title: "Telegram bot",
      to: "/settings?tab=notifications",
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
      to: "/settings?tab=mailbox",
    },
  ];
  const rules = status.rules_mode;
  return (
    <div className="sidebar-status">
      {items.map((item) => (
        <NavLink to={item.to} key={item.text} title={item.title}>
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
  const toast = useToast();
  const navigate = useNavigate();
  const [navOpen, setNavOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem("sidebarCollapsed") === "1",
  );
  const location = useLocation();
  // Close the mobile nav drawer whenever the route changes.
  useEffect(() => setNavOpen(false), [location.pathname]);

  // Handle OAuth callback redirects: backend appends ?gmail_connected=1 or
  // ?gmail_error=auth_failed to the root URL after the Google consent flow.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const connected = params.get("gmail_connected");
    const err = params.get("gmail_error");
    if (!connected && !err) return;
    window.history.replaceState({}, "", window.location.pathname + window.location.hash);
    if (connected === "1") {
      toast.success("Gmail connected successfully");
      navigate("/settings?tab=mailbox");
    } else if (err === "auth_failed") {
      toast.error("Gmail connection failed — please try again");
      navigate("/settings?tab=mailbox");
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleCollapsed = () =>
    setCollapsed((c) => {
      localStorage.setItem("sidebarCollapsed", c ? "0" : "1");
      return !c;
    });

  if (settings && !settings.first_run_complete) {
    return (
      <Routes>
        <Route path="/wizard" element={<Wizard />} />
        <Route path="*" element={<Navigate to="/wizard" replace />} />
      </Routes>
    );
  }
  const closeNav = () => setNavOpen(false);
  return (
    <div className={`shell ${collapsed ? "sidebar-collapsed" : ""}`}>
      <div className="mobile-bar">
        <button
          className="icon-btn hamburger"
          aria-label="Toggle navigation"
          aria-expanded={navOpen}
          onClick={() => setNavOpen((o) => !o)}
        >
          ☰
        </button>
        <span className="mobile-title">MailTriage</span>
      </div>
      {navOpen && <div className="nav-backdrop" onClick={closeNav} />}
      <aside className={`sidebar ${navOpen ? "open" : ""}`}>
        <div className="logo-row">
          <img
            className="logo-mark"
            src="/favicon.svg"
            alt="MailTriage"
            width={20}
            height={20}
            style={{ cursor: "pointer" }}
            title={collapsed ? "Expand sidebar" : undefined}
            onClick={collapsed ? toggleCollapsed : undefined}
          />
          <h1 className="logo">MailTriage</h1>
          <button
            className="collapse-btn icon-btn"
            onClick={toggleCollapsed}
            title="Collapse sidebar"
            aria-label="Collapse sidebar"
          >
            «
          </button>
        </div>
        <nav onClick={closeNav}>
          <NavLink to="/" title="Dashboard">
            <LayoutDashboard size={16} /><span className="nav-label">Dashboard</span>
          </NavLink>
          <NavLink to="/emails" title="Emails">
            <Mail size={16} /><span className="nav-label">Emails</span>
          </NavLink>
          <NavLink to="/categories" title="Categories">
            <Tag size={16} /><span className="nav-label">Categories</span>
          </NavLink>
          <NavLink to="/labels" title="Labels">
            <Tags size={16} /><span className="nav-label">Labels</span>
          </NavLink>
          <NavLink to="/rules" title="Rules">
            <Filter size={16} /><span className="nav-label">Rules</span>
          </NavLink>
          <NavLink to="/digests" title="Digests">
            <Send size={16} /><span className="nav-label">Digests</span>
          </NavLink>
          <NavLink to="/feedback" title="Feedback">
            <MessageCircle size={16} /><span className="nav-label">Feedback</span>
          </NavLink>
          <NavLink to="/settings" title="Settings">
            <SettingsIcon size={16} /><span className="nav-label">Settings</span>
          </NavLink>
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
          <Route path="/labels" element={<Labels />} />
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

  // Heartbeat: status only. Settings change only on explicit user save, so the
  // 15s poll below uses this rather than refetching /settings every tick.
  const refreshStatus = useCallback(async () => {
    setStatus(await get<StatusResponse>("/status"));
  }, []);

  const refresh = useCallback(async () => {
    await refreshStatus();
    try {
      setSettings(await get<Settings>("/settings"));
    } catch {
      /* not authed yet */
    }
  }, [refreshStatus]);

  useEffect(() => {
    get<{ authenticated: boolean }>("/auth/session")
      .then((s) => setAuthed(s.authenticated))
      .catch(() => setAuthed(false));
  }, []);

  useEffect(() => {
    if (!authed) return;
    refresh(); // initial load: status + settings
    const id = setInterval(refreshStatus, 15000); // heartbeat: status only
    return () => clearInterval(id);
  }, [authed, refresh, refreshStatus]);

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
