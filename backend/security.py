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


def require_admin(authorization: Optional[str] = Header(None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Admin authentication required")
    token = authorization.split(" ", 1)[1].strip()
    user = verify_access_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def check_rate_limit(request: Request, bucket: str, max_requests: int = 20, window_sec: int = 60) -> None:
    client_ip = request.client.host if request.client else "unknown"
    key = f"{bucket}:{client_ip}"
    now = time.time()
    cutoff = now - window_sec
    recent = [ts for ts in _RATE_BUCKETS[key] if ts > cutoff]
    if len(recent) >= max_requests:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Please wait a moment and try again.")
    recent.append(now)
    _RATE_BUCKETS[key] = recent


def mask_pii(text: str) -> str:
    """Redacts emails, phone numbers, and card-like numbers before external LLM calls."""
    text = EMAIL_RE.sub("[EMAIL]", text)
    text = CARD_RE.sub("[CARD]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    return text
