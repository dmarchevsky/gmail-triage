import { useCallback, useEffect, useRef, useState } from "react";
import { Category, EmailList, EmailRow, StatusResponse, get, post } from "../api";
import { AsyncButton, Badge, BulkActionBar, ConfirmDialog, Modal, actionLabel, conf, fmtDate } from "../components";
import { useToast } from "../toast";

function statusTone(status: string): "ok" | "warn" | "error" | "neutral" {
  if (status === "actioned" || status === "classified") return "ok";
  if (status === "error") return "error";
  if (status === "pending") return "warn";
  return "neutral";
}

function reclassifySummary(email: EmailRow): string {
  const category = email.classification ?? "none";
  if (email.actions.length === 0)
    return `Re-classified as ${category} — no rule matched`;
  const planned = email.actions.some((a) => a.dry_run && !a.executed);
  const labels = email.actions.map((a) => actionLabel(a.action_type)).join(", ");
  return `Re-classified as ${category} — ${planned ? "planned" : "executed"}: ${labels}`;
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
  const toast = useToast();
  const [categoryId, setCategoryId] = useState<string>("none");
  const [note, setNote] = useState("");

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
      <button
        className="primary"
        onClick={async () => {
          try {
            await post(`/emails/${email.id}/feedback`, {
              correct_category_id: categoryId === "none" ? null : Number(categoryId),
              user_note: note || null,
            });
            toast.success("Feedback recorded — see the Feedback page for proposals");
            onDone();
          } catch (e) {
            toast.error(e instanceof Error ? e.message : String(e));
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
  onChanged,
  onClose,
}: {
  emailId: number;
  categories: Category[];
  onChanged: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
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
          {conf(email.confidence)} confidence
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
              <code>{actionLabel(a.action_type)}</code>{" "}
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

        <AsyncButton
          onClick={async () => {
            try {
              const updated = await post<EmailRow>(`/emails/${email.id}/reclassify`);
              setEmail(updated);
              toast.success(reclassifySummary(updated));
              onChanged();
            } catch (e) {
              toast.error(e instanceof Error ? e.message : String(e));
            }
          }}
        >
          ↻ Re-run classification &amp; rules
        </AsyncButton>

        {!feedbackSent && (
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
  const toast = useToast();
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
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState<"reclassify" | null>(null);

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
    setSelectedIds(new Set());
  }, [load]);
  useEffect(() => {
    get<Category[]>("/categories").then(setCategories);
  }, []);

  // Live refresh while classification runs: poll /status cheaply; reload the
  // list on every tick while running, plus once more when it finishes.
  const [classifying, setClassifying] = useState(false);
  const wasRunning = useRef(false);
  useEffect(() => {
    const tick = async () => {
      try {
        const st = await get<StatusResponse>("/status");
        const running = st.classifier.running;
        setClassifying(running);
        if (running || wasRunning.current) await load();
        wasRunning.current = running;
      } catch {
        /* transient */
      }
    };
    const id = setInterval(tick, 4000);
    return () => clearInterval(id);
  }, [load]);

  const totalPages = list ? Math.max(1, Math.ceil(list.total / list.page_size)) : 1;
  const pageIds = list?.items.map((e) => e.id) ?? [];
  const allChecked = pageIds.length > 0 && pageIds.every((id) => selectedIds.has(id));
  const someChecked = pageIds.some((id) => selectedIds.has(id));

  const selectAllRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (selectAllRef.current)
      selectAllRef.current.indeterminate = someChecked && !allChecked;
  }, [someChecked, allChecked]);

  const toggleSelect = (id: number) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const selectAll = () => setSelectedIds(new Set(pageIds));
  const clearSelection = () => setSelectedIds(new Set());

  const doBulkReclassify = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await post<{ queued: number; classified: number; skipped: number; errors: number }>(
        "/emails/reclassify-bulk",
        { email_ids: ids },
      );
      toast.success(
        `Queued ${r.queued} emails for re-classification (${r.classified} done, ${r.errors} errors)`,
      );
      clearSelection();
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const doBulkRerunRules = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await post<{ processed: number; actioned: number; errors: number }>(
        "/emails/rerun-rules-bulk",
        { email_ids: ids },
      );
      toast.success(`Rules re-applied: ${r.processed} processed, ${r.actioned} actioned`);
      clearSelection();
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div>
      <header className="page-head">
        <h2>Emails</h2>
        {classifying && <Badge tone="warn">classifying — list updates live</Badge>}
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

      <BulkActionBar
        count={selectedIds.size}
        onClear={clearSelection}
        actions={[
          {
            label: "Re-classify (LLM + rules)",
            onClick: async () => setBulkConfirm("reclassify"),
          },
          {
            label: "Re-run rules only",
            onClick: doBulkRerunRules,
          },
        ]}
      />

      <div className="emails-scroll">
      <table className="table emails-table">
        <thead>
          <tr>
            <th className="col-check">
              <input
                type="checkbox"
                ref={selectAllRef}
                checked={allChecked}
                onChange={() => (allChecked ? clearSelection() : selectAll())}
              />
            </th>
            <th className="col-date">Date</th>
            <th className="col-sender">Sender</th>
            <th>Subject</th>
            <th className="col-cat">Category</th>
            <th className="col-conf">Conf.</th>
            <th className="col-status">Status</th>
            <th className="col-acts">Actions</th>
          </tr>
        </thead>
        <tbody>
          {list?.items.map((e) => (
            <tr key={e.id} onClick={() => setOpen(e.id)} className="clickable">
              <td onClick={(ev) => ev.stopPropagation()}>
                <input
                  type="checkbox"
                  checked={selectedIds.has(e.id)}
                  onChange={() => toggleSelect(e.id)}
                />
              </td>
              <td data-label="Date">{fmtDate(e.received_at)}</td>
              <td data-label="Sender" className="ellipsis">{e.sender}</td>
              <td data-label="Subject" className="ellipsis">{e.subject}</td>
              <td data-label="Category">{e.classification ?? "—"}</td>
              <td data-label="Conf.">{conf(e.confidence)}</td>
              <td data-label="Status">
                <Badge tone={statusTone(e.status)}>{e.status}</Badge>{" "}
                {e.dry_run && e.actions.length > 0 && <Badge tone="dry">dry</Badge>}
              </td>
              <td data-label="Actions">{e.actions.map((a) => actionLabel(a.action_type)).join(", ") || "—"}</td>
            </tr>
          ))}
          {list && list.items.length === 0 && (
            <tr>
              <td colSpan={8} className="sub">
                No emails match.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      </div>

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
        <EmailDetail
          emailId={open}
          categories={categories}
          onChanged={load}
          onClose={() => setOpen(null)}
        />
      )}

      {bulkConfirm === "reclassify" && (
        <ConfirmDialog
          title={`Re-classify ${selectedIds.size} email(s)?`}
          confirmLabel="Re-classify"
          message={
            <p>
              This will re-run the LLM on <b>{selectedIds.size}</b> email(s) and may
              incur costs. Existing dry-run actions will be discarded.
            </p>
          }
          onConfirm={async () => {
            setBulkConfirm(null);
            await doBulkReclassify();
          }}
          onCancel={() => setBulkConfirm(null)}
        />
      )}
    </div>
  );
}
