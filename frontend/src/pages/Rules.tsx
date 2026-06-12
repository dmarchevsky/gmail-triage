import { useCallback, useEffect, useState } from "react";
import { Category, Rule, RuleAction, del, get, post, put } from "../api";
import { Badge, ConfirmDialog, Modal, actionLabel, pct } from "../components";
import { useToast } from "../toast";

const ACTION_TYPES: RuleAction["type"][] = [
  "add_label",
  "remove_label",
  "mark_read",
  "archive",
  "trash",
];

function ActionBuilder({
  actions,
  categories,
  onChange,
}: {
  actions: RuleAction[];
  categories: Category[];
  onChange: (a: RuleAction[]) => void;
}) {
  const update = (i: number, patch: Partial<RuleAction>) => {
    const next = actions.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  return (
    <div className="action-builder">
      {actions.map((a, i) => (
        <div key={i} className="action-row">
          <select
            value={a.type}
            onChange={(e) => {
              const type = e.target.value as RuleAction["type"];
              const next: RuleAction = { type };
              onChange(actions.map((x, j) => (j === i ? next : x)));
            }}
          >
            {ACTION_TYPES.map((t) => (
              <option key={t} value={t}>
                {actionLabel(t)}
              </option>
            ))}
          </select>
          {a.type === "add_label" && (
            <select
              value={a.category_id ? `cat:${a.category_id}` : a.label_name ? "custom" : ""}
              onChange={(e) => {
                const v = e.target.value;
                if (v.startsWith("cat:"))
                  update(i, { category_id: Number(v.slice(4)), label_name: undefined });
                else update(i, { category_id: undefined, label_name: a.label_name ?? "" });
              }}
            >
              <option value="">label…</option>
              {categories.map((c) => (
                <option key={c.id} value={`cat:${c.id}`}>
                  {c.gmail_label_name} (category)
                </option>
              ))}
              <option value="custom">custom label…</option>
            </select>
          )}
          {((a.type === "add_label" && a.category_id === undefined) ||
            a.type === "remove_label") && (
            <input
              placeholder="Label name"
              value={a.label_name ?? ""}
              onChange={(e) => update(i, { label_name: e.target.value })}
            />
          )}
          <button
            className="icon-btn"
            onClick={() => onChange(actions.filter((_, j) => j !== i))}
            aria-label="Remove action"
          >
            ✕
          </button>
        </div>
      ))}
      <button onClick={() => onChange([...actions, { type: "mark_read" }])}>+ Add action</button>
    </div>
  );
}

function RuleEditor({
  rule,
  categories,
  onSaved,
  onClose,
}: {
  rule: Rule | null;
  categories: Category[];
  onSaved: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState({
    name: rule?.name ?? "",
    enabled: rule?.enabled ?? true,
    priority: rule?.priority ?? 100,
    match_category_id: rule?.match_category_id ?? null,
    match_min_confidence: rule?.match_min_confidence ?? 0.8,
    match_sender_pattern: rule?.match_sender_pattern ?? "",
    actions: (rule?.actions ?? []) as RuleAction[],
    stop_processing: rule?.stop_processing ?? true,
  });
  const [confirmTrash, setConfirmTrash] = useState(false);

  const doSave = async () => {
    const body = {
      ...form,
      match_sender_pattern: form.match_sender_pattern || null,
    };
    try {
      if (rule) await put(`/rules/${rule.id}`, body);
      else await post("/rules", body);
      toast.success(rule ? "Rule updated" : "Rule created");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const save = () => {
    if (form.actions.some((a) => a.type === "trash")) setConfirmTrash(true);
    else doSave();
  };

  return (
    <Modal title={rule ? `Edit rule: ${rule.name}` : "New rule"} onClose={onClose} wide>
      <div className="form-grid">
        <label>
          Name
          <input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
        </label>
        <label>
          Priority (lower runs first)
          <input
            type="number"
            value={form.priority}
            onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })}
          />
        </label>
        <label>
          Match category
          <select
            value={form.match_category_id ?? ""}
            onChange={(e) =>
              setForm({
                ...form,
                match_category_id: e.target.value === "" ? null : Number(e.target.value),
              })
            }
          >
            <option value="">any / none (sender-only rule)</option>
            {categories.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          Min confidence ({pct(form.match_min_confidence)})
          <input
            type="range"
            min="0"
            max="1"
            step="0.05"
            value={form.match_min_confidence}
            onChange={(e) =>
              setForm({ ...form, match_min_confidence: Number(e.target.value) })
            }
          />
        </label>
        <label className="span2">
          Sender pattern (optional glob or regex; with no category = hard rule that
          bypasses the LLM)
          <input
            placeholder="*@newsletter.example.com"
            value={form.match_sender_pattern}
            onChange={(e) => setForm({ ...form, match_sender_pattern: e.target.value })}
          />
        </label>
        <div className="span2">
          <p className="field-label">Actions</p>
          <ActionBuilder
            actions={form.actions}
            categories={categories}
            onChange={(actions) => setForm({ ...form, actions })}
          />
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.stop_processing}
            onChange={(e) => setForm({ ...form, stop_processing: e.target.checked })}
          />
          Stop processing further rules on match
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
          />
          Enabled
        </label>
      </div>
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save}>
          Save
        </button>
      </div>
      {confirmTrash && (
        <ConfirmDialog
          title="This rule moves email to Trash"
          danger
          confirmLabel="Save rule"
          message={
            <p>
              The <code>trash</code> action moves matching messages to Gmail's Trash
              (auto-purged by Gmail after 30 days). Nothing is permanently deleted by
              MailTriage. Continue?
            </p>
          }
          onConfirm={() => {
            setConfirmTrash(false);
            doSave();
          }}
          onCancel={() => setConfirmTrash(false)}
        />
      )}
    </Modal>
  );
}

