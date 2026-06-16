import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { errMsg, post, put } from "../api";
import { AsyncButton, ErrorNote } from "../components";
import { GmailConnect } from "./Settings";
import { useApp } from "../App";

export default function Wizard() {
  const { status, refresh } = useApp();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [llmUrl, setLlmUrl] = useState("");
  const [llmResult, setLlmResult] = useState<string | null>(null);
  const [category, setCategory] = useState({ name: "", criteria_md: "" });
  const [error, setError] = useState<string | null>(null);

  const finish = async () => {
    await put("/settings", { first_run_complete: true });
    await refresh();
    navigate("/");
  };

  const steps = [
    {
      title: "Welcome",
      body: (
        <>
          <p>
            MailTriage polls your Gmail inbox, classifies mail with your local LLM,
            applies rules you define (label / archive / mark read / trash), and can
            send Telegram digests.
          </p>
          <p>
            <b>New rules start in dry-run</b>: their actions are recorded as planned,
            but nothing in your Gmail changes until you switch each rule to live —
            one by one, once you've reviewed what it <i>would</i> do.
          </p>
          <p>
            MailTriage can never send email — it requests the read/organize scope
            only.
          </p>
        </>
      ),
    },
    {
      title: "Step 1 — Connect Gmail",
      body: (
        <>
          <GmailConnect />
          {status?.gmail.connected && <p className="note">Gmail connected ✓</p>}
        </>
      ),
    },
    {
      title: "Step 2 — LLM endpoint",
      body: (
        <>
          <p className="sub">
            Point MailTriage at your llama.cpp server (OpenAI-compatible /v1 API).
            Leave empty to use the LLM_BASE_URL environment variable.
          </p>
          <input
            placeholder="http://host.docker.internal:8081/v1"
            value={llmUrl}
            onChange={(e) => setLlmUrl(e.target.value)}
          />
          <div className="head-actions">
            <AsyncButton
              onClick={async () => {
                setError(null);
                if (llmUrl) await put("/settings", { llm_base_url: llmUrl });
                const r = await post<{ ok: boolean; error?: string; models?: string[] }>(
                  "/llm/test",
                );
                setLlmResult(
                  r.ok ? `OK — models: ${r.models?.join(", ")}` : `Unreachable: ${r.error}`,
                );
              }}
            >
              Save & test
            </AsyncButton>
          </div>
          {llmResult && <p className="note">{llmResult}</p>}
        </>
      ),
    },
    {
      title: "Step 3 — First category",
      body: (
        <>
          <p className="sub">
            Categories are defined by plain-language criteria — that text is the
            classification prompt. Example: "MarketNews: daily/weekly market
            commentary newsletters; stock, bond and macro analysis."
          </p>
          <input
            placeholder="Category name, e.g. MarketNews"
            value={category.name}
            onChange={(e) => setCategory({ ...category, name: e.target.value })}
          />
          <textarea
            rows={6}
            placeholder="Criteria in plain language…"
            value={category.criteria_md}
            onChange={(e) => setCategory({ ...category, criteria_md: e.target.value })}
          />
          <ErrorNote error={error} />
          <AsyncButton
            onClick={async () => {
              setError(null);
              try {
                await post("/categories", category);
                setStep(step + 1);
              } catch (e) {
                setError(errMsg(e));
              }
            }}
            disabled={!category.name}
          >
            Create category
          </AsyncButton>
        </>
      ),
    },
    {
      title: "All set",
      body: (
        <>
          <p>
            MailTriage starts polling and classifying now. Add rules (they start in
            dry-run), review their planned actions on the Emails page, then graduate
            each rule to live from the Rules page when you're confident.
          </p>
        </>
      ),
    },
  ];

  const current = steps[step];

  return (
    <div className="wizard">
      <div className="wizard-card">
        <h2>{current.title}</h2>
        <div className="wizard-body">{current.body}</div>
        <div className="wizard-nav">
          {step > 0 && <button onClick={() => setStep(step - 1)}>‹ Back</button>}
          <span className="spacer" />
          {step < steps.length - 1 ? (
            <>
              <button onClick={() => setStep(step + 1)}>Skip</button>
              <button className="primary" onClick={() => setStep(step + 1)}>
                Next ›
              </button>
            </>
          ) : (
            <button className="primary" onClick={finish}>
              Finish — go to dashboard
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
