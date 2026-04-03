"""
Persistent LLM work queue for VPN-split workflows.

When the expense portal is only reachable on VPN and OpenAI is blocked on VPN,
we: (1) collect query payloads on VPN, (2) resolve them off-VPN, (3) replay the
browser flow on VPN using stored answers.

Schema is extensible: add new "kind" values and payload shapes; resolve_step
dispatches on kind.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from persistence.atomic_json import atomic_write_json, load_json_or_quarantine

SCHEMA_VERSION = 1

# Kinds we know how to resolve today. Others are preserved for future steps
# (e.g. receipt → expense line mapping).
KNOWN_RESOLVER_KINDS = frozenset({"expense_type"})
REGISTERED_KINDS = frozenset({"expense_type", "receipt_to_expense"})


def llm_pending_file(app_dir: Path) -> Path:
    return app_dir / "llm_query_pending.json"


def expense_type_query_id(merchant_name: str, options: list[str]) -> str:
    """Stable id for (merchant + option list); duplicate rows share one LLM call."""
    norm_m = _norm_vendor_key(merchant_name)
    opts = [str(o).strip() for o in options if str(o).strip()]
    canonical = json.dumps({"m": norm_m, "o": opts}, sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"expense_type:{h}"


def _norm_vendor_key(name: str) -> str:
    import re

    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def expense_type_prompt(merchant_name: str, options: list[str]) -> str:
    """Shared user prompt for dropdown expense-type selection (keep in sync with UI)."""
    return (
        "Pick the single best expense type for this merchant name.\n"
        "Return strict JSON only in this shape: {\"expense_type\":\"<exact option text>\"}.\n"
        "Rules:\n"
        "- expense_type must exactly match one of the provided options.\n"
        "- Choose the most appropriate business expense category from merchant name only.\n"
        "- If uncertain, pick the closest general-purpose travel/business option.\n\n"
        f"Merchant: {merchant_name}\n"
        f"Options: {json.dumps(options)}"
    )


def new_empty_document() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": _iso_now(),
        "queries": {},
        "responses": {},
    }


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return new_empty_document()
    raw = load_json_or_quarantine(path, new_empty_document())
    if not isinstance(raw, dict):
        return new_empty_document()
    ver = int(raw.get("version", 0) or 0)
    if ver != SCHEMA_VERSION:
        return new_empty_document()
    queries = raw.get("queries")
    responses = raw.get("responses")
    if not isinstance(queries, dict):
        queries = {}
    if not isinstance(responses, dict):
        responses = {}
    return {
        "version": SCHEMA_VERSION,
        "updated_at": str(raw.get("updated_at", "") or _iso_now()),
        "queries": queries,
        "responses": responses,
    }


def save_document(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "version": SCHEMA_VERSION,
        "updated_at": _iso_now(),
        "queries": doc.get("queries", {}),
        "responses": doc.get("responses", {}),
    }
    atomic_write_json(path, out, indent=2, ensure_ascii=False)


def register_expense_type_query(
    doc: dict[str, Any],
    merchant_name: str,
    options: list[str],
) -> str:
    """Insert or refresh expense_type query; returns query id."""
    qid = expense_type_query_id(merchant_name, options)
    queries: dict[str, Any] = doc.setdefault("queries", {})
    queries[qid] = {
        "kind": "expense_type",
        "payload": {
            "merchant_name": merchant_name.strip(),
            "options": [str(o).strip() for o in options if str(o).strip()],
        },
    }
    return qid


def response_expense_type(doc: dict[str, Any], qid: str) -> str | None:
    block = doc.get("responses", {}).get(qid)
    if not isinstance(block, dict):
        return None
    val = str(block.get("expense_type", "")).strip()
    return val or None


def set_response_expense_type(doc: dict[str, Any], qid: str, expense_type: str) -> None:
    doc.setdefault("responses", {})[qid] = {
        "expense_type": expense_type.strip(),
        "resolved_at": _iso_now(),
    }


def count_pending(doc: dict[str, Any]) -> int:
    return len(pending_expense_type_ids(doc))


def pending_expense_type_ids(doc: dict[str, Any]) -> list[str]:
    out: list[str] = []
    queries = doc.get("queries", {})
    if not isinstance(queries, dict):
        return out
    for qid, q in queries.items():
        if not isinstance(q, dict):
            continue
        if str(q.get("kind", "")) != "expense_type":
            continue
        if not response_expense_type(doc, str(qid)):
            out.append(str(qid))
    return out


def register_receipt_to_expense_query(doc: dict[str, Any], qid: str, payload: dict[str, Any]) -> str:
    """
    Future: map receipt images/PDFs to expense lines. Use the same pending file; add a resolver
    in the UI worker that dispatches on kind == \"receipt_to_expense\" and extend KNOWN_RESOLVER_KINDS.
    """
    queries: dict[str, Any] = doc.setdefault("queries", {})
    queries[qid] = {"kind": "receipt_to_expense", "payload": dict(payload)}
    return qid


def validate_replay_ready(doc: dict[str, Any]) -> tuple[bool, str]:
    """Ensure every resolvable expense_type query has a response."""
    queries = doc.get("queries", {})
    if not isinstance(queries, dict) or not queries:
        return False, "No cached queries. Add expense-type prompts (e.g. vendor cache / LLM workflow) or use an OpenAI key for live categorization."
    missing: list[str] = []
    for qid, q in queries.items():
        if not isinstance(q, dict):
            continue
        kind = str(q.get("kind", ""))
        if kind == "expense_type":
            if not response_expense_type(doc, str(qid)):
                missing.append(str(qid))
        elif kind in REGISTERED_KINDS and kind not in KNOWN_RESOLVER_KINDS:
            missing.append(f"{qid} ({kind}: resolver not implemented yet)")
    if missing:
        return (
            False,
            f"{len(missing)} query/queries still need LLM resolution. "
            "Turn VPN off and run “Resolve types” (or finish the scrape flow’s LLM step).",
        )
    return True, ""
