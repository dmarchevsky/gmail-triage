import { useEffect, useState } from "react";
import { Stats, get, post } from "../api";
import { AsyncButton, Badge, fmtDate } from "../components";
import { useApp } from "../App";

export default function Dashboard() {
  const { status, refresh } = useApp();
  const [stats, setStats] = useState<Stats | null>(null);
  const [note, setNote] = useState<string | null>(null);

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
              setNote(null);
              try {
                const r = await post<{ new_emails: number }>("/poller/run-now");
                setNote(`Poll done: ${r.new_emails} new email(s)`);
              } catch (e) {
                setNote(`Poll failed: ${e instanceof Error ? e.message : e}`);
              }
              await Promise.all([refresh(), load()]);
            }}
          >
            Poll now
          </AsyncButton>
          <AsyncButton
            onClick={async () => {
              setNote(null);
              try {
                const r = await post<{ classified: number; actioned: number }>(
                  "/classify/run-now",
                );
                setNote(`Classified ${r.classified}, actioned ${r.actioned}`);
              } catch (e) {
                setNote(`Classify failed: ${e instanceof Error ? e.message : e}`);
              }
              await Promise.all([refresh(), load()]);
            }}
          >
            Classify now
          </AsyncButton>
        </div>
      </header>
      {note && <p className="note">{note}</p>}
      {status?.dry_run && (
        <p className="dry-run-banner">
          Dry-run is <b>ON</b> — the pipeline runs fully, but no Gmail changes are
          made and digests are not sent. Planned actions are recorded for review.
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
          </div>

          <h3>Recent activity</h3>
          <table className="table">
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
                  <td>{fmtDate(a.ts)}</td>
                  <td>{a.actor}</td>
                  <td>{a.event_type}</td>
                  <td className="payload">{JSON.stringify(a.payload)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
