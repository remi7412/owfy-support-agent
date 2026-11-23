import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

import httpx

from . import database as db

logger = logging.getLogger("owfy.siem")


def normalize_audit_event(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalizes an audit_logs database row into a Wazuh/SIEM-compliant JSON structure.
    """
    action = (row.get("action") or "").strip()
    target_type = (row.get("target_type") or "").strip()
    target_id = (row.get("target_id") or "").strip()
    details = (row.get("details") or "").strip()
    ip_address = (row.get("ip_address") or "").strip() or "unknown"
    tenant_id = (row.get("tenant_id") or "").strip() or "default"
    created_at = row.get("created_at") or datetime.now(timezone.utc).isoformat()

    # Determine event_type, severity, and action_taken
    if action == "prompt_injection_blocked":
        event_type = "prompt_injection_blocked"
        severity = "high"
        action_taken = "blocked"
    elif action in ("admin_auth_failed", "login_failed"):
        event_type = "admin_auth_failed"
        severity = "high"
        action_taken = "denied"
    elif action == "rate_limit_exceeded":
        event_type = "rate_limit_exceeded"
        severity = "medium"
        action_taken = "throttled"
    elif action in ("takeover_toggle", "human_takeover_toggled"):
        event_type = "human_takeover_toggled"
        severity = "medium"
        action_taken = "toggled"
    elif action.endswith("_delete"):
        event_type = action
        severity = "medium"
        action_taken = "deleted"
    elif action == "login_success":
        event_type = "login_success"
        severity = "low"
        action_taken = "authenticated"
    else:
        event_type = action or "audit_event"
        severity = "low"
        action_taken = "recorded"

    # Extract session_id when applicable
    session_id: Optional[str] = None
    if target_type in ("session", "security") and target_id:
        session_id = target_id
    elif "session" in details.lower():
        # Fallback inspection if target_type wasn't explicit
        session_id = target_id or None

    return {
        "id": row.get("id"),
        "timestamp": created_at,
        "source": "owfy-support-agent",
        "tenant_id": tenant_id,
        "event_type": event_type,
        "severity": severity,
        "source_ip": ip_address,
        "session_id": session_id,
        "action_taken": action_taken,
        "admin_user": row.get("admin_user") or "unknown",
        "target_type": target_type or None,
        "target_id": target_id or None,
        "details": details,
    }


def generate_siem_signature(payload_bytes: bytes, secret: str) -> str:
    """
    Computes HMAC-SHA256 hex digest for webhook payload verification.
    """
    return hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()


async def flush_siem_events(batch_size: int = 100) -> Dict[str, Any]:
    """
    Asynchronously ships unexported audit logs to SIEM_WEBHOOK_URL with HMAC signature.
    Fails gracefully without throwing exceptions so it never blocks chat or admin operations.
    """
    webhook_url = os.getenv("SIEM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        try:
            settings = db.get_settings()
            webhook_url = settings.get("siem_webhook_url", "").strip()
        except Exception:
            pass

    if not webhook_url:
        return {"status": "skipped", "reason": "SIEM_WEBHOOK_URL not configured", "count": 0}

    try:
        unexported = db.get_unexported_audit_logs(limit=batch_size)
    except Exception as e:
        logger.error(f"Error reading unexported audit logs: {e}")
        return {"status": "error", "error": str(e), "count": 0}

    if not unexported:
        return {"status": "idle", "count": 0}

    normalized = [normalize_audit_event(row) for row in unexported]
    payload = {
        "source": "owfy-support-agent",
        "batch_count": len(normalized),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "events": normalized,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    secret = os.getenv("SIEM_WEBHOOK_SECRET", "").strip()
    if not secret:
        try:
            secret = db.get_settings().get("siem_webhook_secret", "").strip()
        except Exception:
            pass

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "owfy-support-agent/1.0",
    }
    if secret:
        sig = generate_siem_signature(payload_bytes, secret)
        headers["X-Owfy-Signature"] = f"sha256={sig}"

    access_key = os.getenv("SIEM_WEBHOOK_KEY", os.getenv("SIEM_ACCESS_KEY", "")).strip()
    if access_key:
        headers["X-Webhook-Access-Key"] = access_key

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(webhook_url, content=payload_bytes, headers=headers)
            resp.raise_for_status()

        log_ids = [r["id"] for r in unexported if r.get("id")]
        db.mark_audit_logs_exported(log_ids)
        return {"status": "success", "exported": len(log_ids)}
    except Exception as e:
        logger.warning(f"Failed to forward SIEM events to {webhook_url}: {e}")
        return {"status": "error", "error": str(e), "pending_count": len(unexported)}


def export_siem_events(
    limit: int = 100,
    format: str = "json",
    severity: Optional[str] = None,
) -> Union[List[Dict[str, Any]], str]:
    """
    Exports recent audit events normalized for pull-based SIEM ingestion.
    Supports 'json' and 'ndjson' formats with optional severity filtering.
    """
    limit = min(max(1, limit), 1000)
    raw_logs = db.list_audit_logs(limit=limit)
    events = [normalize_audit_event(r) for r in raw_logs]

    if severity:
        target_sev = severity.strip().lower()
        events = [e for e in events if e.get("severity", "").lower() == target_sev]

    if format.strip().lower() == "ndjson":
        return "\n".join(json.dumps(e) for e in events) + ("\n" if events else "")

    return events


def get_siem_status() -> Dict[str, Any]:
    """
    Returns telemetry for SIEM queue and forwarder health.
    """
    telemetry = db.get_siem_telemetry()
    webhook_url = os.getenv("SIEM_WEBHOOK_URL", "").strip()
    if not webhook_url:
        try:
            webhook_url = db.get_settings().get("siem_webhook_url", "").strip()
        except Exception:
            pass

    masked_url = ""
    if webhook_url:
        masked_url = webhook_url[:15] + "..." if len(webhook_url) > 15 else "***"

    secret = os.getenv("SIEM_WEBHOOK_SECRET", "").strip()
    if not secret:
        try:
            secret = db.get_settings().get("siem_webhook_secret", "").strip()
        except Exception:
            pass

    return {
        **telemetry,
        "webhook_configured": bool(webhook_url),
        "webhook_url": masked_url,
        "hmac_secret_configured": bool(secret),
        "source": "owfy-support-agent",
    }
