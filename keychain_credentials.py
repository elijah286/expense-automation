"""
Store OpenAI + Oracle secrets in one Keychain entry so macOS usually prompts once per process.

Legacy layout used two items; we migrate on first load.
"""

from __future__ import annotations

import json
import threading
from typing import Any

KEYRING_SERVICE = "expense-automator"
KEYRING_CREDENTIALS_V1 = "credentials_v1"
LEGACY_OPENAI = "openai_api_key"
LEGACY_EXPENSE = "expense_portal_password"

_rlock = threading.RLock()
_blob: dict[str, str] | None = None
_loaded = False


def _coerce_blob(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {
        "openai_api_key": str(raw.get("openai_api_key") or "").strip(),
        "expense_portal_password": str(raw.get("expense_portal_password") or "").strip(),
    }


def _persist_v1(blob: dict[str, str]) -> None:
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_CREDENTIALS_V1, json.dumps(blob, ensure_ascii=False))


def _delete_legacy() -> None:
    import keyring

    for account in (LEGACY_OPENAI, LEGACY_EXPENSE):
        try:
            keyring.delete_password(KEYRING_SERVICE, account)
        except Exception:
            pass


def _load_blob() -> dict[str, str]:
    """Populate _blob from Keychain (prefer single v1 entry)."""
    global _blob, _loaded
    with _rlock:
        if _loaded and _blob is not None:
            return _blob.copy()

        import keyring

        try:
            raw = keyring.get_password(KEYRING_SERVICE, KEYRING_CREDENTIALS_V1)
            if raw:
                data = json.loads(raw)
                got = _coerce_blob(data)
                if got is not None:
                    _blob = got
                    _loaded = True
                    return _blob.copy()
        except Exception:
            pass

        oa = ""
        ep = ""
        try:
            oa = (keyring.get_password(KEYRING_SERVICE, LEGACY_OPENAI) or "").strip()
        except Exception:
            pass
        try:
            ep = (keyring.get_password(KEYRING_SERVICE, LEGACY_EXPENSE) or "").strip()
        except Exception:
            pass

        merged = {"openai_api_key": oa, "expense_portal_password": ep}
        _blob = merged
        _loaded = True

        if oa or ep:
            try:
                _persist_v1(merged)
                _delete_legacy()
            except Exception:
                pass

        return merged.copy()


def warm_up() -> None:
    """Load credentials at startup so Keychain prompts once, early."""
    _load_blob()


def get_keychain_openai_key() -> str:
    return _load_blob().get("openai_api_key", "")


def get_keychain_expense_password() -> str:
    return _load_blob().get("expense_portal_password", "")


def set_keychain_openai_key(key: str) -> str | None:
    stripped = key.strip()
    with _rlock:
        _load_blob()
        assert _blob is not None
        b = {
            "openai_api_key": stripped,
            "expense_portal_password": _blob.get("expense_portal_password", ""),
        }
        try:
            _persist_v1(b)
            _blob = b
        except Exception as e:
            return str(e)
    return None


def set_keychain_expense_password(password: str) -> str | None:
    stripped = password.strip()
    with _rlock:
        _load_blob()
        assert _blob is not None
        b = {
            "openai_api_key": _blob.get("openai_api_key", ""),
            "expense_portal_password": stripped,
        }
        try:
            _persist_v1(b)
            _blob = b
        except Exception as e:
            return f"Could not save password to keychain: {e}"
    return None


def delete_keychain_openai_key() -> None:
    set_keychain_openai_key("")


def delete_keychain_expense_password() -> None:
    set_keychain_expense_password("")
