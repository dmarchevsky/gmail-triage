import { useCallback, useEffect, useRef, useState } from "react";
import { Category, Label, Rule, RuleAction, del, delWithBody, get, post, put } from "../api";
import {
  Badge,
  BulkActionBar,
  ConfirmDialog,
  LabelPill,
  Modal,
  actionLabel,
  pct,
} from "../components";
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
  labels,
  onChange,
}: {
  actions: RuleAction[];
  labels: Label[];
  onChange: (a: RuleAction[]) => void;
}) {
  const update = (i: number, patch: Partial<RuleAction>) => {
    const next = actions.slice();
    next[i] = { ...next[i], ...patch };
    onChange(next);
  };
  const needsLabel = (t: string) => t === "add_label" || t === "remove_label";
  return (
    <div className="action-builder">
      {actions.map((a, i) => (
        <div key={i} className="action-row">
          <select
            value={a.type}
            onChange={(e) => {
              const type = e.target.value as RuleAction["type"];
              // keep label_id when switching between add/remove label
              const next: RuleAction = needsLabel(type)
                ? { type, label_id: a.label_id }
                : { type };
              onChange(actions.map((x, j) => (j === i ? next : x)));
            }}
          >
            {ACTION_TYPES.map((t) => (
              <option key={t} value={t}>
                {actionLabel(t)}
              </option>
            ))}
          </select>
          {needsLabel(a.type) && (
            <select
              value={a.label_id ?? ""}
              onChange={(e) =>
                update(i, { label_id: e.target.value ? Number(e.target.value) : undefined })
              }
            >
              <option value="">label…</option>
              {labels.map((lb) => (
                <option key={lb.id} value={lb.id}>
                  {lb.name}
                </option>
              ))}
            </select>
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
      {labels.length === 0 && (
        <p className="sub">No labels yet — create one on the Labels page to use "Add label".</p>
      )}
    </div>
  );
}

function RuleEditor({
  rule,
  categories,
  labels,
  onSaved,
  onClose,
}: {
  rule: Rule | null;
  categories: Category[];
  labels: Label[];
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
    dry_run: rule?.dry_run ?? true,
  });
  const [confirmTrash, setConfirmTrash] = useState(false);
  const [confirmLive, setConfirmLive] = useState(false);

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
    const goingLive = !form.dry_run && (rule ? rule.dry_run : true);
    if (goingLive) setConfirmLive(true);
    else if (form.actions.some((a) => a.type === "trash")) setConfirmTrash(true);
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
            labels={labels}
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
            checked={form.dry_run}
            onChange={(e) => setForm({ ...form, dry_run: e.target.checked })}
          />
          Dry-run — record planned actions without executing (uncheck to go live)
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
      {confirmLive && (
        <ConfirmDialog
          title="Switch this rule to LIVE?"
          danger
          confirmLabel="Go live"
          message={
            <p>
              This rule's actions will <b>really modify your Gmail</b> from now on:
              labels, mark-read, archive and trash will execute on matching emails.
            </p>
          }
          onConfirm={() => {
            setConfirmLive(false);
            if (form.actions.some((a) => a.type === "trash")) setConfirmTrash(true);
            else doSave();
          }}
          onCancel={() => setConfirmLive(false)}
        />
      )}
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

function DefaultRuleEditor({
  rule,
  labels,
  onSaved,
  onClose,
}: {
  rule: Rule;
  labels: Label[];
  onSaved: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [actions, setActions] = useState<RuleAction[]>(rule.actions);
  const [dryRun, setDryRun] = useState(rule.dry_run);
  const [enabled, setEnabled] = useState(rule.enabled);
  const [confirmLive, setConfirmLive] = useState(false);

  const doSave = async () => {
    try {
      await put(`/rules/${rule.id}/default`, {
        enabled,
        dry_run: dryRun,
        stop_processing: true,
        actions,
      });
      toast.success("Default rule updated");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const save = () => {
    if (!dryRun && rule.dry_run && actions.length > 0) setConfirmLive(true);
    else doSave();
  };

  return (
    <Modal title="Edit default rule" onClose={onClose} wide>
      <p className="sub">
        The default rule runs last and only acts on emails no other rule matched.
        Leave its action list empty to do nothing.
      </p>
      <div className="form-grid">
        <div className="span2">
          <p className="field-label">Default action</p>
          <ActionBuilder actions={actions} labels={labels} onChange={setActions} />
        </div>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(e) => setDryRun(e.target.checked)}
          />
          Dry-run — record planned actions without executing (uncheck to go live)
        </label>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
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
      {confirmLive && (
        <ConfirmDialog
          title="Switch the default rule to LIVE?"
          danger
          confirmLabel="Go live"
          message={
            <p>
              The default action will <b>really modify your Gmail</b> for every
              email no other rule handled.
            </p>
          }
          onConfirm={() => {
            setConfirmLive(false);
            doSave();
          }}
          onCancel={() => setConfirmLive(false)}
        />
      )}
    </Modal>
  );
}

function ruleBody(rule: Rule, overrides: Partial<Rule>) {
  return {
    name: rule.name,
    enabled: rule.enabled,
    priority: rule.priority,
    match_category_id: rule.match_category_id,
    match_min_confidence: rule.match_min_confidence,
    match_sender_pattern: rule.match_sender_pattern,
    actions: rule.actions,
    stop_processing: rule.stop_processing,
    dry_run: rule.dry_run,
    ...overrides,
  };
}

export default function Rules() {
  const toast = useToast();
  const [rules, setRules] = useState<Rule[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [labels, setLabels] = useState<Label[]>([]);
  const [editing, setEditing] = useState<Rule | null | "new">(null);
  const [editingDefault, setEditingDefault] = useState<Rule | null>(null);
  const [deleting, setDeleting] = useState<Rule | null>(null);
  const [goingLive, setGoingLive] = useState<Rule | null>(null);
  const [offerApply, setOfferApply] = useState<Rule | null>(null);
  const [reapplying, setReapplying] = useState<Rule | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState<"delete" | "go-live" | "reapply" | null>(
    null,
  );

  const applyPlanned = async (rule: Rule) => {
    try {
      const r = await post<{ applied: number; failed: number; emails: number }>(
        `/rules/${rule.id}/apply-planned`,
      );
      if (r.failed > 0)
        toast.error(
          `Applied ${r.applied} action(s); ${r.failed} failed — see email details`,
        );
      else toast.success(`Applied ${r.applied} planned action(s) on ${r.emails} email(s)`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
    await load();
  };

  const setMode = async (rule: Rule, dryRun: boolean) => {
    try {
      const updated = rule.is_default
        ? await put<Rule>(`/rules/${rule.id}/default`, {
            enabled: rule.enabled,
            dry_run: dryRun,
            stop_processing: rule.stop_processing,
            actions: rule.actions,
          })
        : await put<Rule>(`/rules/${rule.id}`, ruleBody(rule, { dry_run: dryRun }));
      toast.success(dryRun ? `"${rule.name}" back to dry-run` : `"${rule.name}" is LIVE`);
      if (!dryRun && updated.pending_planned > 0) setOfferApply(updated);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
    await load();
  };

  const reapply = async (rule: Rule) => {
    try {
      const r = await post<{ matched: number; applied: number; failed: number }>(
        `/rules/${rule.id}/reapply`,
      );
      if (r.failed > 0)
        toast.error(`Reapplied to ${r.matched} email(s); ${r.failed} failed`);
      else if (rule.dry_run)
        toast.success(`Reapplied "${rule.name}": ${r.matched} email(s) re-planned`);
      else toast.success(`Reapplied "${rule.name}": ${r.applied} of ${r.matched} executed`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
    await load();
  };

  const doBulkReapply = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await post<{ rules: number; matched: number; applied: number; failed: number }>(
        "/rules/reapply-bulk",
        { rule_ids: ids },
      );
      if (r.failed > 0)
        toast.error(`Reapplied ${r.rules} rule(s); ${r.failed} action(s) failed`);
      else toast.success(`Reapplied ${r.rules} rule(s) across ${r.matched} email(s)`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const load = useCallback(
    () => get<Rule[]>("/rules").then(setRules),
    [],
  );
  useEffect(() => {
    load();
    get<Category[]>("/categories").then(setCategories);
    get<Label[]>("/labels").then(setLabels);
  }, [load]);

  // The default rule is pinned last and never reordered.
  const reorderable = rules.filter((r) => !r.is_default);

  const move = async (index: number, delta: number) => {
    const target = index + delta;
    if (target < 0 || target >= reorderable.length) return;
    const ids = reorderable.map((r) => r.id);
    [ids[index], ids[target]] = [ids[target], ids[index]];
    setRules(await post<Rule[]>("/rules/reorder", { ordered_ids: ids }));
  };

  const catName = (id: number | null) =>
    id == null ? "any" : (categories.find((c) => c.id === id)?.name ?? `#${id}`);

  const selectable = rules.filter((r) => !r.is_default);
  const allChecked = selectable.length > 0 && selectable.every((r) => selectedIds.has(r.id));
  const someChecked = selectable.some((r) => selectedIds.has(r.id));
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

  const selectAll = () => setSelectedIds(new Set(selectable.map((r) => r.id)));
  const clearSelection = () => setSelectedIds(new Set());

  const doBulkUpdate = async (patch: { enabled?: boolean; dry_run?: boolean }) => {
    const ids = Array.from(selectedIds);
    try {
      const r = await put<{ updated: number }>("/rules/bulk", { rule_ids: ids, ...patch });
      toast.success(`${r.updated} rule${r.updated === 1 ? "" : "s"} updated`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const doBulkDelete = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await delWithBody<{ deleted: number }>("/rules/bulk", { rule_ids: ids });
      toast.success(`${r.deleted} rule${r.deleted === 1 ? "" : "s"} deleted`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

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
        stops unless "stop processing" is off. The pinned <b>Default</b> rule runs
        last and acts only on emails no other rule matched (it does nothing until you
        give it an action).
      </p>

      <BulkActionBar
        count={selectedIds.size}
        onClear={clearSelection}
        actions={[
          { label: "Enable", onClick: async () => doBulkUpdate({ enabled: true }) },
          { label: "Disable", onClick: async () => doBulkUpdate({ enabled: false }) },
          {
            label: "Go live",
            onClick: async () => setBulkConfirm("go-live"),
          },
          { label: "Go dry-run", onClick: async () => doBulkUpdate({ dry_run: true }) },
          { label: "Reapply", onClick: async () => setBulkConfirm("reapply") },
          {
            label: "Delete",
            danger: true,
            onClick: async () => setBulkConfirm("delete"),
          },
        ]}
      />

      <div className="table-scroll wide">
      <table className="table rules-table">
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
            <th>Order</th>
            <th>Name</th>
            <th>Match</th>
            <th>Actions</th>
            <th>Mode</th>
            <th>Flow</th>
            <th>Enabled</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {rules.map((r, i) => (
            <tr key={r.id}>
              <td>
                {!r.is_default && (
                  <input
                    type="checkbox"
                    checked={selectedIds.has(r.id)}
                    onChange={() => toggleSelect(r.id)}
                  />
                )}
              </td>
              <td className="order-cell">
                {r.is_default ? (
                  <span className="sub">last</span>
                ) : (
                  <>
                    <button className="icon-btn" disabled={i === 0} onClick={() => move(i, -1)}>
                      ▲
                    </button>
                    <button
                      className="icon-btn"
                      disabled={i === reorderable.length - 1}
                      onClick={() => move(i, 1)}
                    >
                      ▼
                    </button>
                  </>
                )}
              </td>
              <td data-label="Name">
                {r.is_default ? (
                  <b>
                    <i>{r.name}</i>
                  </b>
                ) : (
                  <b>{r.name}</b>
                )}
              </td>
              <td data-label="Match">
                {r.is_default ? (
                  <span className="sub">any — when no other rule matched</span>
                ) : (
                  <>
                    {catName(r.match_category_id)} · ≥{pct(r.match_min_confidence)}
                    {r.match_sender_pattern && (
                      <div className="sub">
                        from: <code>{r.match_sender_pattern}</code>
                        {r.match_category_id == null && " (hard rule, bypasses LLM)"}
                      </div>
                    )}
                  </>
                )}
              </td>
              <td data-label="Actions">
                {r.actions.map((a, idx) => (
                  <span key={idx} className="action-chip">
                    {actionLabel(a.type)}
                    {a.label_name && (
                      <>
                        {" "}
                        <LabelPill
                          name={a.label_name}
                          textColor={a.text_color}
                          backgroundColor={a.background_color}
                        />
                      </>
                    )}
                    {idx < r.actions.length - 1 && ", "}
                  </span>
                ))}
              </td>
              <td data-label="Mode">
                {r.dry_run ? <Badge tone="dry">DRY RUN</Badge> : <Badge tone="ok">LIVE</Badge>}
                {r.pending_planned > 0 && (
                  <div className="sub">
                    {r.pending_planned} planned
                    {!r.dry_run && (
                      <>
                        {" · "}
                        <button className="icon-btn" onClick={() => setOfferApply(r)}>
                          Apply
                        </button>
                      </>
                    )}
                  </div>
                )}
              </td>
              <td data-label="Flow">{r.stop_processing ? "stop" : "continue"}</td>
              <td data-label="Enabled">{r.enabled ? <Badge tone="ok">on</Badge> : <Badge>off</Badge>}</td>
              <td className="row-actions">
                {r.dry_run ? (
                  <button onClick={() => setGoingLive(r)}>Go live</button>
                ) : (
                  <button onClick={() => setMode(r, true)}>To dry-run</button>
                )}
                <button onClick={() => setReapplying(r)}>Reapply</button>
                <button onClick={() => (r.is_default ? setEditingDefault(r) : setEditing(r))}>
                  Edit
                </button>
                {!r.is_default && (
                  <button className="danger" onClick={() => setDeleting(r)}>
                    Delete
                  </button>
                )}
              </td>
            </tr>
          ))}
          {rules.length === 0 && (
            <tr>
              <td colSpan={9} className="sub">
                No rules — classified emails are recorded but nothing is changed in
                Gmail. New rules start in dry-run.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      </div>

      {editing !== null && (
        <RuleEditor
          rule={editing === "new" ? null : editing}
          categories={categories}
          labels={labels}
          onSaved={load}
          onClose={() => setEditing(null)}
        />
      )}
      {editingDefault && (
        <DefaultRuleEditor
          rule={editingDefault}
          labels={labels}
          onSaved={load}
          onClose={() => setEditingDefault(null)}
        />
      )}
      {reapplying && (
        <ConfirmDialog
          title={`Reapply "${reapplying.name}" to the backlog?`}
          danger={!reapplying.dry_run}
          confirmLabel="Reapply"
          message={
            <p>
              This re-runs the rule against every already-classified email and
              {reapplying.dry_run
                ? " re-records its planned actions (nothing is executed in Gmail)."
                : " executes its actions against Gmail for each match."}{" "}
              The rule's previous action records on those emails are replaced.
            </p>
          }
          onConfirm={async () => {
            const rule = reapplying;
            setReapplying(null);
            await reapply(rule);
          }}
          onCancel={() => setReapplying(null)}
        />
      )}
      {bulkConfirm === "reapply" && (
        <ConfirmDialog
          title={`Reapply ${selectedIds.size} rule(s) to the backlog?`}
          danger
          confirmLabel="Reapply"
          message={
            <p>
              Each selected rule is re-run against the already-classified emails.
              Live rules <b>modify your Gmail</b>; dry-run rules only re-record
              planned actions. Previous action records for these rules are replaced.
            </p>
          }
          onConfirm={async () => {
            setBulkConfirm(null);
            await doBulkReapply();
          }}
          onCancel={() => setBulkConfirm(null)}
        />
      )}
      {goingLive && (
        <ConfirmDialog
          title={`Switch "${goingLive.name}" to LIVE?`}
          danger
          confirmLabel="Go live"
          message={
            <p>
              This rule's actions will <b>really modify your Gmail</b> from now on:
              labels, mark-read, archive and trash will execute on matching emails.
            </p>
          }
          onConfirm={async () => {
            const rule = goingLive;
            setGoingLive(null);
            await setMode(rule, false);
          }}
          onCancel={() => setGoingLive(null)}
        />
      )}
      {bulkConfirm === "go-live" && (
        <ConfirmDialog
          title={`Switch ${selectedIds.size} rule(s) to LIVE?`}
          danger
          confirmLabel="Go live"
          message={
            <p>
              These rules' actions will <b>really modify your Gmail</b> from now on:
              labels, mark-read, archive and trash will execute on matching emails.
            </p>
          }
          onConfirm={async () => {
            setBulkConfirm(null);
            await doBulkUpdate({ dry_run: false });
          }}
          onCancel={() => setBulkConfirm(null)}
        />
      )}
      {offerApply && (
        <ConfirmDialog
          title={`Apply ${offerApply.pending_planned} planned action(s)?`}
          confirmLabel="Apply now"
          message={
            <p>
              While "{offerApply.name}" was in dry-run it planned{" "}
              <b>{offerApply.pending_planned}</b> action(s) on past emails. Apply
              them to Gmail now? (Exactly the actions shown in each email's detail
              view — nothing is re-evaluated.)
            </p>
          }
          onConfirm={async () => {
            const rule = offerApply;
            setOfferApply(null);
            await applyPlanned(rule);
          }}
          onCancel={() => setOfferApply(null)}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete rule "${deleting.name}"?`}
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
      {bulkConfirm === "delete" && (
        <ConfirmDialog
          title={`Delete ${selectedIds.size} rule${selectedIds.size === 1 ? "" : "s"}?`}
          danger
          confirmLabel="Delete all"
          message={<p>The rules are removed; past action records are kept.</p>}
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