function TestResults({ rule, onClose }: { rule: Rule; onClose: () => void }) {
  const [result, setResult] = useState<{
    tested: number;
    matched: number;
    matches: { email_id: number; subject: string; sender: string; confidence: number }[];
  } | null>(null);

  useEffect(() => {
    post<typeof result>(`/rules/${rule.id}/test`, { limit: 50 }).then((r) => setResult(r));
  }, [rule.id]);

  return (
    <Modal title={`Test: ${rule.name}`} onClose={onClose}>
      {!result ? (
        <p>Testing against recent classified emails…</p>
      ) : (
        <>
          <p>
            Matched <b>{result.matched}</b> of {result.tested} recent classified emails.
            No actions were executed.
          </p>
          <ul>
            {result.matches.map((m) => (
              <li key={m.email_id}>
                {m.subject} <span className="sub">({m.sender}, {pct(m.confidence)})</span>
              </li>
            ))}
          </ul>
        </>
      )}
    </Modal>
  );
}

export default function Rules() {
  const [rules, setRules] = useState<Rule[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [editing, setEditing] = useState<Rule | null | "new">(null);
  const [testing, setTesting] = useState<Rule | null>(null);
  const [deleting, setDeleting] = useState<Rule | null>(null);

  const load = useCallback(
    () => get<Rule[]>("/rules").then(setRules),
    [],
  );
  useEffect(() => {
    load();
    get<Category[]>("/categories").then(setCategories);
  }, [load]);

  const move = async (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= rules.length) return;
    const ids = rules.map((r) => r.id);
    [ids[index], ids[target]] = [ids[target], ids[index]];
    setRules(await post<Rule[]>("/rules/reorder", { ordered_ids: ids }));
  };

  const catName = (id: number | null) =>
    id == null ? "any" : (categories.find((c) => c.id === id)?.name ?? `#${id}`);

  return (
    <div>
      <header className="page-head">
        <h2>Rules</h2>
        <button className="primary" onClick={() => setEditing("new")}>
          + New rule
        </button>
      </header>
      <p className="sub">
        Evaluated top-down; the first matching rule's actions apply, then evaluation
        stops unless "stop processing" is off. If no rule matches, the email is left
        untouched.
      </p>

      <table className="table">
        <thead>
          <tr>
            <th>Order</th>
            <th>Name</th>
            <th>Match</th>
            <th>Actions</th>
            <th>Flow</th>
            <th>Enabled</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r, i) => (
            <tr key={r.id}>
              <td className="order-cell">
                <button className="icon-btn" disabled={i === 0} onClick={() => move(i, -1)}>
                  ▲
                </button>
                <button
                  className="icon-btn"
                  disabled={i === rules.length - 1}
                  onClick={() => move(i, 1)}
                >
                  ▼
                </button>
              </td>
              <td>
                <b>{r.name}</b>
              </td>
              <td>
                {catName(r.match_category_id)} · ≥{pct(r.match_min_confidence)}
                {r.match_sender_pattern && (
                  <div className="sub">
                    from: <code>{r.match_sender_pattern}</code>
                    {r.match_category_id == null && " (hard rule, bypasses LLM)"}
                  </div>
                )}
              </td>
              <td>{r.actions.map((a) => actionLabel(a.type)).join(", ")}</td>
              <td>{r.stop_processing ? "stop" : "continue"}</td>
              <td>{r.enabled ? <Badge tone="ok">on</Badge> : <Badge>off</Badge>}</td>
              <td className="row-actions">
                <button onClick={() => setTesting(r)}>Test</button>
                <button onClick={() => setEditing(r)}>Edit</button>
                <button className="danger" onClick={() => setDeleting(r)}>
                  Delete
                </button>
              </td>
            </tr>
          ))}
          {rules.length === 0 && (
            <tr>
              <td colSpan={7} className="sub">
                No rules — classified emails are recorded but nothing is changed in
                Gmail.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {editing !== null && (
        <RuleEditor
          rule={editing === "new" ? null : editing}
          categories={categories}
          onSaved={load}
          onClose={() => setEditing(null)}
        />
      )}
      {testing && <TestResults rule={testing} onClose={() => setTesting(null)} />}
      {deleting && (
        <ConfirmDialog
          title={`Delete rule “${deleting.name}”?`}
          danger
          confirmLabel="Delete"
          message={<p>The rule is removed; past action records are kept.</p>}
          onConfirm={async () => {
            await del(`/rules/${deleting.id}`);
            setDeleting(null);
            load();
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
