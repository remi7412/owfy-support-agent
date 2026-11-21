import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import httpx

logger = logging.getLogger("owfy.webhooks")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def dispatch_webhook(
    event: str,
    data: Dict[str, Any],
    webhook_url: Optional[str] = None,
    secret: Optional[str] = None,
) -> bool:
    """
    Asynchronously dispatches an event notification to an external webhook URL
    (e.g., n8n workflow, Slack webhook, WhatsApp automation bridge).
    Includes HMAC-SHA256 signature when secret is configured.
    """
    if not webhook_url or not webhook_url.strip():
        return False

    url = webhook_url.strip()
    payload = {
        "event": event,
        "timestamp": utc_now(),
        "data": data,
    }
    body_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Owfy-Support-Webhook/1.0",
        "X-Owfy-Event": event,
    }

    if secret and secret.strip():
        sig = hmac.new(secret.strip().encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        headers["X-Owfy-Signature"] = f"sha256={sig}"

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(url, content=body_bytes, headers=headers)
            return resp.status_code < 400
    except Exception as exc:
        logger.warning("Webhook dispatch failed for '%s' to %s: %s", event, url, exc)
        return False
