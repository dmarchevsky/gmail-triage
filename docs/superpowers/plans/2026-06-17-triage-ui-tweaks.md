# Triage UI tweaks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Surface classification errors in the dashboard activity feed, tidy the dashboard cards, make the mobile Categories view a compact table, and render Emails-list actions as badges.

**Architecture:** One small backend change (a new `classification_failed` audit event emitted at the existing classifier failure sites) plus four focused frontend edits (Dashboard card merge, Categories mobile table CSS, Emails actions badges, activity-feed label/formatter). No migrations, no new settings, no API shape changes.

**Tech Stack:** FastAPI + SQLAlchemy (backend, pytest + respx), React + TypeScript + Vite (frontend, no unit-test framework — `tsc --noEmit` and `vite build` are the gates).

**Commit policy:** `CLAUDE.md` requires all quality gates to pass and **explicit user confirmation** before any commit. Therefore tasks below verify with tests/lint/build but do **not** commit individually; a single feature commit happens in Task 6 after gates pass and the user confirms. The committed change set includes the spec at `docs/superpowers/specs/2026-06-17-triage-ui-tweaks-design.md` and this plan.

Spec: `docs/superpowers/specs/2026-06-17-triage-ui-tweaks-design.md`

---

## File Structure

- `backend/app/services/classifier.py` — add `_audit_classification_failed` helper + emit at 5 failure sites (modify).
- `backend/tests/test_m2_classification.py` — add audit-on-failure test (modify).
- `frontend/src/activity.ts` — add label + formatter for `classification_failed` (modify).
- `frontend/src/pages/Dashboard.tsx` — merge the two stat cards; add abbreviated headers to the precision table (modify).
- `frontend/src/components.tsx` — add `ActionBadges` component (modify).
- `frontend/src/pages/Emails.tsx` — render the Actions column via `ActionBadges` (modify).
- `frontend/src/styles.css` — `.card-rows`, `.action-badges`, `.th-abbr` base rule, precision-table mobile block, Actions-cell mobile prefix suppression (modify).

---

## Task 1: Backend — `classification_failed` audit event

