import { useCallback, useEffect, useRef, useState } from "react";
import { Category, EmailAction, EmailList, EmailRow, StatusResponse, errMsg, get, post } from "../api";
import { AsyncButton, Badge, BulkActionBar, ConfirmDialog, LabelPill, Modal, ActionBadges, actionLabel, conf, fmtDate } from "../components";
import { useToast } from "../toast";

function statusTone(status: string): "ok" | "warn" | "error" | "neutral" | "info" {
  if (status === "actioned" || status === "classified") return "ok";
  if (status === "processing") return "info";
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
            toast.error(errMsg(e));
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

  // Auto-refresh while the email is being processed by the queue
  useEffect(() => {
    if (!email) return;
    if (email.status !== "pending" && email.status !== "processing") return;
    const id = setInterval(async () => {
      try {
        const updated = await get<EmailRow>(`/emails/${emailId}`);
        setEmail(updated);
        if (updated.status !== "pending" && updated.status !== "processing") {
          onChanged();
        }
      } catch { /* transient */ }
    }, 4000);
    return () => clearInterval(id);
  }, [emailId, email?.status, onChanged]);

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
        {email.summary && (
          <p className="rationale" style={{ whiteSpace: "pre-line" }}>
            <b>Summary:</b> {email.summary}
          </p>
        )}
        {email.error && <p className="error">Error: {email.error}</p>}

        <h4>{email.dry_run ? "Planned actions (dry-run)" : "Actions"}</h4>
        {(() => {
          const maxExecutedAt = email.actions
            .map((a) => a.executed_at)
            .filter(Boolean)
            .sort()
            .at(-1) ?? null;
          const displayActions = maxExecutedAt
            ? email.actions.filter((a) => !a.executed_at || a.executed_at === maxExecutedAt)
            : email.actions;
          if (displayActions.length === 0) return <p className="sub">No actions.</p>;

          // Labels keep badge styling; every other action is plain text. A single
          // shared status + date/time describes the whole group.
          const isLabel = (a: EmailAction) =>
            a.action_type === "add_label" && Boolean(a.action_params?.label_name);
          const labelActions = displayActions.filter(isLabel);
          const otherActions = displayActions.filter((a) => !isLabel(a));
          const errors = displayActions.filter((a) => a.error);
          const status = displayActions.some((a) => a.executed)
            ? `executed · ${fmtDate(maxExecutedAt)}`
            : displayActions.some((a) => a.dry_run)
              ? "planned (dry-run)"
              : "not executed";
          return (
            <>
              <div className="action-summary">
                {labelActions.map((a) => (
                  <LabelPill
                    key={a.id}
                    name={String(a.action_params?.label_name)}
                    textColor={a.action_params?.text_color as string | null}
                    backgroundColor={a.action_params?.background_color as string | null}
                  />
                ))}
                {otherActions.length > 0 && (
                  <span>{otherActions.map((a) => actionLabel(a.action_type)).join(" · ")}</span>
                )}
                <span className="sub action-status">{status}</span>
              </div>
              {errors.map((a) => (
                <p key={a.id} className="error">
                  {actionLabel(a.action_type)} failed: {a.error}
                </p>
              ))}
            </>
          );
        })()}

        <AsyncButton
          className="primary"
          onClick={async () => {
            try {
              const updated = await post<EmailRow>(`/emails/${email.id}/reclassify`);
              setEmail(updated);
              toast.success("Queued for reclassification — watch for status updates");
              onChanged();
            } catch (e) {
              toast.error(errMsg(e));
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
    period_hours: "",
    q: "",
  });
  const [periodCustom, setPeriodCustom] = useState(false);
  const hasActiveFilters =
    filters.category_id !== "" || filters.status !== "" ||
    filters.period_hours !== "" || filters.q !== "";
  const clearFilters = () => {
    setPage(1);
    setPeriodCustom(false);
    setFilters({ category_id: "", status: "", period_hours: "", q: "" });
  };
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [allMatchingSelected, setAllMatchingSelected] = useState(false);
  const [bulkConfirm, setBulkConfirm] = useState<"reclassify" | null>(null);

  // Single source of truth for the list filters, shared by the paginated list
  // and select-all-across-pages so the two can never select different sets.
  const filterParams = useCallback(() => {
    const params = new URLSearchParams();
    if (filters.category_id) params.set("category_id", filters.category_id);
    if (filters.status) params.set("status", filters.status);
    if (filters.period_hours) params.set("received_within_hours", filters.period_hours);
    if (filters.q) params.set("q", filters.q);
    return params;
  }, [filters]);

  const load = useCallback(async () => {
    const params = filterParams();
    params.set("page", String(page));
    setList(await get<EmailList>(`/emails?${params.toString()}`));
  }, [page, filterParams]);

  useEffect(() => {
    load();
    setSelectedIds(new Set());
    setAllMatchingSelected(false);
  }, [load]);
  useEffect(() => {
    get<Category[]>("/categories").then(setCategories);
  }, []);

  // Live refresh while classification runs: poll /status cheaply; reload the
  // list on every tick while running, plus once more when it finishes.
  const [classifying, setClassifying] = useState(false);
  const [pending, setPending] = useState(0);
  const wasRunning = useRef(false);
  const lastPending = useRef<number | null>(null);
  useEffect(() => {
    const tick = async () => {
      try {
        const st = await get<StatusResponse>("/status");
        const running = st.classifier.running || st.classifier.pending_emails > 0;
        setClassifying(running);
        setPending(st.classifier.pending_emails);
        // Reload the (full) list only when progress actually happened — the
        // pending count moved — or once when work just finished, instead of on
        // every 4s tick for the whole duration.
        const changed = lastPending.current !== st.classifier.pending_emails;
        if ((running && changed) || (!running && wasRunning.current)) await load();
        lastPending.current = st.classifier.pending_emails;
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
  const hasMore = list != null && list.total > pageIds.length;

  const selectAllRef = useRef<HTMLInputElement>(null);
  const mobileSelectRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const val = someChecked && !allChecked;
    if (selectAllRef.current) selectAllRef.current.indeterminate = val;
    if (mobileSelectRef.current) mobileSelectRef.current.indeterminate = val;
  }, [someChecked, allChecked]);

  const toggleSelect = (id: number) =>
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const selectAll = () => { setSelectedIds(new Set(pageIds)); setAllMatchingSelected(false); };
  const clearSelection = () => { setSelectedIds(new Set()); setAllMatchingSelected(false); };

  const selectAllMatching = async () => {
    try {
      const params = filterParams();
      const result = await get<{ ids: number[] }>(`/emails/ids?${params.toString()}`);
      setSelectedIds(new Set(result.ids));
      setAllMatchingSelected(true);
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const doBulkReclassify = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await post<{ queued: number }>(
        "/emails/reclassify-bulk",
        { email_ids: ids },
      );
      toast.success(`Re-classifying ${r.queued} emails in the background…`);
      clearSelection();
      await load();
    } catch (e) {
      toast.error(errMsg(e));
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
      toast.error(errMsg(e));
    }
  };

  return (
    <div>
      <header className="page-head">
        <h2>Emails</h2>
        {classifying ? (
          <Badge tone="info">Classifying… · {pending} pending</Badge>
        ) : (
          <Badge tone="neutral">Classifier: idle</Badge>
        )}
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
        <select
          value={periodCustom ? "custom" : filters.period_hours}
          onChange={(e) => {
            const v = e.target.value;
            setPage(1);
            if (v === "custom") {
              setPeriodCustom(true);
            } else {
              setPeriodCustom(false);
              setFilters({ ...filters, period_hours: v });
            }
          }}
        >
          <option value="">Any time</option>
          <option value="1">Past 1h</option>
          <option value="4">Past 4h</option>
          <option value="8">Past 8h</option>
          <option value="24">Past 24h</option>
          <option value="custom">Custom…</option>
        </select>
        {periodCustom && (
          <input
            type="number"
            min="1"
            step="1"
            placeholder="hours"
            value={filters.period_hours}
            onChange={(e) => {
              setPage(1);
              setFilters({ ...filters, period_hours: e.target.value });
            }}
          />
        )}
        <button
          className="clear-filters"
          onClick={clearFilters}
          disabled={!hasActiveFilters}
        >
          Clear filters
        </button>
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

      <div className={`select-banner${(allChecked && hasMore) || allMatchingSelected ? " has-offer" : ""}`}>
        {/* Mobile-only: select-page checkbox (thead is hidden on mobile) */}
        <label className="select-page-mobile">
          <input
            type="checkbox"
            ref={mobileSelectRef}
            checked={allChecked}
            onChange={() => (allChecked ? clearSelection() : selectAll())}
          />
          Select page
        </label>
        {allChecked && hasMore && !allMatchingSelected && (
          <span className="select-all-offer">
            All {pageIds.length} on this page selected.{" "}
            <button className="link-btn" onClick={selectAllMatching}>
              Select all {list!.total} emails
            </button>
          </span>
        )}
        {allMatchingSelected && (
          <span className="select-all-offer">
            All {selectedIds.size} emails selected.
          </span>
        )}
      </div>

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
              <td data-label="Status">
                <Badge tone={statusTone(e.status)}>{e.status}</Badge>{" "}
                {e.dry_run && e.actions.length > 0 && <Badge tone="dry">dry</Badge>}
              </td>
              <td data-label="Actions"><ActionBadges actions={e.actions} /></td>
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
