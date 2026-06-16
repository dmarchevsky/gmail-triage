import { useEffect, useRef, useState } from "react";
import { LlmQueue, Stats, get, post } from "../api";
import { AsyncButton, Badge, fmtDate } from "../components";
import { describeActivity, eventLabel } from "../activity";
import { useApp } from "../App";
import { useToast } from "../toast";

const ACTOR_TONE: Record<string, "info" | "neutral"> = {
  user: "info",
  system: "neutral",
  scheduler: "neutral",
};

export default function Dashboard() {
  const { status, refresh } = useApp();
  const toast = useToast();
  const [stats, setStats] = useState<Stats | null>(null);
  const [queue, setQueue] = useState<LlmQueue | null>(null);

  const load = () => get<Stats>("/stats").then(setStats);
  const loadQueue = () => get<LlmQueue>("/llm/queue").then(setQueue);

  // Surface poller errors as a transient toast (and rely on Recent activity for
  // the durable record) instead of a persistent page banner. Fire only when the
  // error string changes, not on every 15s status refresh.
  const lastErrSeen = useRef<string | null>(null);
  useEffect(() => {
    const err = status?.poller.last_error ?? null;
    if (err && err !== lastErrSeen.current) toast.error(`Poller error: ${err}`);
    lastErrSeen.current = err;
  }, [status?.poller.last_error, toast]);

  useEffect(() => {
    load();
    const id = setInterval(load, 20000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    loadQueue();
    const id = setInterval(loadQueue, 5000); // live view of in-flight LLM work
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
      {stats && (
        <>
          <div className="cards">
            <div className="card">
              <h4>Today</h4>
              <div className="big">{stats.today.processed}</div>
              <div className="sub">
                processed · {stats.today.actions_executed} executed
                {stats.today.actions_planned_dry_run > 0 &&
                  ` · ${stats.today.actions_planned_dry_run} planned (dry-run)`}
              </div>
            </div>
            <div className="card">
              <h4>Last 7 days</h4>
              <div className="big">{stats.week.processed}</div>
              <div className="sub">
                processed · {stats.week.actions_executed} executed
                {stats.week.actions_planned_dry_run > 0 &&
                  ` · ${stats.week.actions_planned_dry_run} planned (dry-run)`}
              </div>
            </div>
            <div className="card">
              <h4>Engine</h4>
              <div className="status-row">
                <span>Poller</span>
                <Badge tone={status?.poller.paused ? "warn" : "ok"}>
                  {status?.poller.paused ? "paused" : status?.poller.status}
                </Badge>
              </div>
              <div className="sub">last run: {fmtDate(status?.poller.last_run_at)}</div>
              <div className="status-row">
                <span>Classifier</span>
                {status?.classifier.running ? (
                  <Badge tone="info">classifying…</Badge>
                ) : (
                  <Badge tone="neutral">idle</Badge>
                )}
              </div>
              <div className="sub">
                <span className={status?.classifier.running ? "pulse" : ""}>
                  {status?.classifier.pending_emails ?? 0}
                </span>{" "}
                pending
              </div>
            </div>
          </div>

          <h3>LLM queue</h3>
          <p className="sub">
            Live view of work hitting the local LLM (served serially). Refreshes
            every 5s.
          </p>
          {queue &&
          queue.processing.length === 0 &&
          queue.digests.length === 0 &&
          queue.pending === 0 ? (
            <p className="sub">Idle — nothing queued.</p>
          ) : (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>Type</th>
                    <th>Item</th>
                    <th>State</th>
                  </tr>
                </thead>
                <tbody>
                  {queue?.digests.map((d) => (
                    <tr key={`d${d.run_id}`}>
                      <td data-label="Type">Digest</td>
                      <td data-label="Item">{d.name}</td>
                      <td data-label="State">
                        <Badge tone="info">summarizing</Badge>{" "}
                        <span className="sub">{fmtDate(d.started_at)}</span>
                      </td>
                    </tr>
                  ))}
                  {queue?.processing.map((e) => (
                    <tr key={`e${e.id}`}>
                      <td data-label="Type">Classify</td>
                      <td data-label="Item">
                        {e.subject || e.sender || `#${e.id}`}
                      </td>
                      <td data-label="State">
                        <Badge tone="info">classifying</Badge>
                      </td>
                    </tr>
                  ))}
                  {queue && queue.pending > 0 && (
                    <tr>
                      <td data-label="Type">Classify</td>
                      <td data-label="Item">
                        <span className="sub">{queue.pending} email(s) waiting</span>
                      </td>
                      <td data-label="State">
                        <Badge tone="neutral">queued</Badge>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          <h3>Categories</h3>
          <p className="sub">
            Classified counts plus precision from your feedback. LLM confidence is
            self-reported and uncalibrated — use these empirical counts to tune rule
            confidence thresholds.
          </p>
          <div className="table-scroll">
            <table className="table precision-table">
              <thead>
                <tr>
                  <th>Category</th>
                  <th>Classified (1d)</th>
                  <th>Classified (7d)</th>
                  <th>Flagged wrong (7d)</th>
                  <th>Precision (7d)</th>
                </tr>
              </thead>
              <tbody>
                {stats.category_precision.length === 0 && (
                  <tr>
                    <td colSpan={5} className="sub">
                      none yet
                    </td>
                  </tr>
                )}
                {[...stats.category_precision]
                  .sort((a, b) => b.classified_7d - a.classified_7d)
                  .map((p) => (
                  <tr key={p.category_id}>
                    <td data-label="Category">{p.category}</td>
                    <td data-label="Classified (1d)">{p.classified_1d}</td>
                    <td data-label="Classified (7d)">{p.classified_7d}</td>
                    <td data-label="Flagged wrong (7d)">{p.flagged_wrong_7d}</td>
                    <td data-label="Precision (7d)">
                      {p.precision_7d == null ? "—" : `${Math.round(p.precision_7d * 100)}%`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <h3>Recent activity</h3>
          <div className="table-scroll">
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
                {stats.recent_activity.length === 0 && (
                  <tr>
                    <td colSpan={4} className="sub">
                      no activity yet
                    </td>
                  </tr>
                )}
                {stats.recent_activity.map((a, i) => (
                  <tr key={i}>
                    <td data-label="Time">{fmtDate(a.ts)}</td>
                    <td data-label="Actor">
                      <Badge tone={ACTOR_TONE[a.actor] ?? "neutral"}>{a.actor}</Badge>
                    </td>
                    <td data-label="Event">{eventLabel(a.event_type)}</td>
                    <td data-label="Detail">{describeActivity(a.event_type, a.payload)}</td>
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
