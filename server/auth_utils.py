"""
Brenducate — Authentication & Key Hashing Utilities (Zero External Dependencies)

Uses Python standard-library PBKDF2-HMAC-SHA256 for password hashing and
HMAC-SHA256 signed tokens for browser dashboard sessions.
"""

import os
import hmac
import json
import time
import base64
import hashlib
import secrets
from typing import Optional


def _get_session_secret() -> bytes:
    secret = os.getenv("SESSION_SECRET", "")
    if not secret:
        # Fallback dev secret derived from process/env for local testing.
        # Set SESSION_SECRET in Vercel / Render production env vars.
        secret = "brenducate-dev-session-secret-change-in-prod"
    return secret.encode("utf-8")


def hash_password(password: str) -> str:
    """Hash a user password using PBKDF2-HMAC-SHA256 with a random 16-byte salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored_hash: Optional[str]) -> bool:
    """Verify a password against a stored PBKDF2-HMAC-SHA256 hash."""
    if not stored_hash or "$" not in stored_hash:
        return False
    try:
        salt_hex, expected_hex = stored_hash.split("$", 1)
        salt = bytes.fromhex(salt_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000).hex()
        return hmac.compare_digest(actual, expected_hex)
    except Exception:
        return False


def generate_api_key() -> str:
    """Generate a new production Brenducate API key."""
    return "br_live_" + secrets.token_hex(16)


def hash_api_key(raw_key: str) -> str:
    """One-way SHA-256 hash for secure API key storage and lookup."""
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def get_api_key_prefix(raw_key: str) -> str:
    """Return non-sensitive display prefix like 'br_live_8f2a...'."""
    if len(raw_key) <= 14:
        return raw_key[:6] + "..."
    return raw_key[:12] + "..." + raw_key[-4:]


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def create_session_token(account_id: str, ttl_seconds: int = 86400 * 7) -> str:
    """Create a signed HMAC session token for dashboard authentication."""
    payload = {
        "sub": account_id,
        "exp": int(time.time()) + ttl_seconds,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(_get_session_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return f"sess_{payload_b64}.{_b64url_encode(sig)}"


def verify_session_token(token: str) -> Optional[str]:
    """Verify session token signature and expiry; returns account_id or None."""
    if not token or not token.startswith("sess_"):
        return None
    body = token.removeprefix("sess_").strip()
    if "." not in body:
        return None
    payload_b64, sig_b64 = body.split(".", 1)
    try:
        expected_sig = hmac.new(
            _get_session_secret(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None
        return payload.get("sub")
    except Exception:
        return None
