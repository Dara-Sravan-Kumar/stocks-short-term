"""LLM news sentiment via the Claude Code CLI (`claude -p`).

Runs on the user's Claude subscription — no Anthropic API billing. Batched
(~12 tickers per call), cached daily in sentiment_log, and degrades to
neutral 0.0 scores if the CLI is unavailable or output can't be parsed.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess

import config
from stockbot import db

SYSTEM_INSTRUCTIONS = """You are an experienced Indian equity market analyst.
For EACH ticker block below, read the news headlines and assess the likely
SHORT-TERM (1-2 week) impact on the NSE stock price.

Respond with ONLY a JSON array — no prose, no markdown fences. One object per
ticker, exactly this shape:
[{"ticker": "<symbol exactly as given>", "score": <float -1.0 to 1.0>,
  "confidence": <float 0.0 to 1.0>, "summary": "<max 2 sentences>"}]

Scoring: -1.0 very bearish, 0.0 neutral, +1.0 very bullish.
If headlines are stale, generic, or irrelevant to the stock, use score 0.0
with low confidence. Include EVERY ticker listed, even with no headlines.
"""


def _build_prompt(batch: list[tuple[str, list[dict]]]) -> str:
    parts = [SYSTEM_INSTRUCTIONS, "\nTickers and recent headlines:\n"]
    for ticker, headlines in batch:
        company = config.COMPANY_NAMES.get(ticker, ticker)
        parts.append(f'\n<ticker symbol="{ticker}" company="{company}">')
        if headlines:
            for h in headlines:
                date = f" ({h['date']})" if h.get("date") else ""
                parts.append(f"- {h['title']} [{h.get('publisher', '?')}]{date}")
        else:
            parts.append("- (no recent headlines found)")
        parts.append("</ticker>")
    return "\n".join(parts)


def _extract_json_array(text: str) -> list | None:
    """Parse a JSON array out of model text, tolerating fences/prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text.startswith("["):
        start, end = text.find("["), text.rfind("]")
        if start == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        return None


def _call_claude_cli(prompt: str, warnings: list[str], attempts: int = 2) -> list | None:
    """One `claude -p` invocation with retry. Returns parsed list of dicts or None."""
    exe = shutil.which("claude")
    if not exe:
        warnings.append("Claude CLI ('claude') not found on PATH - neutral sentiment used")
        return None

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                [exe, "-p", "--output-format", "json", "--model", config.SENTIMENT_MODEL],
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.CLAUDE_CLI_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            last_error = f"timed out after {config.CLAUDE_CLI_TIMEOUT}s"
            continue
        except OSError as exc:
            warnings.append(f"Claude CLI failed to launch ({exc}) - neutral sentiment used")
            return None

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()
            last_error = f"exited {proc.returncode}: {detail[0][:120] if detail else ''}"
            continue  # crash (e.g. transient Bun crash on Windows) - retry

        # -p --output-format json wraps the model text in an envelope
        result_text = proc.stdout
        try:
            envelope = json.loads(proc.stdout)
            if isinstance(envelope, dict) and "result" in envelope:
                result_text = envelope["result"]
        except json.JSONDecodeError:
            pass  # fall through - maybe raw text already

        parsed = _extract_json_array(result_text)
        if parsed is not None:
            return parsed
        last_error = "output was not parseable JSON"

    warnings.append(f"Claude CLI batch failed after {attempts} attempts ({last_error}) "
                    "- neutral sentiment used for this batch")
    return None


def _clamp(v, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def score_tickers(conn: sqlite3.Connection, date: str,
                  headlines_by_ticker: dict[str, list[dict]],
                  warnings: list[str], use_llm: bool = True) -> dict[str, dict]:
    """Return {ticker: {score, confidence, summary, source}} for all tickers.

    Uses the daily cache first; calls `claude -p` in batches only for
    uncached tickers; falls back to neutral 0.0 on any failure.
    """
    results: dict[str, dict] = {}
    to_score: list[str] = []

    for ticker in headlines_by_ticker:
        cached = db.get_sentiment(conn, ticker, date)
        if cached is not None:
            results[ticker] = {
                "score": cached["score"], "confidence": cached["confidence"],
                "summary": cached["summary"], "source": cached["source"],
            }
        else:
            to_score.append(ticker)

    if not to_score:
        return results

    llm_scores: dict[str, dict] = {}
    if use_llm and shutil.which("claude") is not None:
        from concurrent.futures import ThreadPoolExecutor

        batch_size = config.SENTIMENT_BATCH_SIZE
        chunks = [to_score[i : i + batch_size]
                  for i in range(0, len(to_score), batch_size)]

        def run_chunk(chunk: list[str]) -> tuple[list[str], list | None]:
            prompt = _build_prompt([(t, headlines_by_ticker[t]) for t in chunk])
            return chunk, _call_claude_cli(prompt, warnings)

        from concurrent.futures import as_completed

        with ThreadPoolExecutor(max_workers=config.SENTIMENT_PARALLEL_CALLS) as pool:
            futures = [pool.submit(run_chunk, c) for c in chunks]
            for fut in as_completed(futures):
                chunk, parsed = fut.result()
                if parsed is None:
                    continue  # this batch failed - others may still succeed
                for entry in parsed:
                    if not isinstance(entry, dict):
                        continue
                    t = str(entry.get("ticker", "")).strip()
                    if t not in chunk:
                        continue
                    rec = {
                        "score": _clamp(entry.get("score"), -1.0, 1.0, 0.0),
                        "confidence": _clamp(entry.get("confidence"), 0.0, 1.0, 0.0),
                        "summary": str(entry.get("summary", ""))[:500],
                        "source": "claude_cli",
                    }
                    llm_scores[t] = rec
                    # cache immediately - an interrupted run keeps its progress
                    db.upsert_sentiment(
                        conn, t, date, rec["score"], rec["confidence"], rec["summary"],
                        len(headlines_by_ticker.get(t, [])), rec["source"],
                    )
    elif use_llm:
        warnings.append("Claude CLI ('claude') not found on PATH - neutral sentiment used")

    for ticker in to_score:
        entry = llm_scores.get(ticker)
        if entry is None:
            entry = {
                "score": 0.0, "confidence": 0.0,
                "summary": "No LLM sentiment available - neutral fallback",
                "source": "neutral_fallback",
            }
            db.upsert_sentiment(
                conn, ticker, date, entry["score"], entry["confidence"],
                entry["summary"], len(headlines_by_ticker.get(ticker, [])), entry["source"],
            )
        results[ticker] = entry

    missing = [t for t in to_score if t not in llm_scores]
    if use_llm and missing and len(missing) < len(to_score):
        shown = ", ".join(missing[:8]) + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else "")
        warnings.append(f"Sentiment missing from LLM response for {len(missing)} tickers: {shown}")
    return results
