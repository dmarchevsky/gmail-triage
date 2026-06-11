import { useCallback, useEffect, useState } from "react";
import { Category, EmailList, EmailRow, get, post } from "../api";
import { Badge, ErrorNote, Modal, fmtDate, pct } from "../components";

function statusTone(status: string): "ok" | "warn" | "error" | "neutral" {
  if (status === "actioned" || status === "classified") return "ok";
  if (status === "error") return "error";
  if (status === "pending") return "warn";
  return "neutral";
}

function FeedbackForm({
  email,
  categories,
  onDone,
}: {
  email: EmailRow;
  categories: Category[];
  onDone: () => void;
}) {
  const [categoryId, setCategoryId] = useState<string>("none");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  return (
    <div className="feedback-form">
      <h4>Classification wrong?</h4>
      <label>
        Correct category:{" "}
        <select value={categoryId} onChange={(e) => setCategoryId(e.target.value)}>
          <option value="none">none (no category applies)</option>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </label>
      <textarea
        placeholder="Optional note for the criteria revision (e.g. why this is/isn't MarketNews)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <ErrorNote error={error} />
      <button
        className="primary"
        onClick={async () => {
          setError(null);
          try {
            await post(`/emails/${email.id}/feedback`, {
              correct_category_id: categoryId === "none" ? null : Number(categoryId),
              user_note: note || null,
            });
            onDone();
          } catch (e) {
            setError(e instanceof Error ? e.message : String(e));
          }
        }}
      >
        Submit feedback
      </button>
    </div>
  );
}

function EmailDetail({
  emailId,
  categories,
  onClose,
}: {
  emailId: number;
  categories: Category[];
  onClose: () => void;
}) {
  const [email, setEmail] = useState<EmailRow | null>(null);
  const [feedbackSent, setFeedbackSent] = useState(false);

  useEffect(() => {
    get<EmailRow>(`/emails/${emailId}`).then(setEmail);
  }, [emailId]);

  if (!email) return null;
  return (
    <Modal title={email.subject || "(no subject)"} onClose={onClose} wide>
      <div className="email-detail">
        <p>
          <b>{email.sender}</b> · {fmtDate(email.received_at)}{" "}
          {email.dry_run && <Badge tone="dry">dry-run</Badge>}{" "}
          <Badge tone={statusTone(email.status)}>{email.status}</Badge>
        </p>
        <p className="snippet">{email.snippet}</p>
        <p>
          Classification: <b>{email.classification ?? "none"}</b> ·{" "}
          {pct(email.confidence)} confidence
          {email.llm_model ? ` · ${email.llm_model}` : ""}
        </p>
        {email.rationale && (
          <p className="rationale">
            <b>Rationale:</b> {email.rationale}
          </p>
        )}
        {email.error && <p className="error">Error: {email.error}</p>}

        <h4>{email.dry_run ? "Planned actions (dry-run)" : "Actions"}</h4>
        {email.actions.length === 0 && <p className="sub">No actions.</p>}
        <ul className="action-list">
          {email.actions.map((a) => (
            <li key={a.id}>
              <code>{a.action_type}</code>{" "}
              {a.action_params?.label_name ? `→ ${a.action_params.label_name}` : ""}
              {a.executed ? (
                <Badge tone="ok">executed {fmtDate(a.executed_at)}</Badge>
              ) : a.dry_run ? (
                <Badge tone="dry">planned</Badge>
              ) : (
                <Badge tone="error">{a.error ? `failed: ${a.error}` : "not executed"}</Badge>
              )}
            </li>
          ))}
        </ul>

        {feedbackSent ? (
          <p className="note">Feedback recorded — see the Feedback page for proposals.</p>
        ) : (
          <FeedbackForm
            email={email}
            categories={categories}
            onDone={() => setFeedbackSent(true)}
          />
        )}
      </div>
    </Modal>
  );
}

export default function Emails() {
  const [list, setList] = useState<EmailList | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState({
    category_id: "",
    status: "",
    confidence_min: "",
    confidence_max: "",
    q: "",
  });

  const load = useCallback(async () => {
    const params = new URLSearchParams();
    params.set("page", String(page));
    if (filters.category_id) params.set("category_id", filters.category_id);
    if (filters.status) params.set("status", filters.status);
    if (filters.confidence_min) params.set("confidence_min", filters.confidence_min);
    if (filters.confidence_max) params.set("confidence_max", filters.confidence_max);
    if (filters.q) params.set("q", filters.q);
    setList(await get<EmailList>(`/emails?${params.toString()}`));
  }, [page, filters]);

  useEffect(() => {
    load();
  }, [load]);
  useEffect(() => {
    get<Category[]>("/categories").then(setCategories);
  }, []);

  const totalPages = list ? Math.max(1, Math.ceil(list.total / list.page_size)) : 1;

  return (
    <div>
      <header className="page-head">
        <h2>Emails</h2>
      </header>

      <div className="filters">
        <input
          placeholder="Search subject/sender…"
          value={filters.q}
          onChange={(e) => {
            setPage(1);
            setFilters({ ...filters, q: e.target.value });
          }}
        />
        <select
          value={filters.category_id}
          onChange={(e) => {
            setPage(1);
            setFilters({ ...filters, category_id: e.target.value });
          }}
        >
          <option value="">All categories</option>
          <option value="0">none / unclassified</option>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <select
          value={filters.status}
          onChange={(e) => {
            setPage(1);
            setFilters({ ...filters, status: e.target.value });
          }}
        >
          <option value="">All statuses</option>
          {["pending", "classified", "actioned", "skipped", "error"].map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <input
          type="number"
          step="0.1"
          min="0"
          max="1"
          placeholder="min conf"
          value={filters.confidence_min}
          onChange={(e) => {
            setPage(1);
            setFilters({ ...filters, confidence_min: e.target.value });
          }}
        />
        <input
          type="number"
          step="0.1"
          min="0"
          max="1"
          placeholder="max conf"
          value={filters.confidence_max}
          onChange={(e) => {
            setPage(1);
            setFilters({ ...filters, confidence_max: e.target.value });
          }}
        />
      </div>

      <table className="table emails-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Sender</th>
            <th>Subject</th>
            <th>Category</th>
            <th>Conf.</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          {list?.items.map((e) => (
            <tr key={e.id} onClick={() => setOpen(e.id)} className="clickable">
              <td>{fmtDate(e.received_at)}</td>
              <td className="ellipsis">{e.sender}</td>
              <td className="ellipsis">{e.subject}</td>
              <td>{e.classification ?? "—"}</td>
              <td>{pct(e.confidence)}</td>
              <td>
                <Badge tone={statusTone(e.status)}>{e.status}</Badge>{" "}
                {e.dry_run && e.actions.length > 0 && <Badge tone="dry">dry</Badge>}
              </td>
              <td>{e.actions.map((a) => a.action_type).join(", ") || "—"}</td>
            </tr>
          ))}
          {list && list.items.length === 0 && (
            <tr>
              <td colSpan={7} className="sub">
                No emails match.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="pager">
        <button disabled={page <= 1} onClick={() => setPage(page - 1)}>
          ‹ Prev
        </button>
        <span>
          Page {page} / {totalPages} ({list?.total ?? 0} emails)
        </span>
        <button disabled={page >= totalPages} onClick={() => setPage(page + 1)}>
          Next ›
        </button>
      </div>

      {open !== null && (
        <EmailDetail emailId={open} categories={categories} onClose={() => setOpen(null)} />
      )}
    </div>
  );
}
