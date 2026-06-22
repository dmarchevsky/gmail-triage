"""LLM client: OpenAI-compatible Chat Completions against llama.cpp.

- temperature 0, JSON-schema enforcement via response_format json_schema
  (llama.cpp grammar); strict parsing + exactly one retry on invalid output.
- Bounded concurrency (default 1 — local iGPU serving is effectively serial).
- Email content is untrusted; the only output channel is the constrained
  JSON schema / digest text (spec §6.5).
"""

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.config import get_config
from app.logging_setup import get_logger
from app.state import app_state

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

_semaphore: asyncio.Semaphore | None = None
_semaphore_size: int | None = None


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    """Cannot connect to LLM endpoint — callers should leave work queued."""


class LLMTimeout(LLMError):
    """Request timed out after a successful connection — per-email error, not a global outage."""


class LLMInvalidOutput(LLMError):
    """Output failed schema validation twice."""


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text()


def _get_semaphore(size: int) -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    if _semaphore is None or _semaphore_size != size:
        _semaphore = asyncio.Semaphore(size)
        _semaphore_size = size
    return _semaphore


def resolve_llm_target(settings: dict[str, Any] | None = None) -> tuple[str, str]:
    """(base_url, model) — DB settings override env."""
    cfg = get_config()
    settings = settings or {}
    base_url = settings.get("llm_base_url") or cfg.llm_base_url
    model = settings.get("llm_model") or cfg.llm_model
    return base_url, model


def _client(base_url: str, timeout: float) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url, api_key="not-needed", timeout=timeout,
                       max_retries=0)


async def chat_json(system: str, user: str, schema: dict, schema_name: str,
                    timeout: float, settings: dict[str, Any] | None = None,
                    max_concurrency: int = 1) -> dict:
    """Chat completion that must return JSON matching `schema`.

    Tries response_format=json_schema (llama.cpp enforces by grammar); if the
    server rejects that, falls back to JSON-only prompting. Exactly one retry
    on unparseable/invalid output, then LLMInvalidOutput.
    """
    base_url, model = resolve_llm_target(settings)
    client = _client(base_url, timeout)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response_format: dict | None = {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "schema": schema, "strict": True},
    }
    last_error = ""
    try:
        async with _get_semaphore(max_concurrency):
            for attempt in range(2):
                kwargs: dict = {"model": model, "messages": messages, "temperature": 0}
                if response_format is not None:
                    kwargs["response_format"] = response_format
                try:
                    completion = await client.chat.completions.create(**kwargs)
                except APIStatusError as e:
                    if response_format is not None and e.status_code in (400, 422):
                        # Server doesn't accept json_schema — fall back to prompting.
                        log.info("llm_json_schema_unsupported_falling_back")
                        response_format = None
                        messages[0] = {"role": "system", "content":
                                       system + "\nRespond ONLY with JSON. No prose."}
                        try:
                            completion = await client.chat.completions.create(
                                model=model, messages=messages, temperature=0)
                        except APIStatusError as e2:
                            raise LLMError(
                                f"LLM HTTP {e2.status_code}: {e2.message}") from e2
                    else:
                        raise LLMError(f"LLM HTTP {e.status_code}: {e.message}") from e

                raw = (completion.choices[0].message.content or "").strip()
                app_state.llm_status = "ok"
                parsed = _parse_against_schema(raw, schema)
                if parsed is not None:
                    return parsed
                last_error = f"invalid JSON output: {raw[:200]}"
                log.warning("llm_invalid_output_retrying", attempt=attempt)
                messages.append({"role": "assistant", "content": raw[:2000]})
                messages.append({"role": "user", "content":
                                 "That was not valid JSON matching the schema. "
                                 "Respond ONLY with the JSON object."})
    except APITimeoutError as e:
        raise LLMTimeout(str(e)) from e
    except APIConnectionError as e:
        app_state.llm_status = "unreachable"
        raise LLMUnavailable(str(e)) from e
    finally:
        await client.close()
    raise LLMInvalidOutput(last_error)


_FINAL_LABEL_KWS = ("final version:", "final answer:", "final summary:", "final output:")


