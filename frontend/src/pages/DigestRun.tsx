import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ExternalLink } from "lucide-react";
import { ApiError, DigestItem, DigestRun, errMsg, get, post } from "../api";
import { useApp } from "../App";
import { Badge, fmtDate, runTone } from "../components";

function ItemCard({ item }: { item: DigestItem }) {
  return (
    <article className="digest-item">
      <h4>
        {item.gmail_url ? (
          <a href={item.gmail_url} target="_blank" rel="noopener noreferrer">
            {item.subject} <ExternalLink size={12} />
          </a>
        ) : (
          item.subject
        )}
      </h4>
      <p className="sub">
        {item.sender}
        {item.received_at && <> · {fmtDate(item.received_at)}</>}
        {item.category && <> · {item.category}</>}
      </p>
      {item.summary && <p className="digest-item-summary">{item.summary}</p>}
    </article>
  );
}

/** One digest run, rendered from its persisted content snapshot. */
export default function DigestRunPage() {
  const { runId } = useParams();
  const { refreshUnread } = useApp();
  const [run, setRun] = useState<DigestRun | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setRun(null);
    setError(null);
    get<DigestRun>(`/digests/runs/${runId}`)
      .then(async (r) => {
        if (cancelled) return;
        setRun(r);
        if (r.notified_inapp && !r.read_at) {
          await post(`/digests/runs/${r.id}/read`);
          refreshUnread();
        }
      })
      .catch((e) => {
        if (!cancelled)
          setError(e instanceof ApiError && e.status === 404 ? "Digest not found." : errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, refreshUnread]);

  const content = run?.content;
  return (
    <div className="digest-run">
      <header className="page-head">
        <h2>
          <Link to="/digests" className="icon-btn" title="Back to digests" aria-label="Back">
            <ArrowLeft size={18} />
          </Link>{" "}
          {run?.digest_name ?? content?.title ?? "Digest"}
        </h2>
      </header>
      {error && <p className="error">{error}</p>}
      {!run && !error && <p className="sub">Loading…</p>}
      {run && (
        <>
          <p className="digest-run-meta">
            {fmtDate(run.started_at)} <Badge tone={runTone(run.status)}>{run.status}</Badge>{" "}
            <span className="sub">
              {content?.email_count ?? run.email_ids.length} email
              {(content?.email_count ?? run.email_ids.length) === 1 ? "" : "s"}
            </span>
          </p>
          {run.error && <p className="error">{run.error}</p>}

          {content ? (
            <>
              {content.items.length === 0 && <p className="sub">No news — nothing new arrived.</p>}
              {content.mode === "synthesize" && (content.headline || content.body) && (
                <section className="digest-synthesis">
                  {content.headline && <p className="digest-headline">{content.headline}</p>}
                  {content.body && <div className="digest-summary">{content.body}</div>}
                </section>
              )}
              {content.mode === "synthesize" && content.items.length > 0 ? (
                <details className="digest-sources">
                  <summary>Emails included ({content.items.length})</summary>
                  {content.items.map((i) => (
                    <ItemCard key={i.email_id} item={i} />
                  ))}
                </details>
              ) : (
                content.items.map((i) => <ItemCard key={i.email_id} item={i} />)
              )}
            </>
          ) : (
            run.summary_text && <div className="digest-summary">{run.summary_text}</div>
          )}
        </>
      )}
    </div>
  );
}
