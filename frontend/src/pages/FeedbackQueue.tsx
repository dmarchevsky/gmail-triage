import { useCallback, useEffect, useState } from "react";
import {
  Category,
  FeedbackItem,
  ProposalKind,
  approveProposal,
  errMsg,
  get,
  post,
  rejectProposal,
} from "../api";
import { AsyncButton, Badge, DiffView, Modal, fmtDate } from "../components";
import { useToast } from "../toast";

function ProposalReview({
  item,
  kind,
  categories,
  onDone,
  onClose,
}: {
  item: FeedbackItem;
  kind: ProposalKind;
  categories: Category[];
  onDone: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const isSource = kind === "source";
  const criteriaMd = isSource ? item.proposed_source_criteria_md : item.proposed_criteria_md;
  const explanation = isSource ? item.proposal_source_explanation : item.proposal_explanation;
  const coversCount = isSource ? item.source_covers_count : item.covers_count;
  const [edited, setEdited] = useState(criteriaMd ?? "");

  const reviewedCategory = categories.find(
    (c) =>
      c.id ===
      (isSource
        ? item.source_category_id
        : (item.correct_category_id ??
          categories.find((x) => x.name === item.original_category)?.id)),
  );

  const approve = async () => {
    try {
      await approveProposal(item.id, editing ? edited : undefined, kind);
      toast.success("Criteria updated");
      onDone();
      onClose();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };
  const reject = async () => {
    try {
      await rejectProposal(item.id, kind);
      toast.success("Proposal rejected");
      onDone();
      onClose();
    } catch (e) {
      toast.error(errMsg(e));
    }
  };

  return (
    <Modal
      title={`Proposal — ${reviewedCategory?.name ?? "?"} (${isSource ? "exclusion" : "inclusion"})`}
      onClose={onClose}
      wide
    >
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
      {isSource && (
        <p className="note">
          This revises <b>{item.source_category ?? "?"}</b>'s criteria to exclude mail
          like this one, so it stops winning over the correct category.
        </p>
      )}
      {(coversCount ?? 0) > 1 && (
        <p className="note">
          This consolidated proposal considers <b>{coversCount}</b> feedback
          items for this category — approving incorporates them all at once.
        </p>
      )}
      {explanation && (
        <p className="rationale">
          <b>LLM explanation:</b> {explanation}
        </p>
      )}

      <h4>Criteria change (current → proposed)</h4>
      {editing ? (
        <textarea rows={12} value={edited} onChange={(e) => setEdited(e.target.value)} />
      ) : (
        <DiffView
          oldText={reviewedCategory?.criteria_md ?? ""}
          newText={criteriaMd ?? ""}
        />
      )}
      <div className="modal-actions">
        <button onClick={reject}>Reject</button>
        {editing ? (
          <button className="primary" onClick={approve}>
            Approve edited version
          </button>
        ) : (
          <>
            <button onClick={() => setEditing(true)}>Edit then approve</button>
            <button className="primary" onClick={approve}>
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
  const [reviewing, setReviewing] = useState<{ item: FeedbackItem; kind: ProposalKind } | null>(
    null,
  );
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
          {items.map((f) => (
            <tr key={f.id}>
              <td data-label="When">{fmtDate(f.created_at)}</td>
              <td data-label="Email" className="ellipsis">{f.email_subject}</td>
              <td data-label="Was">{f.original_category ?? "none"}</td>
              <td data-label="Should be">{f.correct_category ?? "none"}</td>
              <td data-label="Note" className="ellipsis">{f.user_note}</td>
              <td data-label="Proposal">
                {f.merged_into ? (
                  <Badge tone="neutral">merged into review</Badge>
                ) : (
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
                    {f.proposal_status === "pending_review" &&
                      (f.covers_count ?? 0) > 1 &&
                      ` · covers ${f.covers_count}`}
                  </Badge>
                )}
                {f.source_category && f.proposal_source_status !== "none" && (
                  f.source_merged_into ? (
                    <Badge tone="neutral">source fix merged</Badge>
                  ) : (
                    <Badge
                      tone={
                        f.proposal_source_status === "pending_review"
                          ? "info"
                          : f.proposal_source_status === "rejected"
                            ? "error"
                            : "neutral"
                      }
                    >
                      Source fix: {f.proposal_source_status}
                      {f.proposal_source_status === "pending_review" &&
                        (f.source_covers_count ?? 0) > 1 &&
                        ` · covers ${f.source_covers_count}`}
                    </Badge>
                  )
                )}
              </td>
              <td className="row-actions">
                {f.merged_into ? (
                  <span className="sub">in pending review</span>
                ) : f.proposal_status === "pending_review" ? (
                  <button className="primary" onClick={() => setReviewing({ item: f, kind: "target" })}>
                    Review
                  </button>
                ) : (
                  <AsyncButton
                    onClick={async () => {
                      try {
                        await post(`/feedback/${f.id}/generate-proposal`);
                        toast.success("Proposal generated");
                      } catch (e) {
                        toast.error(
                          `Generation failed: ${e instanceof Error ? e.message : e}`,
                        );
                      }
                      await load();
                    }}
                  >
                    Generate now
                  </AsyncButton>
                )}
                {f.source_category && !f.source_merged_into && (
                  f.proposal_source_status === "pending_review" ? (
                    <button onClick={() => setReviewing({ item: f, kind: "source" })}>
                      Review source fix
                    </button>
                  ) : f.proposal_source_status !== "approved" ? (
                    <AsyncButton
                      onClick={async () => {
                        try {
                          await post(`/feedback/${f.id}/generate-source-proposal`);
                          toast.success("Source fix proposal generated");
                        } catch (e) {
                          toast.error(
                            `Generation failed: ${e instanceof Error ? e.message : e}`,
                          );
                        }
                        await load();
                      }}
                    >
                      Generate source fix
                    </AsyncButton>
                  ) : null
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
      </div>

      {reviewing && (
        <ProposalReview
          item={reviewing.item}
          kind={reviewing.kind}
          categories={categories}
          onDone={load}
          onClose={() => setReviewing(null)}
        />
      )}
    </div>
  );
}