def _extract_reasoning_answer(reasoning: str) -> str:
    """Pull the final answer from a thinking model's reasoning_content when content is empty.

    Two common patterns in thinking model output:

    1. Label then content on the next line(s):
         *   *Final Version:*
             Reply to the email with an essay draft...
    2. Summary paragraph followed by a character-count check:
         Wall Street finished the week higher...

         *Character count check:* 628 characters. Perfect.
    """
    lines = reasoning.splitlines()

    # Pattern 1: find a "Final Version / Answer / Summary" label line and take
    # the content on the immediately following non-empty lines.
    for i in range(len(lines) - 1, -1, -1):
        lc = lines[i].strip().lower()
        if any(kw in lc for kw in _FINAL_LABEL_KWS):
            result: list[str] = []
            for j in range(i + 1, len(lines)):
                ln = lines[j].strip()
                if ln and len(ln) > 10:
                    result.append(ln)
                elif result:
                    break
            if result:
                text = " ".join(result)
                if len(text) > 20:
                    return text

    # Pattern 2: scan individual lines from the end; strip bullet markers and
    # unwrap quoted drafts — handles models that iterate through numbered
    # drafts like `*   "The SAT was moderately okay..."` on each line.
    for line in reversed(lines):
        ln = line.strip()
        if not ln or "character count" in ln.lower():
            continue
        # Strip leading bullet / list markers
        core = re.sub(r"^[*\-•]+\s*", "", ln).strip()
        # Skip pure *meta-commentary* lines (italic/bold wrappers with no quotes)
        if re.match(r"^\*[^*\"]+\*\s*$", core):
            continue
        # Unwrap a quoted draft "answer text" → answer text
        core = core.strip('"').strip()
        if len(core) > 30:
            return core
    return ""


async def chat_text(system: str, user: str, timeout: float,
                    settings: dict[str, Any] | None = None,
                    max_concurrency: int = 1,
                    max_tokens: int | None = None) -> str:
    """Plain-text chat completion (digest summaries).

    `max_tokens` bounds generation so a verbose local model can't run a single
    call for minutes — essential when the per-call timeout is large.

    Thinking/reasoning models emit output in reasoning_content rather than
    content when their token budget is consumed by the reasoning phase.  When
    content is empty we fall back to the last substantive paragraph of
    reasoning_content, which is where these models draft their final answer.
    """
    base_url, model = resolve_llm_target(settings)
    client = _client(base_url, timeout)
    kwargs: dict[str, Any] = {}
    if max_tokens is not None and max_tokens > 0:
        kwargs["max_tokens"] = max_tokens
    try:
        async with _get_semaphore(max_concurrency):
            completion = await client.chat.completions.create(
                model=model, temperature=0,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                **kwargs)
        app_state.llm_status = "ok"
        content = (completion.choices[0].message.content or "").strip()
        if not content:
            rc = (completion.choices[0].message.model_extra or {}).get(
                "reasoning_content") or ""
            if rc:
                content = _extract_reasoning_answer(rc)
        return content
    except APITimeoutError as e:
        raise LLMTimeout(str(e)) from e
    except APIConnectionError as e:
        app_state.llm_status = "unreachable"
        raise LLMUnavailable(str(e)) from e
    except APIStatusError as e:
        raise LLMError(f"LLM HTTP {e.status_code}: {e.message}") from e
    finally:
        await client.close()


def _parse_against_schema(raw: str, schema: dict) -> dict | None:
    """Strict-ish validation: JSON object with required keys of correct type."""
    text = raw
    if text.startswith("```"):
        text = text.strip("`\n")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    props: dict = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in data:
            return None
        expected = props.get(key, {}).get("type")
        value = data[key]
        if expected == "string" and not isinstance(value, str):
            return None
        if expected == "number" and not isinstance(value, int | float):
            return None
        if "enum" in props.get(key, {}) and value not in props[key]["enum"]:
            return None
        minimum = props.get(key, {}).get("minimum")
        maximum = props.get(key, {}).get("maximum")
        if isinstance(value, int | float) and not isinstance(value, bool):
            if minimum is not None and value < minimum:
                return None
            if maximum is not None and value > maximum:
                return None
    return data


async def health_probe(settings: dict[str, Any] | None = None,
                       timeout: float = 10) -> dict:
    base_url, model = resolve_llm_target(settings)
    client = _client(base_url, timeout)
    try:
        models = await client.models.list()
        app_state.llm_status = "ok"
        return {"ok": True, "base_url": base_url,
                "models": [m.id for m in models.data][:5], "configured_model": model}
    except Exception as e:  # noqa: BLE001 — any failure means unreachable
        app_state.llm_status = "unreachable"
        return {"ok": False, "base_url": base_url, "error": str(e)[:300]}
    finally:
        await client.close()


async def fetch_context_length(settings: dict[str, Any] | None = None,
                               timeout: float = 5) -> int | None:
    """Detected context window (n_ctx) from the llama.cpp `/props` endpoint.

    `/props` lives at the server root, not under `/v1`, so the trailing `/v1`
    is stripped. Returns None for non-llama.cpp servers or any failure — the
    manual `llm_max_context_tokens` setting stays authoritative in that case.
    """
    base_url, _ = resolve_llm_target(settings)
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{root}/props")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001 — best-effort probe; never raises to caller
        log.info("llm_props_probe_failed", error=str(e)[:200])
        return None
    n_ctx = data.get("n_ctx")
    if n_ctx is None:
        gen = data.get("default_generation_settings") or {}
        n_ctx = gen.get("n_ctx")
    try:
        return int(n_ctx) if n_ctx else None
    except (TypeError, ValueError):
        return None
