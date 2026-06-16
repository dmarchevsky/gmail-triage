import { useCallback, useEffect, useState } from "react";
import { Category, Digest, DigestRun, del, delWithBody, errMsg, get, post, put } from "../api";
import { useSelection } from "../useSelection";
import {
  AsyncButton,
  Badge,
  BulkActionBar,
  ConfirmDialog,
  Modal,
  fmtDate,
  pct,
} from "../components";
import { useToast } from "../toast";

interface DigestForm {
  name: string;
  enabled: boolean;
  category_ids: number[];
  cron_times: string;
  timezone: string;
  min_confidence: number;
  telegram_chat_id: string;
  include_links: boolean;
  include_metadata: boolean;
  max_emails: number;
  send_no_news: boolean;
}

function DigestEditor({
  digest,
  categories,
  onSaved,
  onClose,
}: {
  digest: Digest | null;
  categories: Category[];
  onSaved: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState<DigestForm>({
    name: digest?.name ?? "",
    enabled: digest?.enabled ?? true,
    category_ids: digest?.category_ids ?? [],
    cron_times: (digest?.cron_times ?? ["07:00"]).join(", "),
    timezone:
      digest?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? "UTC",
    min_confidence: digest?.min_confidence ?? 0.8,
    telegram_chat_id: digest?.telegram_chat_id ?? "",
    include_links: digest?.include_links ?? true,
    include_metadata: digest?.include_metadata ?? true,
    max_emails: digest?.max_emails ?? 50,
    send_no_news: digest?.send_no_news ?? false,
  });
  const toggleCategory = (id: number) =>
    setForm({
      ...form,
      category_ids: form.category_ids.includes(id)
        ? form.category_ids.filter((c) => c !== id)
        : [...form.category_ids, id],
    });

  const save = async () => {
    const body = {
      ...form,
      telegram_chat_id: form.telegram_chat_id || null,
      cron_times: form.cron_times
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    };
    try {
      if (digest) await put(`/digests/${digest.id}`, body);
      else await post("/digests", body);
      toast.success(digest ? "Digest updated" : "Digest created");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  return (
    <Modal title={digest ? `Edit digest: ${digest.name}` : "New digest"} onClose={onClose} wide>
      <div className="form-grid">
        <label>
          Name
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>
          Times of day (HH:MM, comma-separated)
          <input
            placeholder="07:00, 16:00"
            value={form.cron_times}
            onChange={(e) => setForm({ ...form, cron_times: e.target.value })}
          />
        </label>
        <label>
          Min confidence ({pct(form.min_confidence)})
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={form.min_confidence}
            onChange={(e) => setForm({ ...form, min_confidence: Number(e.target.value) })}
          />
        </label>
        <div className="span2">
          <p className="field-label">Categories</p>
          <div className="head-actions">
            {categories.map((c) => (
              <label key={c.id} className="checkbox">
                <input
                  type="checkbox"
                  checked={form.category_ids.includes(c.id)}
                  onChange={() => toggleCategory(c.id)}
                />
                {c.name}
              </label>
            ))}
            {categories.length === 0 && (
              <span className="sub">No categories defined yet.</span>
            )}
          </div>
        </div>
        <label>
          Telegram chat id (empty = default from Settings)
          <input
            value={form.telegram_chat_id}
            onChange={(e) => setForm({ ...form, telegram_chat_id: e.target.value })}
          />
        </label>
        <label>
          Max emails per digest
          <input
            type="number"
            min="1"
            max="500"
            value={form.max_emails}
            onChange={(e) => setForm({ ...form, max_emails: Number(e.target.value) })}
          />
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.include_metadata}
            onChange={(e) => setForm({ ...form, include_metadata: e.target.checked })}
          />
          Include sender/subject/time list
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.include_links}
            onChange={(e) => setForm({ ...form, include_links: e.target.checked })}
          />
          Include Gmail deep links
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.send_no_news}
            onChange={(e) => setForm({ ...form, send_no_news: e.target.checked })}
          />
          Send a "no news" message when empty (default: skip silently)
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
          Enabled (scheduled)
        </label>
      </div>
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save}>
          Save
        </button>
      </div>
    </Modal>
  );
}

