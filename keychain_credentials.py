"""
Store secrets in the system keychain (macOS Keychain / Windows Credential Manager).

Oracle sign-in uses a separate, portal-bound entry. Legacy v1 blobs that included
an expense password are migrated to OpenAI-only on load.
"""

from __future__ import annotations

import json
import threading
from urllib.parse import urlsplit
from typing import Any

KEYRING_SERVICE = "expense-automator"
KEYRING_CREDENTIALS_V1 = "credentials_v1"
LEGACY_OPENAI = "openai_api_key"
LEGACY_EXPENSE = "expense_portal_password"
ORACLE_CREDENTIALS = "oracle_credentials_v1"

_rlock = threading.RLock()
_blob: dict[str, str] | None = None
_loaded = False
_oracle_blob: dict[str, str] | None = None

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
    global _keychain_gated, _loaded, _blob, _oracle_blob
    with _rlock:
        _keychain_gated = False
        _loaded = False
        _blob = None
        _oracle_blob = None
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
    return get_oracle_credentials().get("password", "")


def set_keychain_openai_key(key: str) -> str | None:
    global _blob, _loaded
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


def oracle_origin(url: str) -> str:
    """Only send stored passwords to the HTTPS origin explicitly saved by the user."""
    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Saved Oracle sign-in requires an HTTPS portal URL without embedded credentials.")
    port = parsed.port
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}:{port if port is not None else 443}"


def get_oracle_credentials() -> dict[str, str]:
    global _oracle_blob
    with _rlock:
        if _keychain_gated:
            return {}
        if _oracle_blob is None:
            import keyring

            try:
                raw = keyring.get_password(KEYRING_SERVICE, ORACLE_CREDENTIALS)
                data = json.loads(raw) if raw else {}
                if not isinstance(data, dict) or any(not isinstance(v, str) for v in data.values()):
                    raise ValueError("Invalid Oracle credential entry")
                if data and (
                    not data.get("username", "").strip()
                    or not data.get("password")
                    or data.get("rejected", "") not in ("", "true")
                    or oracle_origin(data.get("origin", "")) != data.get("origin")
                ):
                    raise ValueError("Incomplete Oracle credential entry")
            except Exception:
                raise RuntimeError(
                    "Could not read Oracle sign-in from the system keychain. "
                    "Unlock secure storage or save your credentials again in Settings."
                ) from None
            _oracle_blob = data
        return _oracle_blob.copy()


def _write_oracle_credentials(data: dict[str, str]) -> str | None:
    global _oracle_blob
    if _keychain_gated:
        return 'Open the "Secure storage" notice and choose Continue before changing Oracle sign-in.'
    import keyring

    try:
        keyring.set_password(KEYRING_SERVICE, ORACLE_CREDENTIALS, json.dumps(data))
    except Exception:
        return "Oracle sign-in was not saved: system keychain unavailable. No plain-text copy was written."
    _oracle_blob = data.copy()
    return None


def save_oracle_credentials(portal_url: str, username: str, password: str | None = None) -> str | None:
    """A blank replacement preserves the existing password only for the same account/origin."""
    with _rlock:
        origin = oracle_origin(portal_url)
        username = username.strip()
        if not username:
            raise ValueError("Enter an Oracle username or use Forget saved sign-in.")
        if not password:
            old = get_oracle_credentials()
            if old.get("origin") != origin or old.get("username") != username or not old.get("password"):
                raise ValueError("Enter a password when saving a new Oracle account or portal.")
            if old.get("rejected") == "true":
                raise ValueError("Oracle rejected the saved password. Enter your current password to re-enable sign-in.")
            password = old["password"]
        return _write_oracle_credentials({
            "origin": origin, "username": username, "password": password, "rejected": "",
        })


def mark_oracle_credentials_rejected(expected: dict[str, str]) -> str | None:
    global _oracle_blob
    with _rlock:
        current = get_oracle_credentials()
        if current != expected:
            return None
        rejected = {**current, "rejected": "true"}
        error = _write_oracle_credentials(rejected)
        # Suppress repeated attempts in this process even when persistence fails.
        _oracle_blob = rejected
        return error


def delete_oracle_credentials() -> str | None:
    global _oracle_blob
    with _rlock:
        if _keychain_gated:
            return 'Open the "Secure storage" notice and choose Continue before removing Oracle sign-in.'
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(KEYRING_SERVICE, ORACLE_CREDENTIALS)
        except PasswordDeleteError:
            # Some backends also use this error for access failures: verify absence.
            try:
                remaining = keyring.get_password(KEYRING_SERVICE, ORACLE_CREDENTIALS)
            except Exception:
                return "Could not verify removal of Oracle sign-in from the system keychain."
            if remaining is not None:
                return "Could not remove Oracle sign-in from the system keychain."
        except Exception:
            return "Could not remove Oracle sign-in from the system keychain."
        _oracle_blob = {}
        return None


def delete_keychain_openai_key() -> None:
    set_keychain_openai_key("")


def delete_keychain_expense_password() -> str | None:
    return delete_oracle_credentials()
