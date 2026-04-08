"""
Store the OpenAI API key in the system keychain (macOS Keychain / Windows Credential Manager).

Oracle portal credentials are not stored — users sign in manually in the browser each session.
Legacy v1 blobs that included an expense password are migrated to OpenAI-only on load.
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

# When True, skip all keyring access (returns empty OpenAI key) until the user
# consents in the web UI. The desktop (Tk) app never enables this.
_keychain_gated = False


def _coerce_blob(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    return {"openai_api_key": str(raw.get("openai_api_key") or "").strip()}


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


def enable_keychain_access_gate() -> None:
    """Web UI: defer keyring reads/writes until the user accepts the security notice."""
    global _keychain_gated
    _keychain_gated = True


def is_keychain_access_gated() -> bool:
    return _keychain_gated


def grant_keychain_access_after_user_consent() -> None:
    """Call after the user acknowledges the OS keychain prompt; loads secrets from keyring."""
    global _keychain_gated, _loaded, _blob
    with _rlock:
        _keychain_gated = False
        _loaded = False
        _blob = None
    warm_up()


def _load_blob() -> dict[str, str]:
    """Populate _blob from Keychain (prefer single v1 entry).

    Each macOS Security.framework / ``security`` invocation can produce its own prompt.
    We call ``get_password`` for ``credentials_v1`` at most once per load. The legacy
    ``openai_api_key`` item is read only when v1 did not yield a usable blob (missing,
    corrupt, or wrong shape), so a normal v1-only install does not touch the legacy item.
    In-process caching (_loaded) ensures we do not re-query the keychain on every
    ``get_keychain_openai_key`` call.
    """
    global _blob, _loaded
    with _rlock:
        if _keychain_gated:
            return {"openai_api_key": ""}

        if _loaded and _blob is not None:
            return _blob.copy()

        import keyring

        v1_raw: str | None = None
        try:
            v1_raw = keyring.get_password(KEYRING_SERVICE, KEYRING_CREDENTIALS_V1)
        except Exception:
            v1_raw = None

        v1_needs_clean_persist = False
        v1_usable = False
        if v1_raw:
            try:
                data = json.loads(v1_raw)
                if isinstance(data, dict) and data.get("expense_portal_password"):
                    data = {"openai_api_key": str(data.get("openai_api_key") or "").strip()}
                    v1_needs_clean_persist = True
                got = _coerce_blob(data)
                if got is not None:
                    v1_usable = True
                    _blob = got
                    _loaded = True
                    if v1_needs_clean_persist:
                        try:
                            _persist_v1(_blob)
                        except Exception:
                            pass
                    return _blob.copy()
            except Exception:
                pass

        oa = ""
        if not v1_usable:
            try:
                oa = (keyring.get_password(KEYRING_SERVICE, LEGACY_OPENAI) or "").strip()
            except Exception:
                pass

        merged = {"openai_api_key": oa}
        _blob = merged
        _loaded = True

        if oa:
            try:
                _persist_v1(merged)
                _delete_legacy()
            except Exception:
                pass
        try:
            keyring.delete_password(KEYRING_SERVICE, LEGACY_EXPENSE)
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
    if _keychain_gated:
        return (
            'Open the "Secure storage" notice and choose Continue to save your API key in the system keychain.'
            if stripped
            else None
        )
    with _rlock:
        b = {"openai_api_key": stripped}
        try:
            _persist_v1(b)
            _blob = b
            _loaded = True
        except Exception as e:
            return str(e)
    return None


def set_keychain_expense_password(password: str) -> str | None:
    """No-op: Oracle passwords are not stored. Clears legacy keychain item if present."""
    if _keychain_gated:
        return None
    import keyring

    try:
        keyring.delete_password(KEYRING_SERVICE, LEGACY_EXPENSE)
    except Exception:
        pass
    with _rlock:
        global _blob, _loaded
        try:
            raw = keyring.get_password(KEYRING_SERVICE, KEYRING_CREDENTIALS_V1)
            if raw:
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("expense_portal_password"):
                    cleaned = {"openai_api_key": str(data.get("openai_api_key") or "").strip()}
                    _persist_v1(cleaned)
        except Exception:
            pass
        _loaded = False
        _blob = None
    return None


def delete_keychain_openai_key() -> None:
    set_keychain_openai_key("")


def delete_keychain_expense_password() -> None:
    set_keychain_expense_password("")
