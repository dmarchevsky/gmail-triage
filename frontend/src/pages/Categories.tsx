import { useCallback, useEffect, useRef, useState } from "react";
import {
  Category,
  ColorSwatch,
  CriteriaVersion,
  Label,
  del,
  delWithBody,
  get,
  post,
  put,
} from "../api";
import {
  Badge,
  BulkActionBar,
  ColorChoice,
  ConfirmDialog,
  DiffView,
  LabelPill,
  Modal,
  SwatchPicker,
  fmtDate,
  pct,
} from "../components";
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
    criteria_md: category?.criteria_md ?? "",
    enabled: category?.enabled ?? true,
  });
  // Optional quick-create of a label + a dry-run rule (new categories only).
  const [palette, setPalette] = useState<ColorSwatch[]>([]);
  const [quick, setQuick] = useState(false);
  const [quickName, setQuickName] = useState("");
  const [quickColor, setQuickColor] = useState<ColorChoice>({ text: null, background: null });
  const [quickConf, setQuickConf] = useState(0.8);

  // Quick rule creation (both new and existing categories).
  const [labels, setLabels] = useState<Label[]>([]);
  const [quickRule, setQuickRule] = useState(false);
  const [quickRuleName, setQuickRuleName] = useState("");
  const [quickRuleConf, setQuickRuleConf] = useState(0.8);
  const [quickRuleAction, setQuickRuleAction] = useState<"archive" | "mark_read" | "add_label">("archive");
  const [quickRuleLabelId, setQuickRuleLabelId] = useState<number | null>(null);

  // Quick digest creation (both new and existing categories).
  const [quickDigest, setQuickDigest] = useState(false);
  const [quickDigestName, setQuickDigestName] = useState("");
  const [quickDigestTimes, setQuickDigestTimes] = useState("07:00");

  useEffect(() => {
    if (!category) get<ColorSwatch[]>("/labels/palette").then(setPalette);
    get<Label[]>("/labels").then(setLabels);
  }, [category]);

  // Auto-fill quick names from the category name when they're still empty.
  useEffect(() => {
    if (quickRule && !quickRuleName) setQuickRuleName(form.name || "");
    if (quickDigest && !quickDigestName) setQuickDigestName(form.name || "");
  }, [quickRule, quickDigest, form.name]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    try {
      let categoryId = category?.id;
      if (category) {
        await put(`/categories/${category.id}`, form);
      } else {
        const created = await post<Category>("/categories", form);
        categoryId = created.id;
      }
      if (!category && quick && quickName.trim() && categoryId) {
        await post(`/categories/${categoryId}/quick-label`, {
          name: quickName.trim(),
          text_color: quickColor.text,
          background_color: quickColor.background,
          min_confidence: quickConf,
        });
      }
      if (quickRule && quickRuleName.trim() && categoryId) {
        const ruleActions =
          quickRuleAction === "add_label" && quickRuleLabelId
            ? [{ type: "add_label", label_id: quickRuleLabelId }]
            : [{ type: quickRuleAction }];
        await post("/rules", {
          name: quickRuleName.trim(),
          match_category_id: categoryId,
          match_min_confidence: quickRuleConf,
          actions: ruleActions,
          dry_run: true,
        });
      }
      if (quickDigest && quickDigestName.trim() && categoryId) {
        await post("/digests", {
          name: quickDigestName.trim(),
          enabled: true,
          category_ids: [categoryId],
          cron_times: quickDigestTimes.split(",").map((s) => s.trim()).filter(Boolean),
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone ?? "UTC",
          min_confidence: 0.8,
        });
      }
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
        <label className="span2">
          Name
          <input
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
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
          Classification criteria (markdown — this text is the LLM prompt)
          <textarea
            rows={10}
            value={form.criteria_md}
            onChange={(e) => setForm({ ...form, criteria_md: e.target.value })}
            placeholder="Describe in plain language what belongs in this category, e.g.: daily/weekly market commentary newsletters; stock, bond, macro analysis; NOT individual trade confirmations."
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

        {!category && (
          <div className="span2 quick-label-box">
            <label className="checkbox">
              <input
                type="checkbox"
                checked={quick}
                onChange={(e) => setQuick(e.target.checked)}
              />
              Also create a label and a rule to apply it to this category
            </label>
            {quick && (
              <div className="quick-label-fields">
                <label>
                  Label name
                  <input
                    placeholder={`MailTriage/${form.name || "…"}`}
                    value={quickName}
                    onChange={(e) => setQuickName(e.target.value)}
                  />
                </label>
                <label>
                  Apply at confidence ≥ {pct(quickConf)}
                  <input
                    type="range"
                    min="0"
                    max="1"
                    step="0.05"
                    value={quickConf}
                    onChange={(e) => setQuickConf(Number(e.target.value))}
                  />
                </label>
                <div>
                  <p className="field-label">Color</p>
                  <SwatchPicker palette={palette} selected={quickColor} onPick={setQuickColor} />
                  <p className="sub" style={{ marginTop: "0.4rem" }}>
                    <LabelPill
                      name={quickName || "label"}
                      textColor={quickColor.text}
                      backgroundColor={quickColor.background}
                    />{" "}
                    — the rule starts in dry-run.
                  </p>
                </div>
              </div>
            )}
          </div>
        )}

        <div className="span2 quick-label-box">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={quickRule}
              onChange={(e) => setQuickRule(e.target.checked)}
            />
            Also create a rule for this category (starts in dry-run)
          </label>
          {quickRule && (
            <div className="quick-label-fields">
              <label>
                Rule name
                <input
                  value={quickRuleName}
                  onChange={(e) => setQuickRuleName(e.target.value)}
                  placeholder={form.name || "Rule name"}
                />
              </label>
              <label>
                Min confidence ≥ {pct(quickRuleConf)}
                <input
                  type="range"
                  min="0"
                  max="1"
                  step="0.05"
                  value={quickRuleConf}
                  onChange={(e) => setQuickRuleConf(Number(e.target.value))}
                />
              </label>
              <label>
                Action
                <select
                  value={quickRuleAction}
                  onChange={(e) =>
                    setQuickRuleAction(e.target.value as "archive" | "mark_read" | "add_label")
                  }
                >
                  <option value="archive">Archive</option>
                  <option value="mark_read">Mark as read</option>
                  <option value="add_label">Add label…</option>
                </select>
              </label>
              {quickRuleAction === "add_label" && (
                <label>
                  Label
                  <select
                    value={quickRuleLabelId ?? ""}
                    onChange={(e) =>
                      setQuickRuleLabelId(e.target.value ? Number(e.target.value) : null)
                    }
                  >
                    <option value="">pick a label…</option>
                    {labels.map((lb) => (
                      <option key={lb.id} value={lb.id}>
                        {lb.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          )}
        </div>

        <div className="span2 quick-label-box">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={quickDigest}
              onChange={(e) => setQuickDigest(e.target.checked)}
            />
            Also create a digest for this category
          </label>
          {quickDigest && (
            <div className="quick-label-fields">
              <label>
                Digest name
                <input
                  value={quickDigestName}
                  onChange={(e) => setQuickDigestName(e.target.value)}
                  placeholder={form.name || "Digest name"}
                />
              </label>
              <label>
                Send at (HH:MM, comma-separated)
                <input
                  value={quickDigestTimes}
                  onChange={(e) => setQuickDigestTimes(e.target.value)}
                  placeholder="07:00"
                />
              </label>
            </div>
          )}
        </div>
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
  const toast = useToast();
  const [categories, setCategories] = useState<Category[]>([]);
  const [editing, setEditing] = useState<Category | null | "new">(null);
  const [history, setHistory] = useState<Category | null>(null);
  const [deleting, setDeleting] = useState<Category | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [bulkConfirm, setBulkConfirm] = useState<"delete" | null>(null);

  const load = useCallback(() => get<Category[]>("/categories").then(setCategories), []);
  useEffect(() => {
    load();
  }, [load]);

  const allChecked =
    categories.length > 0 && categories.every((c) => selectedIds.has(c.id));
  const someChecked = categories.some((c) => selectedIds.has(c.id));
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

  const selectAll = () => setSelectedIds(new Set(categories.map((c) => c.id)));
  const clearSelection = () => setSelectedIds(new Set());

  const doBulkEnable = async (enabled: boolean) => {
    const ids = Array.from(selectedIds);
    try {
      const r = await put<{ updated: number }>("/categories/bulk", {
        category_ids: ids,
        enabled,
      });
      toast.success(`${r.updated} categor${r.updated === 1 ? "y" : "ies"} ${enabled ? "enabled" : "disabled"}`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

  const doBulkDelete = async () => {
    const ids = Array.from(selectedIds);
    try {
      const r = await delWithBody<{ deleted: number }>("/categories/bulk", {
        category_ids: ids,
      });
      toast.success(`${r.deleted} categor${r.deleted === 1 ? "y" : "ies"} deleted`);
      clearSelection();
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    }
  };

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

      <BulkActionBar
        count={selectedIds.size}
        onClear={clearSelection}
        actions={[
          { label: "Enable", onClick: async () => doBulkEnable(true) },
          { label: "Disable", onClick: async () => doBulkEnable(false) },
          {
            label: "Delete",
            danger: true,
            onClick: async () => setBulkConfirm("delete"),
          },
        ]}
      />

      <div className="table-scroll wide">
      <table className="table categories-table">
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
                <input
                  type="checkbox"
                  checked={selectedIds.has(c.id)}
                  onChange={() => toggleSelect(c.id)}
                />
              </td>
              <td data-label="Name">
                <b>{c.name}</b>
                {c.description && <div className="sub">{c.description}</div>}
              </td>
              <td data-label="Criteria" className="ellipsis criteria-preview">{c.criteria_md.slice(0, 80)}</td>
              <td data-label="Version">v{c.criteria_version}</td>
              <td data-label="Enabled">{c.enabled ? <Badge tone="ok">on</Badge> : <Badge>off</Badge>}</td>
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
      </div>

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
          title={`Delete category "${deleting.name}"?`}
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
      {bulkConfirm === "delete" && (
        <ConfirmDialog
          title={`Delete ${selectedIds.size} categor${selectedIds.size === 1 ? "y" : "ies"}?`}
          danger
          confirmLabel="Delete all"
          message={
            <p>
              Emails keep their history but lose classification references. Rules
              matching these categories will stop matching. Gmail labels are{" "}
              <b>not</b> deleted.
            </p>
          }
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
