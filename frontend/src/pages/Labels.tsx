import { useCallback, useEffect, useState } from "react";
import { ColorSwatch, Label, del, errMsg, get, post, put } from "../api";
import { ConfirmDialog, LabelPill, Modal, SwatchPicker } from "../components";
import { useToast } from "../toast";
import { Pencil, Trash2 } from "lucide-react";

function LabelEditor({
  label,
  palette,
  onSaved,
  onClose,
}: {
  label: Label | null;
  palette: ColorSwatch[];
  onSaved: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(label?.name ?? "");
  const [color, setColor] = useState<{ text: string | null; background: string | null }>({
    text: label?.text_color ?? null,
    background: label?.background_color ?? null,
  });

  const save = async () => {
    const body = {
      name,
      text_color: color.text,
      background_color: color.background,
    };
    try {
      if (label) await put(`/labels/${label.id}`, body);
      else await post("/labels", body);
      toast.success(label ? "Label updated" : "Label created");
      onSaved();
      onClose();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  return (
    <Modal title={label ? `Edit label: ${label.name}` : "New label"} onClose={onClose}>
      <div className="form-grid">
        <label className="span2">
          Name (the Gmail label; use "/" for nesting, e.g. MailTriage/News)
          <input value={name} onChange={(e) => setName(e.target.value)} autoFocus />
        </label>
        <div className="span2">
          <p className="field-label">Color</p>
          <SwatchPicker palette={palette} selected={color} onPick={setColor} />
          <p className="sub" style={{ marginTop: "0.5rem" }}>
            Preview: <LabelPill name={name || "label"} textColor={color.text}
              backgroundColor={color.background} />
          </p>
        </div>
      </div>
      <div className="modal-actions">
        <button onClick={onClose}>Cancel</button>
        <button className="primary" onClick={save} disabled={!name.trim()}>
          Save
        </button>
      </div>
    </Modal>
  );
}

export default function Labels() {
  const toast = useToast();
  const [labels, setLabels] = useState<Label[]>([]);
  const [palette, setPalette] = useState<ColorSwatch[]>([]);
  const [editing, setEditing] = useState<Label | null | "new">(null);
  const [deleting, setDeleting] = useState<Label | null>(null);

  const load = useCallback(() => get<Label[]>("/labels").then(setLabels), []);
  useEffect(() => {
    load();
    get<ColorSwatch[]>("/labels/palette").then(setPalette);
  }, [load]);

  return (
    <div>
      <header className="page-head">
        <h2>Labels</h2>
        <button className="primary" onClick={() => setEditing("new")}>
          + New label
        </button>
      </header>
      <p className="sub">
        Labels are Gmail labels applied to emails by rules. Create a label here (or
        quickly from a category), pick a Gmail color, then reference it in a rule's
        "Add label" action.
      </p>

      <div className="table-scroll">
        <table className="table labels-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Gmail</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {labels.map((lb) => (
              <tr key={lb.id}>
                <td data-label="Label">
                {lb.is_system ? (
                  <LabelPill
                    name={lb.name}
                    textColor={lb.text_color}
                    backgroundColor={lb.background_color}
                  />
                ) : (
                  <button className="name-link label-name-link" onClick={() => setEditing(lb)}>
                    <LabelPill
                      name={lb.name}
                      textColor={lb.text_color}
                      backgroundColor={lb.background_color}
                    />
                  </button>
                )}
              </td>
                <td data-label="Gmail">
                  {lb.is_system
                    ? "built-in"
                    : lb.gmail_label_id
                    ? "synced"
                    : <span className="sub">not yet created</span>}
                </td>
                <td className="row-actions">
                  {!lb.is_system && (
                    <>
                      <button className="icon-btn" title="Edit" onClick={() => setEditing(lb)}>
                        <Pencil size={15} />
                      </button>
                      <button className="icon-btn danger" title="Delete" onClick={() => setDeleting(lb)}>
                        <Trash2 size={15} />
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {labels.length === 0 && (
              <tr>
                <td colSpan={3} className="sub">
                  No labels yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {editing !== null && (
        <LabelEditor
          label={editing === "new" ? null : editing}
          palette={palette}
          onSaved={load}
          onClose={() => setEditing(null)}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Delete label “${deleting.name}”?`}
          danger
          confirmLabel="Delete"
          message={
            <p>
              The label is removed locally and in Gmail (it's unassigned from any
              messages; no message is deleted). If a rule still uses it you'll be
              told to remove it from that rule first.
            </p>
          }
          onConfirm={async () => {
            try {
              await del(`/labels/${deleting.id}`);
              toast.success("Label deleted");
            } catch (e) {
              toast.error(errMsg(e));
            }
            setDeleting(null);
            load();
          }}
          onCancel={() => setDeleting(null)}
        />
      )}
    </div>
  );
}
