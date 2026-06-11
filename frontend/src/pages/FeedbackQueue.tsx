import { useCallback, useEffect, useState } from "react";
import { FeedbackItem, get } from "../api";
import { Badge, fmtDate } from "../components";

export default function FeedbackQueue() {
  const [items, setItems] = useState<FeedbackItem[]>([]);

  const load = useCallback(
    () => get<FeedbackItem[]>("/feedback?status=open").then(setItems),
    [],
  );
  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <header className="page-head">
        <h2>Feedback queue</h2>
      </header>
      <p className="sub">
        Misclassification feedback collects here. LLM-proposed criteria revisions
        (with approve/edit/reject) arrive in milestone M6.
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>When</th>
            <th>Email</th>
            <th>Was</th>
            <th>Should be</th>
            <th>Note</th>
            <th>Proposal</th>
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
                <Badge>{f.proposal_status}</Badge>
              </td>
            </tr>
          ))}
          {items.length === 0 && (
            <tr>
              <td colSpan={6} className="sub">
                No open feedback. Flag a misclassified email from the Emails page.
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
