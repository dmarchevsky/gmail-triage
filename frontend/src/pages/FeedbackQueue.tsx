import { useCallback, useEffect, useState } from "react";
import {
  Category,
  FeedbackItem,
  approveProposal,
  errMsg,
  get,
  post,
  rejectProposal,
} from "../api";
import { AsyncButton, Badge, DiffView, Modal, fmtDate } from "../components";
import { useToast } from "../toast";

/** Whether this row is the representative holding a reviewable target/source
 * proposal — merged (non-representative) rows never carry "pending_review". */
function targetReady(f: FeedbackItem) {
  return f.proposal_status === "pending_review";
}
function sourceReady(f: FeedbackItem) {
  return !!f.source_category && f.proposal_source_status === "pending_review";
}
function sourceDue(f: FeedbackItem) {
  return !!f.source_category && f.proposal_source_status !== "approved";
}
function sourceGenerating(f: FeedbackItem) {
  return !!f.source_category && f.proposal_source_status === "none";
}

function proposalSummary(f: FeedbackItem): { label: string; tone: "warn" | "info" | "neutral" | "error" } {
  const tMerged = !!f.merged_into;
  const sMerged = !!f.source_category && !!f.source_merged_into;
  if (tMerged && (!f.source_category || sMerged)) {
    return { label: "merged into review", tone: "neutral" };
  }
  if (targetReady(f) && (!f.source_category || sourceReady(f))) {
    return { label: "Ready to review", tone: "warn" };
  }
  if (targetReady(f) && sourceGenerating(f)) {
    return { label: "Generating source fix…", tone: "info" };
  }
  if (sourceReady(f) && f.proposal_status !== "pending_review") {
    return { label: "Ready to review", tone: "warn" };
  }
  const rejected =
    f.proposal_status === "rejected" ||
    (!!f.source_category && f.proposal_source_status === "rejected");
  if (rejected) {
    return { label: "rejected", tone: "error" };
  }
  return { label: "none", tone: "neutral" };
}