function RunHistory({ digest, onClose }: { digest: Digest; onClose: () => void }) {
  const [runs, setRuns] = useState<DigestRun[]>([]);
  useEffect(() => {
    get<DigestRun[]>(`/digests/${digest.id}/runs`).then(setRuns);
  }, [digest.id]);

  const tone = (s: string) =>
    s === "success" ? "ok" : s === "error" ? "error" : s === "dry_run" ? "dry" : "neutral";

  return (
    <Modal title={`Runs — ${digest.name}`} onClose={onClose} wide>
      {runs.length === 0 && <p className="sub">No runs yet.</p>}
      {runs.map((r) => (
        <div key={r.id} className="settings-section">
          <p>
            {fmtDate(r.started_at)} <Badge tone={tone(r.status)}>{r.status}</Badge>{" "}
            <span className="sub">{r.email_ids.length} email(s)</span>
          </p>
          {r.error && <p className="error">{r.error}</p>}
          {r.summary_text && <div className="digest-summary">{r.summary_text}</div>}
        </div>
      ))}
    </Modal>
  );
}

export default function Digests() {
  const [digests, setDigests] = useState<Digest[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [editing, setEditing] = useState<Digest | null | "new">(null);
  const [history, setHistory] = useState<Digest | null>(null);
  const [deleting, setDeleting] = useState<Digest | null>(null);
  const [bulkConfirm, setBulkConfirm] = useState<"delete" | "send" | null>(null);
  const toast = useToast();

  const load = useCallback(() => get<Digest[]>("/digests").then(setDigests), []);
  useEffect(() => {
    load();
    get<Category[]>("/categories").then(setCategories);
    const id = setInterval(load, 10000); // keep run status live
    return () => clearInterval(id);
  }, [load]);

  const runTone = (s: string) =>
    s === "running"
      ? "info"
      : s === "success"
        ? "ok"
        : s === "error"
          ? "error"
          : s === "dry_run"
            ? "dry"
            : "neutral";

  const catNames = (ids: number[]) =>
    ids.map((id) => categories.find((c) => c.id === id)?.name ?? `#${id}`).join(", ");

  const {
    selectedIds, allChecked, selectAllRef,
    toggle: toggleSelect, selectAll, clear: clearSelection,
  } = useSelection(digests, (d) => d.id);

  const doBulkEnable = async (enabled: boolean) => {
    const ids = Array.from(selectedIds);
    try {
      const r = await put<{ updated: number }>("/digests/bulk", {
        digest_ids: ids,
        enabled,
      });
      toast.success(`${r.updated} digest${r.updated === 1 ? "" : "s"} ${enabled ? "enabled" : "disabled"}`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const doBulkDelete = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await delWithBody<{ deleted: number }>("/digests/bulk", {
        digest_ids: ids,
      });
      toast.success(`${r.deleted} digest${r.deleted === 1 ? "" : "s"} deleted`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  const doBulkSend = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await post<{ sent: number; errors: { digest_id: number; error: string }[] }>(
        "/digests/bulk-send",
        { digest_ids: ids },
      );
      if (r.errors.length > 0)
        toast.error(`Sent ${r.sent}; ${r.errors.length} failed — check individual digests`);
      else toast.success(`${r.sent} digest${r.sent === 1 ? "" : "s"} sent`);
      clearSelection();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  return (
    <div>
      <header className="page-head">
        <h2>Digests</h2>
        <button className="primary" onClick={() => setEditing("new")}>
          + New digest
        </button>
      </header>

      <BulkActionBar
        count={selectedIds.size}
        onClear={clearSelection}
        actions={[
          { label: "Enable", onClick: async () => doBulkEnable(true) },
          { label: "Disable", onClick: async () => doBulkEnable(false) },
          { label: "Send now", onClick: async () => setBulkConfirm("send") },
          { label: "Delete", danger: true, onClick: async () => setBulkConfirm("delete") },
        ]}
      />

      <div className="table-scroll wide">
      <table className="table digests-table">
        <thead>
          <tr>
            <th>
              <input
                type="checkbox"
                ref={selectAllRef}
                checked={allChecked}
                onChange={() => (allChecked ? clearSelection() : selectAll())}
              />
            </th>
            <th>Name</th>
            <th>Schedule</th>
            <th>Categories</th>
            <th>Min conf.</th>
            <th>Enabled</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {digests.map((d) => (
            <tr key={d.id}>
              <td>
                <input
                  type="checkbox"
                  checked={selectedIds.has(d.id)}
                  onChange={() => toggleSelect(d.id)}
                />
              </td>
              <td data-label="Name">
                <b>{d.name}</b>
              </td>
              <td data-label="Schedule">
                {d.cron_times.join(", ")} <span className="sub">{d.timezone}</span>
              </td>
              <td data-label="Categories">{catNames(d.category_ids) || "—"}</td>
              <td data-label="Min conf.">{pct(d.min_confidence)}</td>
              <td data-label="Enabled">{d.enabled ? <Badge tone="ok">on</Badge> : <Badge>off</Badge>}</td>
              <td data-label="Status">
                {d.last_run ? (
                  <Badge tone={runTone(d.last_run.status)}>{d.last_run.status}</Badge>
                ) : (
                  <span className="sub">—</span>
                )}
              </td>
              <td className="row-actions">
                <AsyncButton
                  disabled={d.last_run?.status === "running"}
                  onClick={async () => {
                    const r = await post<DigestRun>(`/digests/${d.id}/run-now`, {
                      preview: true,
                    });
                    load();
                    toast.success(
                      r.status === "running"
                        ? "Already running — see Runs"
                        : r.status === "empty"
                          ? "No eligible emails."
                          : `Preview rendered (${r.email_ids.length} emails) — see Runs`,
                    );
                  }}
                >
                  Preview
                </AsyncButton>
                <AsyncButton
                  disabled={d.last_run?.status === "running"}
                  onClick={async () => {
                    const r = await post<DigestRun>(`/digests/${d.id}/run-now`, {});
                    load();
                    const message =
                      r.status === "running"
                        ? "Already running — see Runs"
                        : r.status === "empty"
                          ? "No eligible emails."
                          : r.status === "success"
                            ? `Sent (${r.email_ids.length} emails)`
                            : `Run ${r.status}: ${r.error ?? ""}`;
                    if (r.status === "error") toast.error(message);
                    else toast.success(message);
                  }}
                >
                  Send now
                </AsyncButton>
                <button onClick={() => setHistory(d)}>Runs</button>
                <button onClick={() => setEditing(d)}>Edit</button>
                <button className="danger" onClick={() => setDeleting(d)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {digests.length === 0 && (
            <tr>
              <td colSpan={8} className="sub">
                No digests. Example: "Market news — 07:00 &amp; 16:00 — category MarketNews
                — min confidence 0.8."
              </td>
            </tr>
          )}
        </tbody>
      </table>
      </div>

      {editing !== null && (
        <DigestEditor
          digest={editing === "new" ? null : editing}
          categories={categories}
          onSaved={load}
          onClose={() => setEditing(null)}
        />
      )}
      {history && <RunHistory digest={history} onClose={() => setHistory(null)} />}
      {deleting && (
        <ConfirmDialog
          title={`Delete digest "${deleting.name}"?`}
          danger
          confirmLabel="Delete"
          message={<p>Run history is removed as well.</p>}
          onConfirm={async () => {
            await del(`/digests/${deleting.id}`);
            setDeleting(null);
            load();
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
      {bulkConfirm === "send" && (
        <ConfirmDialog
          title={`Send ${selectedIds.size} digest${selectedIds.size === 1 ? "" : "s"} now?`}
          confirmLabel="Send now"
          message={
            <p>
              This will immediately send <b>{selectedIds.size}</b> digest
              {selectedIds.size === 1 ? "" : "s"} via Telegram.
            </p>
          }
          onConfirm={async () => {
            setBulkConfirm(null);
            await doBulkSend();
          }}
          onCancel={() => setBulkConfirm(null)}
        />
      )}
      {bulkConfirm === "delete" && (
        <ConfirmDialog
          title={`Delete ${selectedIds.size} digest${selectedIds.size === 1 ? "" : "s"}?`}
          danger
          confirmLabel="Delete all"
          message={<p>Run history is removed as well.</p>}
          onConfirm={async () => {
            setBulkConfirm(null);
            await doBulkDelete();
          }}
          onCancel={() => setBulkConfirm(null)}
        />
      )}
    </div>
  );
}
