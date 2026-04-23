from __future__ import annotations

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