function CombinedProposalReview({
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
  const toast = useToast();
  const showTarget = targetReady(item);
  const showSource = sourceReady(item);

  const [editingTarget, setEditingTarget] = useState(false);
  const [editedTarget, setEditedTarget] = useState(item.proposed_criteria_md ?? "");
  const [editingSource, setEditingSource] = useState(false);
  const [editedSource, setEditedSource] = useState(item.proposed_source_criteria_md ?? "");

  const targetCategory = categories.find(
    (c) =>
      c.id ===
      (item.correct_category_id ??
        categories.find((x) => x.name === item.original_category)?.id),
  );
  const sourceCategory = categories.find((c) => c.id === item.source_category_id);

  const approve = async () => {
    try {
      if (showTarget) {
        await approveProposal(item.id, editingTarget ? editedTarget : undefined, "target");
      }
      if (showSource) {
        await approveProposal(item.id, editingSource ? editedSource : undefined, "source");
      }
      toast.success("Criteria updated");
      onDone();
      onClose();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const reject = async () => {
    try {
      if (showTarget) await rejectProposal(item.id, "target");
      if (showSource) await rejectProposal(item.id, "source");
      toast.success("Proposal rejected");
      onDone();
      onClose();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  return (
    <Modal title="Proposal review" onClose={onClose} wide>
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

      {showTarget && (
        <div className="proposal-section">
          <h4>{targetCategory?.name ?? "?"} — inclusion</h4>
          {(item.covers_count ?? 0) > 1 && (
            <p className="note">
              This consolidated proposal considers <b>{item.covers_count}</b> feedback
              items for this category — approving incorporates them all at once.
            </p>
          )}
          {item.proposal_explanation && (
            <p className="rationale">
              <b>LLM explanation:</b> {item.proposal_explanation}
            </p>
          )}
          {editingTarget ? (
            <textarea
              rows={10}
              value={editedTarget}
              onChange={(e) => setEditedTarget(e.target.value)}
            />
          ) : (
            <DiffView
              oldText={targetCategory?.criteria_md ?? ""}
              newText={item.proposed_criteria_md ?? ""}
            />
          )}
          {!editingTarget && (
            <button onClick={() => setEditingTarget(true)}>Edit this criteria</button>
          )}
        </div>
      )}

      {showSource && (
        <div className="proposal-section">
          <h4>{sourceCategory?.name ?? "?"} — exclusion</h4>
          <p className="note">
            This revises <b>{item.source_category}</b>'s criteria to exclude mail like
            this one, so it stops winning over the correct category.
          </p>
          {(item.source_covers_count ?? 0) > 1 && (
            <p className="note">
              This consolidated proposal considers <b>{item.source_covers_count}</b>{" "}
              feedback items for this category — approving incorporates them all at once.
            </p>
          )}
          {item.proposal_source_explanation && (
            <p className="rationale">
              <b>LLM explanation:</b> {item.proposal_source_explanation}
            </p>
          )}
          {editingSource ? (
            <textarea
              rows={10}
              value={editedSource}
              onChange={(e) => setEditedSource(e.target.value)}
            />
          ) : (
            <DiffView
              oldText={sourceCategory?.criteria_md ?? ""}
              newText={item.proposed_source_criteria_md ?? ""}
            />
          )}
          {!editingSource && (
            <button onClick={() => setEditingSource(true)}>Edit this criteria</button>
          )}
        </div>
      )}

      <div className="modal-actions">
        <button onClick={reject}>Reject</button>
        <button className="primary" onClick={approve}>
          Approve
        </button>
      </div>
    </Modal>
  );
}

export default function FeedbackQueue() {
  const [items, setItems] = useState<FeedbackItem[]>([]);
  const [categories, setCategories] = useState<Category[]>([]);
  const [reviewing, setReviewing] = useState<FeedbackItem | null>(null);
  const toast = useToast();

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

      <div className="table-scroll wide">
      <table className="table feedback-table">
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
          {items.map((f) => {
            const canReview = targetReady(f) || sourceReady(f);
            const targetNeedsGen =
              !f.merged_into && f.proposal_status !== "pending_review";
            const sourceNeedsGen =
              sourceDue(f) && !f.source_merged_into &&
              f.proposal_source_status !== "pending_review";
            const needsGenerate = targetNeedsGen || sourceNeedsGen;
            const summary = proposalSummary(f);
            return (
              <tr key={f.id}>
                <td data-label="When">{fmtDate(f.created_at)}</td>
                <td data-label="Email" className="ellipsis">{f.email_subject}</td>
                <td data-label="Was">{f.original_category ?? "none"}</td>
                <td data-label="Should be">{f.correct_category ?? "none"}</td>
                <td data-label="Note" className="ellipsis">{f.user_note}</td>
                <td data-label="Proposal">
                  <Badge tone={summary.tone}>{summary.label}</Badge>
                </td>
                <td className="row-actions">
                  {canReview && (
                    <button className="primary" onClick={() => setReviewing(f)}>
                      Review
                    </button>
                  )}
                  {needsGenerate && (
                    <AsyncButton
                      onClick={async () => {
                        try {
                          if (targetNeedsGen) {
                            await post(`/feedback/${f.id}/generate-proposal`);
                          }
                          if (sourceNeedsGen) {
                            await post(`/feedback/${f.id}/generate-source-proposal`);
                          }
                          toast.success("Proposal generated");
                        } catch (e) {
                          toast.error(
                            `Generation failed: ${e instanceof Error ? e.message : e}`,
                          );
                        }
                        await load();
                      }}
                    >
                      Generate
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
            );
          })}
          {items.length === 0 && (
            <tr>
              <td colSpan={7} className="sub">
                No open feedback. Flag a misclassified email from the Emails page.
              </td>
            </tr>
          )}
        </tbody>
      </table>
      </div>

      {reviewing && (
        <CombinedProposalReview
          item={reviewing}
          categories={categories}
          onDone={load}
          onClose={() => setReviewing(null)}
        />
      )}
    </div>
  );
}
