import { useEffect, useState } from "react";
import { Stats, get, post } from "../api";
import { AsyncButton, Badge, fmtDate } from "../components";
import { useApp } from "../App";
import { useToast } from "../toast";

export default function Dashboard() {
  const { status, refresh } = useApp();
  const toast = useToast();
  const [stats, setStats] = useState<Stats | null>(null);

  const load = () => get<Stats>("/stats").then(setStats);
  useEffect(() => {
    load();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, []);

  return (
    <div>
      <header className="page-head">
        <h2>Dashboard</h2>
        <div className="head-actions">
          <AsyncButton
            onClick={async () => {
              try {
                const r = await post<{ new_emails: number }>("/poller/run-now");
                toast.success(`Poll done: ${r.new_emails} new email(s)`);
              } catch (e) {
                toast.error(`Poll failed: ${e instanceof Error ? e.message : e}`);
              }
              await Promise.all([refresh(), load()]);
            }}
          >
            Poll now
          </AsyncButton>
          <AsyncButton
            onClick={async () => {
              try {
                const r = await post<{ classified: number; actioned: number }>(
                  "/classify/run-now",
                );
                toast.success(`Classified ${r.classified}, actioned ${r.actioned}`);
              } catch (e) {
                toast.error(`Classify failed: ${e instanceof Error ? e.message : e}`);
              }
              await Promise.all([refresh(), load()]);
            }}
          >
            Classify now
          </AsyncButton>
        </div>
      </header>
      {(status?.rules_mode.dry ?? 0) > 0 && (
        <p className="dry-run-banner">
          {status?.rules_mode.dry} rule(s) in <b>dry-run</b> — their actions are
          recorded as planned but not executed. Graduate them to live one by one
          from the Rules page.
        </p>
      )}
      {status?.poller.last_error && (
        <p className="error">Last poller error: {status.poller.last_error}</p>
      )}

      {stats && (
        <>
          <div className="cards">
            <div className="card">
              <h4>Today</h4>
              <div className="big">{stats.today.processed}</div>
              <div className="sub">
                processed · {stats.today.actions_executed} executed ·{" "}
                {stats.today.actions_planned_dry_run} planned (dry-run)
              </div>
            </div>
            <div className="card">
              <h4>Last 7 days</h4>
              <div className="big">{stats.week.processed}</div>
              <div className="sub">
                processed · {stats.week.actions_executed} executed ·{" "}
                {stats.week.actions_planned_dry_run} planned (dry-run)
              </div>
            </div>
            <div className="card">
              <h4>By category (7d)</h4>
              {stats.by_category.length === 0 && <div className="sub">none yet</div>}
              {stats.by_category.map((c) => (
                <div key={c.category} className="cat-count">
                  <span>{c.category}</span>
                  <b>{c.count}</b>
                </div>
              ))}
            </div>
            <div className="card">
              <h4>Poller</h4>
              <div className="sub">
                last run: {fmtDate(status?.poller.last_run_at)}
                <br />
                status:{" "}
                <Badge tone={status?.poller.paused ? "warn" : "ok"}>
                  {status?.poller.paused ? "paused" : status?.poller.status}
                </Badge>
              </div>
            </div>
            <div className="card">
              <h4>Classifier</h4>
              {status?.classifier.running ? (
                <>
                  <div className="big pulse">{status.classifier.pending_emails}</div>
                  <div className="sub">
                    pending · <Badge tone="warn">classifying…</Badge>
                  </div>
                </>
              ) : (
                <>
                  <div className="big">{status?.classifier.pending_emails ?? 0}</div>
                  <div className="sub">
                    pending · <Badge tone="ok">idle</Badge>
                  </div>
                </>
              )}
            </div>
          </div>

          {stats.category_precision.some((p) => p.flagged_wrong > 0) && (
            <>
              <h3>Category precision (from your feedback)</h3>
              <p className="sub">
                LLM confidence is self-reported and uncalibrated — use these
                empirical counts to tune rule confidence thresholds.
              </p>
              <div className="table-scroll wide">
<table className="table precision-table">
                <thead>
                  <tr>
                    <th>Category</th>
                    <th>Classified</th>
                    <th>Flagged wrong</th>
                    <th>Precision</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.category_precision.map((p) => (
                    <tr key={p.category_id}>
                      <td data-label="Category">{p.category}</td>
                      <td data-label="Classified">{p.classified_total}</td>
                      <td data-label="Flagged wrong">{p.flagged_wrong}</td>
                      <td data-label="Precision">{p.precision == null ? "—" : `${Math.round(p.precision * 100)}%`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
</div>
            </>
          )}

          <h3>Recent activity</h3>
          <div className="table-scroll wide">
<table className="table activity-table">
            <thead>
              <tr>
                <th>Time</th>
                <th>Actor</th>
                <th>Event</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {stats.recent_activity.map((a, i) => (
                <tr key={i}>
                  <td data-label="Time">{fmtDate(a.ts)}</td>
                  <td data-label="Actor">{a.actor}</td>
                  <td data-label="Event">{a.event_type}</td>
                  <td data-label="Detail" className="payload">{JSON.stringify(a.payload)}</td>
                </tr>
              ))}
            </tbody>
          </table>
</div>
        </>
      )}
    </div>
  );
}