**Files:**
- Modify: `backend/app/services/classifier.py`
- Test: `backend/tests/test_m2_classification.py`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_m2_classification.py` (after `test_llm_down_leaves_pending`, end of file):

```python
@respx.mock
def test_classification_failure_logged_to_audit(auth_client, db_session, seeded, monkeypatch):
    """A classification failure (here: LLM timeout) is recorded in the audit log
    so it surfaces in the dashboard Recent activity feed, not only the email
    detail modal."""
    mock_gmail_full(seeded)
    from app.services import classifier, llm

    async def timeout(*args, **kwargs):
        raise llm.LLMTimeout("deadline exceeded")

    monkeypatch.setattr(classifier.llm, "chat_json", timeout)

    resp = auth_client.post("/api/v1/classify/run-now")
    assert resp.json()["errors"] == 1

    from app.models import AuditLog, Email
    email = db_session.query(Email).one()
    assert email.status == "error"
    row = (db_session.query(AuditLog)
           .filter(AuditLog.event_type == "classification_failed").one())
    assert row.actor == "system"
    assert row.payload["email_id"] == email.id
    assert row.payload["reason"] == "timeout"
    assert "deadline exceeded" in row.payload["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/bin/python -m pytest tests/test_m2_classification.py::test_classification_failure_logged_to_audit -q`
Expected: FAIL — `NoResultFound` (no `classification_failed` row exists yet).

- [ ] **Step 3: Add the audit import and helper**

In `backend/app/services/classifier.py`, add the import alongside the other `app.services` imports (near line 21, `from app.services import llm, settings_service`):

```python
from app.services.audit import audit
```

Add this helper just after the module-level constants (after `_SUMMARY_MAX_TOKENS = {...}`, before `CLASSIFICATION_SCHEMA_TEMPLATE`):

```python
def _audit_classification_failed(session: Session, email: Email,
                                 reason: str, error: str) -> None:
    """Record a classification failure in the audit log so it appears in the
    dashboard Recent activity feed (not only the email detail modal). `reason`
    is a short stable code: timeout | invalid_output | gmail_not_found |
    gmail_auth | stalled."""
    audit(session, "system", "classification_failed",
          {"email_id": email.id, "reason": reason, "error": (error or "")[:300]})
```

- [ ] **Step 4: Emit at the five failure sites**

In `classify_email`, the `GmailNotFound` handler — after setting `email.attempts = max(...)`, before `return`:

```python
    except GmailNotFound:
        email.status = EmailStatus.error.value
        email.error = "Message no longer available in Gmail"
        # A 404 is a deterministic permanent failure — park attempts at the cap
        # so the recovery sweep doesn't retry it (and re-hit Gmail) every cycle.
        email.attempts = max(email.attempts or 0,
                             int(settings.get("classify_max_attempts") or 5))
        _audit_classification_failed(session, email, "gmail_not_found", email.error)
        return
```

In `classify_email`, the LLM result `except` blocks:

```python
    except llm.LLMInvalidOutput as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM output invalid after retry: {e}"
        _audit_classification_failed(session, email, "invalid_output", email.error)
        return
    except llm.LLMTimeout as e:
        email.status = EmailStatus.error.value
        email.error = f"LLM timed out: {e}"
        _audit_classification_failed(session, email, "timeout", email.error)
        return
```

In `_process_next`, the `GmailAuthError` handler:

```python
        try:
            client = GmailClient(session, client_secret)
        except GmailAuthError:
            email.status = EmailStatus.error.value
            email.error = "Gmail auth expired"
            email.processing_started_at = None
            _audit_classification_failed(session, email, "gmail_auth", email.error)
            session.commit()
            return True
```

In `_recover_stalled_emails`, the give-up branch:

```python
        if (e.attempts or 0) >= max_attempts:
            e.status = EmailStatus.error.value
            e.error = f"Stalled in processing; gave up after {e.attempts} attempts"
            _audit_classification_failed(session, e, "stalled", e.error)
            log.warning("stalled_email_failed", email_id=e.id, attempts=e.attempts)
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd backend && .venv/bin/python -m pytest tests/test_m2_classification.py::test_classification_failure_logged_to_audit -q`
Expected: PASS.

- [ ] **Step 6: Run lint + full backend suite (no regressions)**

Run: `cd backend && .venv/bin/ruff check . && .venv/bin/python -m pytest -q`
Expected: ruff clean, 0 failures.

---

## Task 2: Frontend — activity-feed label + formatter

**Files:**
- Modify: `frontend/src/activity.ts`

- [ ] **Step 1: Add the event label**

In `frontend/src/activity.ts`, in the `EVENT_LABELS` map, under the `// poller / classifier` section (after `actions_failed: "Actions failed",`):

```typescript
  classification_failed: "Classification failed",
```

- [ ] **Step 2: Add the detail formatter**

In the `FORMATTERS` map, after the `actions_failed` entry:

```typescript
  classification_failed: (p) =>
    `${emailRef(p)} failed: ${p.error ?? "unknown error"}`,
```

`emailRef` is already defined in this file and resolves the email subject/sender from the payload (the backend enriches `email_id` → `email_from`/`email_subject` in `/stats`). The `system` actor already renders neutral-toned in `Dashboard.tsx`.

- [ ] **Step 3: Verify the build**

Run: `cd frontend && npm run lint && npm run build`
Expected: 0 type errors, build succeeds.

---

## Task 3: Frontend — merge Today + Last-7-days into one card

**Files:**
- Modify: `frontend/src/pages/Dashboard.tsx` (lines 107-125)
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Replace the two cards with one**

In `frontend/src/pages/Dashboard.tsx`, replace the two `<div className="card">` blocks for "Today" and "Last 7 days" (lines 108-125) with a single card. The result (the Engine card that follows is unchanged):

```tsx
            <div className="card">
              <h4>Processed</h4>
              <div className="card-rows">
                <div className="card-row">
                  <span>Today</span>
                  <span className="num">{stats.today.processed}</span>
                </div>
                <div className="card-row">
                  <span>Last 7 days</span>
                  <span className="num">{stats.week.processed}</span>
                </div>
              </div>
            </div>
```

This removes the executed and planned (dry-run) counts entirely (per the approved design — dry-run visibility remains in the top banner and the Emails list).

- [ ] **Step 2: Add the card-rows styles**

In `frontend/src/styles.css`, immediately after the `.card .big { ... }` rule (around line 382):

```css
.card-rows {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.3rem;
}

.card-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.75rem;
}

.card-row > span:first-child {
  color: var(--muted);
}

.card-row .num {
  font-size: 1.5rem;
  font-weight: 700;
}
```

- [ ] **Step 3: Verify the build**

Run: `cd frontend && npm run lint && npm run build`
Expected: 0 type errors, build succeeds.

- [ ] **Step 4: Visual check (manual)**

Load the dashboard; confirm one "Processed" card with two rows (Today / Last 7 days), no "executed" text, and the Engine card beside it.

---

## Task 4: Frontend — mobile Categories table (no horizontal scroll)

**Files:**
- Modify: `frontend/src/pages/Dashboard.tsx` (precision-table `<thead>`, lines 218-225)
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Give each precision-table header a full + abbreviated label**

In `frontend/src/pages/Dashboard.tsx`, replace the precision table's `<thead>` (lines 218-226) with:

```tsx
              <thead>
                <tr>
                  <th><span className="th-full">Category</span><span className="th-abbr">Cat.</span></th>
                  <th><span className="th-full">Classified (1d)</span><span className="th-abbr">1d</span></th>
                  <th><span className="th-full">Classified (7d)</span><span className="th-abbr">7d</span></th>
                  <th><span className="th-full">Flagged wrong (7d)</span><span className="th-abbr">Wrong</span></th>
                  <th><span className="th-full">Precision (7d)</span><span className="th-abbr">Prec.</span></th>
                </tr>
              </thead>
```

- [ ] **Step 2: Hide the abbreviated labels on desktop (base rule)**

In `frontend/src/styles.css`, add near the table base rules (after the `.table th { ... }` block, around line 420):

```css
/* Mobile-only abbreviated column headers (see precision-table mobile block). */
.th-abbr {
  display: none;
}
```

- [ ] **Step 3: Keep the precision table a compact real table on mobile**

In `frontend/src/styles.css`, append this block at the **end** of the existing `@media (max-width: 720px)` block that starts at line 1116 (i.e. as the last rules before that media query's closing `}`). The doubled `.precision-table.precision-table` selector plus end-of-query placement guarantees these win over the generic `.table` stacked-card rules above without editing them:

```css
  /* Categories (precision) table stays a compact, full-width table on mobile —
     abbreviated headers + small type instead of collapsing into stacked cards,
     and it must fit the viewport with no horizontal scroll. The doubled class
     beats the generic .table collapse rules above. */
  .precision-table.precision-table {
    display: table;
    width: 100%;
    table-layout: fixed;
    border: 1px solid var(--border);
    background: var(--card);
    font-size: 0.72rem;
  }
  .precision-table.precision-table thead {
    display: table-header-group;
  }
  .precision-table.precision-table tbody {
    display: table-row-group;
  }
  .precision-table.precision-table tr {
    display: table-row;
    border: none;
    background: transparent;
    margin: 0;
    padding: 0;
    border-radius: 0;
  }
  .precision-table.precision-table th,
  .precision-table.precision-table td {
    display: table-cell;
    border: none;
    border-bottom: 1px solid var(--border);
    padding: 0.3rem 0.35rem;
    font-size: 0.72rem;
    font-weight: normal;
  }
  .precision-table.precision-table td::before {
    content: none;
  }
  .precision-table.precision-table .th-full {
    display: none;
  }
  .precision-table.precision-table .th-abbr {
    display: inline;
  }
  .precision-table.precision-table td:first-child,
  .precision-table.precision-table th:first-child {
    white-space: normal;
    text-align: left;
    padding-right: 0.35rem;
  }
  .precision-table.precision-table td:not(:first-child),
  .precision-table.precision-table th:not(:first-child) {
    text-align: right;
    white-space: nowrap;
  }
  .precision-table.precision-table tr:last-child td {
    border-bottom: none;
  }
```

- [ ] **Step 4: Verify the build**

Run: `cd frontend && npm run lint && npm run build`
Expected: 0 type errors, build succeeds.

- [ ] **Step 5: Visual check (manual, mobile width)**

In dev tools at ~360px width, confirm the Categories section renders as a 5-column table with abbreviated headers (Cat./1d/7d/Wrong/Prec.), no horizontal scrollbar, category names wrapping, numbers right-aligned. Other tables (Emails, Recent activity) still collapse to cards.

---

## Task 5: Frontend — Emails Actions column as badges

**Files:**
- Modify: `frontend/src/components.tsx`
- Modify: `frontend/src/pages/Emails.tsx` (lines 521-525)
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add the `ActionBadges` component**

In `frontend/src/components.tsx`, change the first import line to also import the `EmailAction` type, and add a tone map + component.

Change line 1 from:

```tsx
import { ReactNode, useEffect, useState } from "react";
```

to:

```tsx
import { ReactNode, useEffect, useState } from "react";
import { EmailAction } from "./api";
```

Then add, just after the `LabelPill` component (after its closing `}` around line 73):

```tsx
// Non-label action → badge tone. Labels are rendered as colored LabelPills;
// every other action gets a distinct, tone-coded badge.
const ACTION_TONE: Record<string, "ok" | "warn" | "error" | "neutral" | "info"> = {
  archive: "info",
  mark_read: "neutral",
  trash: "error",
  remove_label: "warn",
};

// Render an email's actions as badges: add_label → colored LabelPill, all other
// actions → tone-coded Badge. Deduped by type+label so an email actioned by
// several rules doesn't show repeated identical badges.
export function ActionBadges({ actions }: { actions: EmailAction[] }) {
  if (actions.length === 0) return <>—</>;
  const seen = new Set<string>();
  const pills: ReactNode[] = [];
  const badges: ReactNode[] = [];
  for (const a of actions) {
    const lname = a.action_params?.label_name as string | undefined;
    const key = `${a.action_type}:${lname ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (a.action_type === "add_label" && lname) {
      pills.push(
        <LabelPill
          key={key}
          name={lname}
          textColor={a.action_params?.text_color as string | null}
          backgroundColor={a.action_params?.background_color as string | null}
        />,
      );
    } else if (a.action_type === "remove_label" && lname) {
      badges.push(<Badge key={key} tone="warn">Remove: {lname}</Badge>);
    } else {
      badges.push(
        <Badge key={key} tone={ACTION_TONE[a.action_type] ?? "neutral"}>
          {actionLabel(a.action_type)}
        </Badge>,
      );
    }
  }
  return <span className="action-badges">{pills}{badges}</span>;
}
```

- [ ] **Step 2: Use it in the Emails list**

In `frontend/src/pages/Emails.tsx`, update the import on line 3 to include `ActionBadges`:

```tsx
import { AsyncButton, Badge, BulkActionBar, ConfirmDialog, LabelPill, Modal, ActionBadges, actionLabel, conf, fmtDate } from "../components";
```

Replace the Actions `<td>` (lines 521-525):

```tsx
              <td data-label="Actions">{e.actions.map((a) => {
                const base = actionLabel(a.action_type);
                const lname = a.action_params?.label_name as string | undefined;
                return lname ? `${base} → ${lname}` : base;
              }).join(", ") || "—"}</td>
```

with:

```tsx
              <td data-label="Actions"><ActionBadges actions={e.actions} /></td>
```

Note: `actionLabel` is still imported and used elsewhere in this file (the EmailDetail modal at lines 166 and 172), so leave the import in place.

- [ ] **Step 3: Add the `.action-badges` layout + mobile prefix suppression**

In `frontend/src/styles.css`, add near the `.label-pill` rule (around line 870):

```css
.action-badges {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  align-items: center;
}
```

Then, in the `@media (max-width: 720px)` block, extend the "badge cells read fine without a label" group (currently lines 1238-1244) to include the Actions cell so the `Actions:` prefix is suppressed on mobile:

```css
  /* badge cells read fine without a label */
  .emails-table td[data-label="Status"]::before,
  .emails-table td[data-label="Actions"]::before,
  .rules-table td[data-label="Mode"]::before,
  .rules-table td[data-label="Enabled"]::before,
  .categories-table td[data-label="Enabled"]::before,
  .digests-table td[data-label="Enabled"]::before {
    display: none;
  }
```

- [ ] **Step 4: Verify the build**

Run: `cd frontend && npm run lint && npm run build`
Expected: 0 type errors, build succeeds. (Confirms no circular-import / unused-import issues — the `EmailAction` import in `components.tsx` is type-only and erased at build.)

- [ ] **Step 5: Visual check (manual)**

In the Emails list, confirm label actions show as colored pills and archive/mark-read/trash/remove-label show as tone-coded badges, deduped, on both desktop and mobile (no `Actions:` prefix on mobile).

---

## Task 6: Full quality gates, Docker boot, and feature commit

**Files:** none (verification + commit only)

- [ ] **Step 1: Backend gates**

Run: `cd backend && .venv/bin/ruff check . && .venv/bin/python -m pytest -q`
Expected: ruff clean, 0 test failures.

- [ ] **Step 2: Frontend gates**

Run: `cd frontend && npm run lint && npm run build`
Expected: 0 type errors, build succeeds.

- [ ] **Step 3: Docker boot check**

Run:
```bash
docker compose build && docker compose up -d
curl -fsS http://localhost:8080/api/v1/status
docker compose logs --tail=25 mailtriage
```
Expected: HTTP 200 from `/status`; logs show `startup_complete`.

- [ ] **Step 4: Commit (after explicit user confirmation)**

Per `CLAUDE.md`, confirm with the user first. Then:

```bash
git add backend/app/services/classifier.py backend/tests/test_m2_classification.py \
        frontend/src/activity.ts frontend/src/pages/Dashboard.tsx \
        frontend/src/pages/Emails.tsx frontend/src/components.tsx frontend/src/styles.css \
        docs/superpowers/specs/2026-06-17-triage-ui-tweaks-design.md \
        docs/superpowers/plans/2026-06-17-triage-ui-tweaks.md
git commit -m "$(cat <<'EOF'
UI: classification errors in activity feed, merged Processed card, mobile category table, action badges

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 5: After pushing (if the user asks to push), verify CI is green**

Run: `gh run list --limit 1` then `gh run watch <id>` — a red build blocks shipping.

---

## Self-review notes

- **Spec coverage:** §1 → Task 1 (backend) + Task 2 (frontend label/formatter); §2 → Task 3; §3 → Task 4; §4 → Task 5; §5 (digests) is proposal-only and intentionally has no task. Quality-gates section → Task 6.
- **Type consistency:** `classification_failed` event_type and payload keys (`email_id`, `reason`, `error`) match between `_audit_classification_failed`, the test, and the `activity.ts` formatter. `ActionBadges` uses the existing `EmailAction` type and `Badge` tone union (`info`/`neutral`/`error`/`warn` all valid). `.th-abbr`/`.th-full` class names match between `Dashboard.tsx` and `styles.css`.
- **No frontend unit tests:** the repo has no frontend test framework; `tsc --noEmit` + `vite build` + manual visual checks are the verification, consistent with `CLAUDE.md`.
