from __future__ import annotations

import json
import re
from typing import Any


DEFAULT_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\buber\b|\blyft\b", re.IGNORECASE), "Transportation"),
    (re.compile(r"\bmarriott\b|\bhilton\b|\bhyatt\b", re.IGNORECASE), "Lodging"),
    (re.compile(r"\bdelta\b|\bunited\b|\bamerican airlines\b", re.IGNORECASE), "Airfare"),
    (re.compile(r"\bstarbucks\b|\bcafe\b|\brestaurant\b", re.IGNORECASE), "Meals"),
]


def _norm_merchant(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def choose_expense_type_with_ai(
    merchant_name: str,
    options: list[str],
    *,
    api_key: str,
    model: str,
    http_verify_preferred: str | None = None,
) -> str:
    """Ask AI for an exact dropdown option; never substitute a fallback on failure."""
    from browser_automation import build_openai_client, openai_tls_troubleshooting_hint
    from llm_query_cache import expense_type_prompt

    if not merchant_name.strip():
        raise ValueError("A merchant name is required.")
    if not options:
        raise ValueError("Configure expense types in Settings before using AI pick.")
    if not api_key.strip():
        raise ValueError("Add an OpenAI API key in Settings before using AI pick.")

    with build_openai_client(api_key, http_verify_preferred=http_verify_preferred) as client:
        try:
            response = client.responses.create(
                model=model,
                input=[{"role": "user", "content": [
                    {"type": "input_text", "text": expense_type_prompt(merchant_name, options)},
                ]}],
            )
        except Exception as exc:
            hint = openai_tls_troubleshooting_hint(exc)
            raise RuntimeError(f"OpenAI expense type request failed: {exc}{hint}") from exc

    raw = (response.output_text or "").strip()
    if raw.startswith("```") and raw.endswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("AI returned an invalid response. No expense type was changed; try again.") from exc
    choice = parsed.get("expense_type") if isinstance(parsed, dict) else None
    if not isinstance(choice, str) or choice not in options:
        raise ValueError("AI did not return an available expense type. No expense type was changed; try again.")
    return choice


def classify_transactions(
    transactions: list[dict[str, Any]],
    *,
    user_memory: dict[str, str] | None = None,
    llm_suggestions: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Precedence:
    1) user_memory merchant overrides
    2) deterministic rules
    3) optional llm_suggestions
    """
    memory = dict(user_memory or {})
    llm = dict(llm_suggestions or {})
    out: list[dict[str, Any]] = []
    for txn in transactions:
        txn_id = str(txn.get("line_id") or txn.get("transaction_id") or "").strip()
        merchant_raw = str(txn.get("merchant_name") or txn.get("merchant") or "").strip()
        merchant_key = _norm_merchant(merchant_raw)
        if merchant_key in memory:
            out.append(
                {
                    "transaction_id": txn_id,
                    "type": memory[merchant_key],
                    "confidence": 0.95,
                    "justification": "Previously confirmed user mapping for this merchant.",
                    "source": "user",
                }
            )
            continue

        rule_type: str | None = None
        for pat, exp_type in DEFAULT_RULES:
            if pat.search(merchant_raw):
                rule_type = exp_type
                break
        if rule_type:
            out.append(
                {
                    "transaction_id": txn_id,
                    "type": rule_type,
                    "confidence": 0.90,
                    "justification": f"Rule-based mapping matched merchant pattern for '{merchant_raw}'.",
                    "source": "rule",
                }
            )
            continue

        llm_pick = llm.get(txn_id) or {}
        llm_type = str(llm_pick.get("type") or "").strip()
        if llm_type:
            out.append(
                {
                    "transaction_id": txn_id,
                    "type": llm_type,
                    "confidence": float(llm_pick.get("confidence") or 0.65),
                    "justification": str(llm_pick.get("justification") or "LLM suggested category from merchant context."),
                    "source": "llm",
                }
            )
            continue

        out.append(
            {
                "transaction_id": txn_id,
                "type": "Uncategorized",
                "confidence": 0.2,
                "justification": "No deterministic rule or user mapping available.",
                "source": "llm",
            }
        )
    return out
