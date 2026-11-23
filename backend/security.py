import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from collections import defaultdict
from typing import Dict, List, Optional
from fastapi import Header, HTTPException, Request

_RUNTIME_SECRET = secrets.token_hex(32)
_RATE_BUCKETS: Dict[str, List[float]] = defaultdict(list)

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{4}(?!\d)")
CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")


def _get_secret() -> str:
    return os.getenv("ADMIN_SECRET", "").strip() or _RUNTIME_SECRET


def verify_admin_credentials(username: str, password: str) -> bool:
    expected_user = os.getenv("ADMIN_USER", "admin").strip()
    expected_pass = os.getenv("ADMIN_PASS", "admin123").strip()
    return hmac.compare_digest(username, expected_user) and hmac.compare_digest(password, expected_pass)


def create_access_token(username: str, ttl_seconds: int = 86400) -> str:
    payload = {"sub": username, "exp": int(time.time()) + ttl_seconds}
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    b64 = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    sig = hmac.new(_get_secret().encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def verify_access_token(token: str) -> Optional[str]:
    try:
        b64, sig = token.split(".", 1)
        expected_sig = hmac.new(_get_secret().encode("utf-8"), b64.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return None
        padded = b64 + "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload.get("sub")
    except Exception:
        return None


def require_admin(request: Request, authorization: Optional[str] = Header(None)) -> str:
    client_ip = request.client.host if request.client else "unknown"
    path = request.url.path if request.url else "admin_endpoint"
    if not authorization or not authorization.startswith("Bearer "):
        try:
            from . import database as db
            db.add_audit_log(
                admin_user="unknown",
                action="admin_auth_failed",
                target_type="auth",
                target_id=path,
                details="Missing or invalid Bearer authorization header",
                ip_address=client_ip,
            )
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Admin authentication required")
    token = authorization.split(" ", 1)[1].strip()
    user = verify_access_token(token)
    if not user:
        try:
            from . import database as db
            db.add_audit_log(
                admin_user="unknown",
                action="admin_auth_failed",
                target_type="auth",
                target_id=path,
                details="Invalid or expired Bearer token",
                ip_address=client_ip,
            )
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def check_rate_limit(request: Request, bucket: str, max_requests: int = 20, window_sec: int = 60) -> None:
    if os.getenv("DISABLE_RATE_LIMIT", "0").lower() in ("1", "true", "yes") or os.getenv("CI", "false").lower() == "true":
        return
    client_ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{client_ip}"
    now = time.time()
    cutoff = now - window_sec
    recent = [ts for ts in _RATE_BUCKETS[key] if ts > cutoff]
    if len(recent) >= max_requests:
        try:
            from . import database as db
            db.add_audit_log(
                admin_user="system",
                action="rate_limit_exceeded",
                target_type="rate_limit",
                target_id=bucket,
                details=f"Rate limit exceeded on bucket {bucket} ({len(recent)} requests in {window_sec}s)",
                ip_address=client_ip,
            )
        except Exception:
            pass
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a moment and try again.")
    recent.append(now)
    _RATE_BUCKETS[key] = recent


def mask_pii(text: str) -> str:
    """Redacts emails, phone numbers, and card-like numbers before external LLM calls."""
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = CARD_RE.sub("[CARD]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    return text


# --- Prompt-Injection Guardrails & Input Sanitization ---
PROMPT_INJECTION_PATTERNS = [
    re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bdisregard\s+(?:all\s+)?(?:previous|prior|above)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\bforget\s+(?:all\s+)?(?:your\s+)?instructions\b", re.IGNORECASE),
    re.compile(r"\b(?:you\s+are\s+now|act\s+as)\s+(?:DAN|developer\s+mode|unrestricted|jailbroken)\b", re.IGNORECASE),
    re.compile(r"\b(?:reveal|show|print|output|leak|repeat)\s+(?:your\s+)?(?:system\s+prompt|developer\s+instructions|initial\s+prompt)\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+override\b", re.IGNORECASE),
    re.compile(r"<\s*/?\s*system\s*>", re.IGNORECASE),
    re.compile(r"\[\s*/?\s*INST\s*\]", re.IGNORECASE),
    re.compile(r"<\s*/?\s*s\s*>", re.IGNORECASE),
    re.compile(r"\bnew\s+system\s+prompt\s*:", re.IGNORECASE),
    re.compile(r"\bbypass\s+(?:content\s+filter|guardrails?|safety\s+filters?)\b", re.IGNORECASE),
]


def detect_prompt_injection(text: str) -> tuple[bool, Optional[str]]:
    """
    Scans input for common prompt injection heuristics and jailbreak attempts.
    Returns (is_injection, reason).
    """
    if not text:
        return False, None

    for pattern in PROMPT_INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return True, f"Matched injection pattern: '{match.group(0)}'"

    # Check for excessive delimiter abuse or pseudo-role injections
    role_pattern = re.compile(r"(?:\n|^)\s*(?:system|assistant|admin)\s*:\s*", re.IGNORECASE)
    if len(role_pattern.findall(text)) >= 2:
        return True, "Repeated structural role spoofing detected"

    return False, None


def sanitize_user_input(text: str) -> str:
    """
    Sanitizes user input by removing null bytes, neutralizing control tags,
    and trimming excessive structural whitespaces.
    """
    if not text:
        return ""
    sanitized = "".join(ch for ch in text if ch in ("\n", "\r", "\t") or ord(ch) >= 32)
    sanitized = sanitized.replace("</system>", "&lt;/system&gt;").replace("<system>", "&lt;system&gt;")
    sanitized = sanitized.replace("[INST]", "&#91;INST&#93;").replace("[/INST]", "&#91;/INST&#93;")
    return sanitized.strip()
