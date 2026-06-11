import { useCallback, useEffect, useState } from "react";
import { Category, FeedbackItem, get, post } from "../api";
import { AsyncButton, Badge, DiffView, ErrorNote, Modal, fmtDate } from "../components";

function ProposalReview({
  item,
  categories,
  onDone,
  onClose,
}: {
  item: FeedbackItem;
  categories: Category[];
  onDone: () => void;
  onClose: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [edited, setEdited] = useState(item.proposed_criteria_md ?? "");
  const [error, setError] = useState<string | null>(null);

  const targetCategory = categories.find(
    (c) =>
      c.id ===
      (item.correct_category_id ??
        categories.find((x) => x.name === item.original_category)?.id),
  );

  const act = async (path: string, body?: unknown) => {
    setError(null);
    try {
      await post(`/feedback/${item.id}/${path}`, body);
      onDone();
      onClose();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Modal title={`Proposal — ${targetCategory?.name ?? "?"}`} onClose={onClose} wide>
      <p className="sub">
        Email “{item.email_subject}” from {item.email_sender}: classified as{" "}
        <b>{item.original_category ?? "none"}</b>, should be{" "}
        <b>{item.correct_category ?? "none"}</b>.
        {item.user_note && (
          <>
            {" "}
            Note: <i>{item.user_note}</i>
          </>
        )}
      </p>
      {item.proposal_explanation && (
        <p className="rationale">
          <b>LLM explanation:</b> {item.proposal_explanation}
        </p>
      )}

      <h4>Criteria change (current → proposed)</h4>
      {editing ? (
        <textarea rows={12} value={edited} onChange={(e) => setEdited(e.target.value)} />
      ) : (
        <DiffView
          oldText={targetCategory?.criteria_md ?? ""}
          newText={item.proposed_criteria_md ?? ""}
        />
      )}
      <ErrorNote error={error} />
      <div className="modal-actions">
        <button onClick={() => act("reject")}>Reject</button>
        {editing ? (
          <button className="primary" onClick={() => act("approve", { criteria_md: edited })}>
            Approve edited version
          </button>
        ) : (
          <>
            <button onClick={() => setEditing(true)}>Edit then approve</button>
            <button className="primary" onClick={() => act("approve")}>
              Approve
            </button>
          </>
        )}
      </div>
    </Modal>
  );
}

export default function FeedbackQueue() {
  const [items, setItems] = useState<FeedbackItem[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [reviewing, setReviewing] = useState<FeedbackItem | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setItems(await get<FeedbackItem[]>("/feedback?status=open"));
    setCategories(await get<Category[]>("/categories"));
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div>
      <header className="page-head">
        <h2>Feedback queue</h2>
      </header>
      <p className="sub">
        When you flag a misclassified email, the LLM proposes a revision of the
        affected category's criteria (debounced ~1 min). Nothing changes without your
        approval.
      </p>
      {note && <p className="note">{note}</p>}

      <table className="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Email</th>
            <th>Was</th>
            <th>Should be</th>
            <th>Note</th>
            <th>Proposal</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {items.map((f) => (
            <tr key={f.id}>
              <td>{fmtDate(f.created_at)}</td>
              <td className="ellipsis">{f.email_subject}</td>
              <td>{f.original_category ?? "none"}</td>
              <td>{f.correct_category ?? "none"}</td>
              <td className="ellipsis">{f.user_note}</td>
              <td>
                <Badge
                  tone={
                    f.proposal_status === "pending_review"
                      ? "warn"
                      : f.proposal_status === "rejected"
                        ? "error"
                        : "neutral"
                  }
                >
                  {f.proposal_status}
                </Badge>
              </td>
              <td className="row-actions">
                {f.proposal_status === "pending_review" ? (
                  <button className="primary" onClick={() => setReviewing(f)}>
                    Review
                  </button>
                ) : (
                  <AsyncButton
                    onClick={async () => {
                      setNote(null);
                      try {
                        await post(`/feedback/${f.id}/generate-proposal`);
                        setNote("Proposal generated.");
                      } catch (e) {
                        setNote(
                          `Generation failed: ${e instanceof Error ? e.message : e}`,
                        );
                      }
                      await load();
                    }}
                  >
                    Generate now
                  </AsyncButton>
                )}
                <AsyncButton
                  onClick={async () => {
                    await post(`/feedback/${f.id}/dismiss`);
                    await load();
                  }}
                >
                  Dismiss
                </AsyncButton>
              </td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr>
              <td colSpan={7} className="sub">
                No open feedback. Flag a misclassified email from the Emails page.
              </td>
            </tr>
          )}
        </tbody>
      </table>

      {reviewing && (
        <ProposalReview
          item={reviewing}
          categories={categories}
          onDone={() => {
            setNote("Done.");
            load();
          }}
          onClose={() => setReviewing(null)}
        />
      )}
    </div>
  );
}
