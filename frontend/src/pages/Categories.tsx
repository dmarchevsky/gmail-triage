import { useCallback, useEffect, useState } from "react";
import { Category, CriteriaVersion, del, get, post, put } from "../api";
import { Badge, ConfirmDialog, DiffView, Modal, fmtDate } from "../components";
import { useToast } from "../toast";

function CategoryEditor({
  category,
  onSaved,
  onClose,
}: {
  category: Category | null;
  onSaved: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState({
    name: category?.name ?? "",
    description: category?.description ?? "",
    gmail_label_name: category?.gmail_label_name ?? "",
    criteria_md: category?.criteria_md ?? "",
    enabled: category?.enabled ?? true,
  });
  const save = async () => {
    try {
      if (category) await put(`/categories/${category.id}`, form);
      else await post("/categories", form);
      toast.success(category ? "Category updated" : "Category created");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Modal title={category ? `Edit ${category.name}` : "New category"} onClose={onClose} wide>
      <div className="form-grid">
        <label>
          Name
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
          />
        </label>
        <label>
          Gmail label
          <input
            placeholder={`MailTriage/${form.name || "…"}`}
            value={form.gmail_label_name}
            onChange={(e) => setForm({ ...form, gmail_label_name: e.target.value })}
          />
        </label>
        <label className="span2">
          Description
          <input
            value={form.description ?? ""}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
          />
        </label>
        <label className="span2">
          Classification criteria (markdown — this text <i>is</i> the LLM prompt)
          <textarea
            rows={10}
            value={form.criteria_md}
            onChange={(e) => setForm({ ...form, criteria_md: e.target.value })}
            placeholder={
              "Describe in plain language what belongs in this category.\n" +
              "E.g.: Daily/weekly market commentary newsletters; stock, bond,\n" +
              "macro analysis; NOT individual trade confirmations."
            }
          />
        </label>
        <label className="checkbox span2">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
          Enabled (included in classification prompt)
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

function HistoryViewer({
  category,
  onRestored,
  onClose,
}: {
  category: Category;
  onRestored: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [history, setHistory] = useState<CriteriaVersion[]>([]);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    get<CriteriaVersion[]>(`/categories/${category.id}/criteria-history`).then(setHistory);
  }, [category.id]);

  const selectedVersion = history.find((h) => h.version === selected);
  const current = history[0];

  return (
    <Modal title={`Criteria history — ${category.name}`} onClose={onClose} wide>
      <div className="history-layout">
        <ul className="history-list">
          {history.map((h) => (
            <li key={h.version}>
              <button
                className={selected === h.version ? "selected" : ""}
                onClick={() => setSelected(h.version)}
              >
                v{h.version} · {h.source} · {fmtDate(h.created_at)}
                {h.version === category.criteria_version && <Badge tone="ok">current</Badge>}
              </button>
            </li>
          ))}
        </ul>
        <div className="history-detail">
          {selectedVersion ? (
            selectedVersion.version === category.criteria_version ? (
              <pre className="criteria-text">{selectedVersion.criteria_md}</pre>
            ) : (
              <>
                <p className="sub">
                  Diff: v{selectedVersion.version} → current (v{current?.version})
                </p>
                <DiffView
                  oldText={selectedVersion.criteria_md}
                  newText={current?.criteria_md ?? ""}
                />
                <button
                  className="primary"
                  onClick={async () => {
                    try {
                      await put(`/categories/${category.id}`, {
                        name: category.name,
                        description: category.description,
                        gmail_label_name: category.gmail_label_name,
                        criteria_md: selectedVersion.criteria_md,
                        enabled: category.enabled,
                      });
                      toast.success(`Restored v${selectedVersion.version} as a new version`);
                      onRestored();
                      onClose();
                    } catch (e) {
                      toast.error(e instanceof Error ? e.message : String(e));
                    }
                  }}
                >
                  Restore v{selectedVersion.version} (creates a new version)
                </button>
              </>
            )
          ) : (
            <p className="sub">Select a version to view/diff.</p>
          )}
        </div>
      </div>
    </Modal>
  );
}

export default function Categories() {
  const [categories, setCategories] = useState<Category[]>([]);
  const [editing, setEditing] = useState<Category | null | "new">(null);
  const [history, setHistory] = useState<Category | null>(null);
  const [deleting, setDeleting] = useState<Category | null>(null);

  const load = useCallback(() => get<Category[]>("/categories").then(setCategories), []);
  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <header className="page-head">
        <h2>Categories</h2>
        <button className="primary" onClick={() => setEditing("new")}>
          + New category
        </button>
      </header>
      <p className="sub">
        Each category's criteria text is fed directly to the LLM as classification
        instructions. Confidence values are self-reported by the model and not
        calibrated — use the per-category precision stats (Dashboard, after feedback
        exists) to tune rule confidence thresholds empirically.
      </p>

      <table className="table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Gmail label</th>
            <th>Criteria (start)</th>
            <th>Version</th>
            <th>Enabled</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {categories.map((c) => (
            <tr key={c.id}>
              <td>
                <b>{c.name}</b>
                {c.description && <div className="sub">{c.description}</div>}
              </td>
              <td>
                <code>{c.gmail_label_name}</code>
              </td>
              <td className="ellipsis criteria-preview">{c.criteria_md.slice(0, 80)}</td>
              <td>v{c.criteria_version}</td>
              <td>{c.enabled ? <Badge tone="ok">on</Badge> : <Badge>off</Badge>}</td>
              <td className="row-actions">
                <button onClick={() => setEditing(c)}>Edit</button>
                <button onClick={() => setHistory(c)}>History</button>
                <button className="danger" onClick={() => setDeleting(c)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {categories.length === 0 && (
            <tr>
              <td colSpan={6} className="sub">
                No categories yet — create one to start classifying.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {editing !== null && (
        <CategoryEditor
          category={editing === "new" ? null : editing}
          onSaved={load}
          onClose={() => setEditing(null)}
        />
      )}
      {history && (
        <HistoryViewer category={history} onRestored={load} onClose={() => setHistory(null)} />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete category “${deleting.name}”?`}
          danger
          confirmLabel="Delete"
          message={
            <p>
              Emails keep their history but lose this classification reference.
              Rules matching this category will stop matching. The Gmail label itself
              is <b>not</b> deleted.
            </p>
          }
          onConfirm={async () => {
            await del(`/categories/${deleting.id}`);
            setDeleting(null);
            load();
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
