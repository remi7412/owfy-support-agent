import asyncio
import os
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend import database as db
from backend.emailer import send_escalation_email
from backend.ingest import extract_pdf_content, fetch_website_content
from backend.llm import generate_support_reply
from backend.rag import retrieve_relevant_chunks
from backend.webhooks import dispatch_webhook
from backend.security import (
    check_rate_limit,
    create_access_token,
    detect_prompt_injection,
    require_admin,
    sanitize_user_input,
    verify_admin_credentials,
)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

allowed_origins = [
    o.strip()
    for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",")
    if o.strip()
]

app = FastAPI(title="Owfy Enterprise AI Support Platform", version="3.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    db.init_db()


# --- Schemas ---
class LoginRequest(BaseModel):
    username: str
    password: str


class TenantPayload(BaseModel):
    slug: str = Field(..., min_length=2)
    name: str = Field(..., min_length=1)
    business_mode: str = "hybrid"
    support_email: Optional[str] = ""
    system_prompt: Optional[str] = ""
    widget_color: Optional[str] = "#141413"


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    mode: Optional[str] = None
    visitor_name: Optional[str] = "Guest"
    visitor_email: Optional[str] = ""
    tenant_id: Optional[str] = "default"


class FeedbackRequest(BaseModel):
    message_id: int
    rating: int = Field(..., ge=-1, le=1)


class KnowledgePayload(BaseModel):
    title: str = Field(..., min_length=1)
    category: str = "General"
    mode_scope: str = "all"
    content: str = Field(..., min_length=1)
    tenant_id: Optional[str] = "default"


class ImportUrlPayload(BaseModel):
    url: str = Field(..., min_length=4)
    category: str = "Web"
    mode_scope: str = "all"
    custom_title: Optional[str] = None
    tenant_id: Optional[str] = "default"


class TicketPayload(BaseModel):
    session_id: str
    customer_name: str = "Guest"
    customer_email: str = Field(..., min_length=3)
    subject: str = Field(..., min_length=2)
    summary: Optional[str] = ""
    tenant_id: Optional[str] = "default"


class TicketStatusPayload(BaseModel):
    status: str = Field(..., pattern="^(open|in_progress|resolved)$")


class BookingPayload(BaseModel):
    session_id: str
    customer_name: str = Field(..., min_length=1)
    customer_contact: str = Field(..., min_length=3)
    service_type: str = Field(..., min_length=2)
    preferred_date: str = Field(..., min_length=2)
    preferred_time: str = Field(..., min_length=2)
    call_mode: str = "voice_callback"
    notes: Optional[str] = ""
    tenant_id: Optional[str] = "default"


class BookingStatusPayload(BaseModel):
    status: str = Field(..., pattern="^(pending|confirmed|completed|cancelled)$")


class TrackablePayload(BaseModel):
    tracking_code: str = Field(..., min_length=2)
    customer_name: str = Field(..., min_length=1)
    item_title: str = Field(..., min_length=2)
    category: str = "order"
    status: str = "in_progress"
    eta: str = "TBD"
    last_update: str = Field(..., min_length=2)
    tenant_id: Optional[str] = "default"


class SettingsPayload(BaseModel):
    business_name: Optional[str] = None
    groq_model: Optional[str] = None
    support_email: Optional[str] = None
    agent_mode: Optional[str] = None
    system_prompt: Optional[str] = None
    webhook_tickets_url: Optional[str] = None
    webhook_bookings_url: Optional[str] = None
    webhook_voice_callback_url: Optional[str] = None
    webhook_secret: Optional[str] = None


class TakeoverPayload(BaseModel):
    active: bool


class AdminReplyPayload(BaseModel):
    message: str = Field(..., min_length=1)


class WebhookTestPayload(BaseModel):
    url: str = Field(..., min_length=4)
    secret: Optional[str] = ""


# --- Frontend Routes ---
@app.get("/", include_in_schema=False)
def serve_landing_page():
    return FileResponse(FRONTEND_DIR / "landing.html")


@app.get("/chat", include_in_schema=False)
def serve_chat_ui():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/widget.js", include_in_schema=False)
def serve_widget_js():
    return FileResponse(FRONTEND_DIR / "widget.js", media_type="application/javascript")


@app.get("/admin", include_in_schema=False)
def serve_admin_ui():
    return FileResponse(FRONTEND_DIR / "admin.html")


# --- Auth ---
@app.post("/api/auth/login")
def admin_login(payload: LoginRequest, request: Request):
    check_rate_limit(request, "login", max_requests=10, window_sec=60)
    ip = request.client.host if request.client else None
    if not verify_admin_credentials(payload.username, payload.password):
        db.add_audit_log(
            admin_user=payload.username or "unknown",
            action="login_failed",
            target_type="auth",
            details="Invalid username or password attempted",
            ip_address=ip,
        )
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token(payload.username)
    db.add_audit_log(
        admin_user=payload.username,
        action="login_success",
        target_type="auth",
        details="Admin signed in successfully",
        ip_address=ip,
    )
    return {"access_token": token, "token_type": "bearer"}


# --- Public Customer Routes ---
@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/public/tenant/{slug}")
def get_public_tenant(slug: str):
    tenant = db.get_tenant(slug)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant '{slug}' not found")
    return {
        "slug": tenant["slug"],
        "name": tenant["name"],
        "business_mode": tenant["business_mode"],
        "support_email": tenant["support_email"],
        "widget_color": tenant.get("widget_color", "#141413"),
    }


@app.get("/api/public/settings")
def read_public_settings(tenant: Optional[str] = "default"):
    settings = db.get_settings()
    t = db.get_tenant(tenant or "default") or db.get_tenant("default")
    b_name = t["name"] if t and t.get("name") else settings.get("business_name", "Owfy")
    a_mode = t["business_mode"] if t and t.get("business_mode") else settings.get("agent_mode", "hybrid")
    w_color = t.get("widget_color", "#141413") if t else "#141413"
    return {
        "business_name": b_name,
        "agent_mode": a_mode,
        "widget_color": w_color,
        "tenant_id": t["slug"] if t else (tenant or "default"),
    }


@app.get("/api/chat/history/{session_id}")
def get_public_session_history(session_id: str):
    session = db.get_session(session_id)
    return {
        "session_id": session_id,
        "human_active": bool(session.get("human_active")) if session else False,
        "messages": db.get_session_messages(session_id, limit=50),
    }


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    check_rate_limit(request, "chat", max_requests=20, window_sec=60)
    tenant_slug = req.tenant_id or "default"
    clean_message = sanitize_user_input(req.message)

    # Prompt-injection & adversarial attack guardrail
    is_injection, reason = detect_prompt_injection(clean_message)
    if is_injection:
        db.add_audit_log(
            admin_user="system",
            action="prompt_injection_blocked",
            target_type="security",
            target_id=req.session_id,
            details=f"Blocked prompt injection: {reason}",
            ip_address=request.client.host if request.client else None,
        )
        db.ensure_session(req.session_id, req.visitor_name or "Guest", req.visitor_email or "", tenant_id=tenant_slug)
        db.add_message(req.session_id, "user", clean_message, tenant_id=tenant_slug)
        guardrail_reply = (
            "Security Notice: Your input contains prohibited instructions or prompt override patterns. "
            "Please state your customer support request clearly."
        )
        msg_id = db.add_message(
            req.session_id,
            "assistant",
            guardrail_reply,
            sources=[{"title": "Security Guardrail", "category": "Safety"}],
            escalated=False,
            tenant_id=tenant_slug,
        )
        return {
            "session_id": req.session_id,
            "message_id": msg_id,
            "reply": guardrail_reply,
            "sources": [{"title": "Security Guardrail", "category": "Safety"}],
            "suggest_escalation": False,
            "action_card": None,
            "tenant_id": tenant_slug,
        }

    db.ensure_session(req.session_id, req.visitor_name or "Guest", req.visitor_email or "", tenant_id=tenant_slug)

    session = db.get_session(req.session_id)
    if session and session.get("human_active"):
        db.add_message(req.session_id, "user", clean_message, tenant_id=tenant_slug)
        return {
            "session_id": req.session_id,
            "message_id": None,
            "reply": "A live specialist has joined your chat session. Your message has been delivered directly to them.",
            "sources": [],
            "suggest_escalation": False,
            "action_card": None,
            "human_active": True,
            "tenant_id": tenant_slug,
        }

    history = db.get_session_messages(req.session_id, limit=12)
    settings = db.get_settings()

    tenant = db.get_tenant(tenant_slug) or db.get_tenant("default")
    if tenant:
        settings["business_name"] = tenant["name"]
        if tenant.get("business_mode"):
            settings["agent_mode"] = tenant["business_mode"]
        if tenant.get("support_email"):
            settings["support_email"] = tenant["support_email"]
        if tenant.get("system_prompt"):
            settings["system_prompt"] = tenant["system_prompt"]

    effective_mode = req.mode if req.mode in ("it_support", "business_booking", "hybrid") else settings.get("agent_mode", "hybrid")
    docs = db.list_knowledge_docs(mode_filter=effective_mode, tenant_id=tenant_slug)

    chunks = retrieve_relevant_chunks(clean_message, docs, top_k=3)
    sources = [{"title": c["title"], "category": c["category"], "score": c["score"]} for c in chunks]

    reply, should_escalate, action_card = await generate_support_reply(
        session_id=req.session_id,
        user_message=clean_message,
        history=history,
        retrieved_chunks=chunks,
        settings=settings,
        mode_override=effective_mode,
        tenant_id=tenant_slug,
    )

    db.add_message(req.session_id, "user", clean_message, tenant_id=tenant_slug)
    msg_id = db.add_message(req.session_id, "assistant", reply, sources=sources, escalated=should_escalate, tenant_id=tenant_slug)

    return {
        "session_id": req.session_id,
        "message_id": msg_id,
        "reply": reply,
        "sources": sources,
        "suggest_escalation": should_escalate,
        "action_card": action_card,
        "tenant_id": tenant_slug,
    }


@app.post("/api/feedback")
def submit_feedback(payload: FeedbackRequest):
    if not db.set_message_feedback(payload.message_id, payload.rating):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"ok": True}


@app.post("/api/bookings")
async def create_public_booking(payload: BookingPayload, request: Request):
    check_rate_limit(request, "bookings", max_requests=10, window_sec=60)
    tenant_slug = payload.tenant_id or "default"
    db.ensure_session(payload.session_id, payload.customer_name, payload.customer_contact, tenant_id=tenant_slug)
    booking = db.create_booking(
        session_id=payload.session_id,
        customer_name=payload.customer_name,
        customer_contact=payload.customer_contact,
        service_type=payload.service_type,
        preferred_date=payload.preferred_date,
        preferred_time=payload.preferred_time,
        call_mode=payload.call_mode,
        notes=payload.notes or "",
        tenant_id=tenant_slug,
    )

    settings = db.get_settings()
    w_url = settings.get("webhook_bookings_url", "").strip()
    w_sec = settings.get("webhook_secret", "").strip()
    if w_url:
        asyncio.create_task(dispatch_webhook("booking.created", booking, w_url, w_sec))

    return {"booking": booking}


@app.get("/api/track/{tracking_code}")
def lookup_public_tracking(tracking_code: str, request: Request, tenant: Optional[str] = None):
    check_rate_limit(request, "track", max_requests=30, window_sec=60)
    item = db.get_trackable_by_code(tracking_code, tenant_id=tenant)
    if not item:
        raise HTTPException(status_code=404, detail=f"No record found for tracking code '{tracking_code}'")
    return {"item": item}


@app.post("/api/tickets")
async def submit_ticket(payload: TicketPayload, request: Request):
    check_rate_limit(request, "tickets", max_requests=5, window_sec=60)
    tenant_slug = payload.tenant_id or "default"
    db.ensure_session(payload.session_id, payload.customer_name, payload.customer_email, tenant_id=tenant_slug)
    settings = db.get_settings()
    tenant = db.get_tenant(tenant_slug)
    if tenant:
        if tenant.get("name"):
            settings["business_name"] = tenant["name"]
        if tenant.get("support_email"):
            settings["support_email"] = tenant["support_email"]

    summary = payload.summary.strip()
    if not summary:
        msgs = db.get_session_messages(payload.session_id, limit=10)
        summary = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in msgs) or payload.subject

    ticket = db.create_ticket(
        session_id=payload.session_id,
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        subject=payload.subject,
        summary=summary,
        email_sent=False,
        email_note="Pending",
        tenant_id=tenant_slug,
    )

    sent, note = send_escalation_email(
        ticket_id=ticket["id"],
        customer_name=payload.customer_name,
        customer_email=payload.customer_email,
        subject=payload.subject,
        summary=summary,
        support_email=settings.get("support_email", ""),
        business_name=settings.get("business_name", "Owfy"),
    )

    conn = db.get_conn()
    conn.execute(
        "UPDATE tickets SET email_sent = ?, email_note = ? WHERE id = ?",
        (1 if sent else 0, note, ticket["id"]),
    )
    conn.commit()
    updated = dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket["id"],)).fetchone())
    conn.close()

    w_url = settings.get("webhook_tickets_url", "").strip()
    w_sec = settings.get("webhook_secret", "").strip()
    if w_url:
        asyncio.create_task(dispatch_webhook("ticket.created", updated, w_url, w_sec))

    return {"ticket": updated}


# --- Protected Admin Routes ---
@app.get("/api/tenants")
def get_tenants(_: str = Depends(require_admin)):
    return {"tenants": db.list_tenants()}


@app.post("/api/tenants")
def save_tenant(payload: TenantPayload, user: str = Depends(require_admin)):
    tenant = db.upsert_tenant(
        slug=payload.slug,
        name=payload.name,
        business_mode=payload.business_mode,
        support_email=payload.support_email or "",
        system_prompt=payload.system_prompt or "",
        widget_color=payload.widget_color or "#141413",
    )
    db.add_audit_log(user, "tenant_upsert", "tenant", payload.slug, f"Saved tenant '{payload.name}'")
    return {"tenant": tenant}


@app.delete("/api/tenants/{tenant_id}")
def remove_tenant(tenant_id: int, user: str = Depends(require_admin)):
    if not db.delete_tenant(tenant_id):
        raise HTTPException(status_code=400, detail="Cannot delete tenant (default tenant is protected).")
    db.add_audit_log(user, "tenant_delete", "tenant", str(tenant_id), "Deleted custom tenant")
    return {"deleted": True}


@app.get("/api/sessions")
def get_sessions(tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    return {
        "sessions": db.list_sessions(tenant_id=tenant_id),
        "feedback": db.get_feedback_stats(tenant_id=tenant_id),
        "resolution": db.get_resolution_analytics(tenant_id=tenant_id),
    }


@app.get("/api/analytics/resolution")
def get_resolution_analytics(tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    return {
        "resolution": db.get_resolution_analytics(tenant_id=tenant_id),
        "feedback": db.get_feedback_stats(tenant_id=tenant_id),
    }


@app.get("/api/sessions/{session_id}")
def get_session_history(session_id: str, _: str = Depends(require_admin)):
    session = db.get_session(session_id)
    return {
        "session_id": session_id,
        "session": session,
        "messages": db.get_session_messages(session_id, limit=100),
    }


@app.post("/api/sessions/{session_id}/takeover")
def toggle_takeover(session_id: str, payload: TakeoverPayload, user: str = Depends(require_admin)):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    db.set_session_human_takeover(session_id, payload.active)
    updated_session = db.get_session(session_id)
    db.add_audit_log(user, "takeover_toggle", "session", session_id, f"Takeover active={payload.active}")
    return {"session_id": session_id, "human_active": bool(payload.active), "session": updated_session}


@app.post("/api/sessions/{session_id}/reply")
def admin_reply(session_id: str, payload: AdminReplyPayload, _: str = Depends(require_admin)):
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    tenant_slug = session.get("tenant_id") or "default"
    msg_id = db.add_message(
        session_id=session_id,
        role="assistant",
        content=payload.message.strip(),
        sources=[{"title": "Live Support Specialist", "category": "Human Specialist"}],
        escalated=False,
        tenant_id=tenant_slug,
    )
    return {
        "session_id": session_id,
        "message_id": msg_id,
        "reply": payload.message.strip(),
        "role": "assistant",
    }


@app.post("/api/webhooks/test")
async def test_webhook(payload: WebhookTestPayload, _: str = Depends(require_admin)):
    success = await dispatch_webhook(
        event="test.ping",
        data={"message": "Test ping from Owfy Support Platform", "status": "operational"},
        webhook_url=payload.url,
        secret=payload.secret,
    )
    return {"success": success, "url": payload.url}


@app.get("/api/knowledge")
def get_knowledge(tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    return {"docs": db.list_knowledge_docs(tenant_id=tenant_id)}


@app.post("/api/knowledge")
def create_knowledge(payload: KnowledgePayload, user: str = Depends(require_admin)):
    doc = db.add_knowledge_doc(
        title=payload.title,
        category=payload.category,
        content=payload.content,
        mode_scope=payload.mode_scope,
        tenant_id=payload.tenant_id or "default",
    )
    db.add_audit_log(user, "knowledge_create", "knowledge", str(doc["id"]), f"Created doc '{payload.title}' ({payload.tenant_id or 'default'})")
    return {"doc": doc}


@app.put("/api/knowledge/{doc_id}")
def edit_knowledge(doc_id: int, payload: KnowledgePayload, user: str = Depends(require_admin)):
    doc = db.update_knowledge_doc(
        doc_id,
        payload.title,
        payload.category,
        payload.content,
        payload.mode_scope,
        tenant_id=payload.tenant_id or "default",
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    db.add_audit_log(user, "knowledge_edit", "knowledge", str(doc_id), f"Updated doc '{payload.title}'")
    return {"doc": doc}


@app.delete("/api/knowledge/{doc_id}")
def remove_knowledge(doc_id: int, user: str = Depends(require_admin)):
    if not db.delete_knowledge_doc(doc_id):
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    db.add_audit_log(user, "knowledge_delete", "knowledge", str(doc_id), "Deleted knowledge document")
    return {"deleted": True}


@app.get("/api/knowledge/search")
def search_knowledge(q: str, tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    docs = db.list_knowledge_docs(tenant_id=tenant_id)
    return {"results": retrieve_relevant_chunks(q, docs, top_k=5)}


@app.post("/api/knowledge/import-url")
async def import_knowledge_from_url(payload: ImportUrlPayload, user: str = Depends(require_admin)):
    try:
        title, content = await fetch_website_content(payload.url)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"Web extraction error: {err}")

    final_title = payload.custom_title.strip() if payload.custom_title else title
    doc = db.add_knowledge_doc(
        title=final_title,
        category=payload.category,
        content=content,
        mode_scope=payload.mode_scope,
        tenant_id=payload.tenant_id or "default",
    )
    db.add_audit_log(user, "knowledge_import_url", "knowledge", str(doc["id"]), f"Imported URL '{payload.url}'")
    return {"doc": doc, "chars": len(content)}


@app.post("/api/knowledge/import-pdf")
async def import_knowledge_from_pdf(
    file: UploadFile = File(...),
    category: str = Form("Manual"),
    mode_scope: str = Form("all"),
    custom_title: Optional[str] = Form(None),
    tenant_id: str = Form("default"),
    user: str = Depends(require_admin),
):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only .pdf files are supported")

    try:
        file_bytes = await file.read()
        title, content, pages = extract_pdf_content(file_bytes, filename=file.filename)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"PDF extraction error: {err}")

    final_title = custom_title.strip() if custom_title else title
    doc = db.add_knowledge_doc(
        title=final_title,
        category=category,
        content=content,
        mode_scope=mode_scope,
        tenant_id=tenant_id or "default",
    )
    db.add_audit_log(user, "knowledge_import_pdf", "knowledge", str(doc["id"]), f"Imported PDF '{file.filename}' ({pages} pages)")
    return {"doc": doc, "pages": pages, "chars": len(content)}


@app.get("/api/tickets")
def get_tickets(tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    return {"tickets": db.list_tickets(tenant_id=tenant_id)}


@app.patch("/api/tickets/{ticket_id}")
def patch_ticket(ticket_id: int, payload: TicketStatusPayload, user: str = Depends(require_admin)):
    ticket = db.update_ticket_status(ticket_id, payload.status)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.add_audit_log(user, "ticket_status_update", "ticket", str(ticket_id), f"Status updated to '{payload.status}'")
    return {"ticket": ticket}


@app.delete("/api/tickets/{ticket_id}")
def remove_ticket(ticket_id: int, user: str = Depends(require_admin)):
    if not db.delete_ticket(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket not found")
    db.add_audit_log(user, "ticket_delete", "ticket", str(ticket_id), "Deleted ticket")
    return {"deleted": True}


@app.get("/api/bookings")
def get_bookings(tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    return {"bookings": db.list_bookings(tenant_id=tenant_id)}


@app.patch("/api/bookings/{booking_id}")
async def patch_booking(booking_id: int, payload: BookingStatusPayload, user: str = Depends(require_admin)):
    booking = db.update_booking_status(booking_id, payload.status)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    db.add_audit_log(user, "booking_status_update", "booking", str(booking_id), f"Status changed to '{payload.status}'")

    voice_dispatched = False
    if payload.status == "confirmed" and booking.get("call_mode") == "voice_callback":
        settings = db.get_settings()
        v_url = settings.get("webhook_voice_callback_url", "").strip() or settings.get("webhook_bookings_url", "").strip()
        v_sec = settings.get("webhook_secret", "").strip()
        if v_url:
            voice_payload = {
                "event": "voice_callback.confirmed",
                "booking_id": booking["id"],
                "customer_name": booking["customer_name"],
                "phone_number": booking["customer_contact"],
                "customer_contact": booking["customer_contact"],
                "service_type": booking["service_type"],
                "preferred_date": booking["preferred_date"],
                "preferred_time": booking["preferred_time"],
                "notes": booking.get("notes", ""),
                "call_mode": booking["call_mode"],
                "tenant_id": booking.get("tenant_id", "default"),
                "vapi_payload": {
                    "phoneNumber": booking["customer_contact"],
                    "customer": {"name": booking["customer_name"]},
                    "assistantOverrides": {
                        "variableValues": {
                            "customer_name": booking["customer_name"],
                            "service_type": booking["service_type"],
                            "booking_id": str(booking["id"]),
                        }
                    },
                },
                "twilio_payload": {
                    "To": booking["customer_contact"],
                    "Parameters": {
                        "customer_name": booking["customer_name"],
                        "service": booking["service_type"],
                        "booking_id": str(booking["id"]),
                    },
                },
            }
            asyncio.create_task(dispatch_webhook("voice_callback.confirmed", voice_payload, v_url, v_sec))
            db.mark_booking_voice_dispatched(booking_id)
            booking["voice_dispatched"] = 1
            voice_dispatched = True

    return {"booking": booking, "voice_hook_dispatched": voice_dispatched}


@app.delete("/api/bookings/{booking_id}")
def remove_booking(booking_id: int, user: str = Depends(require_admin)):
    if not db.delete_booking(booking_id):
        raise HTTPException(status_code=404, detail="Booking not found")
    db.add_audit_log(user, "booking_delete", "booking", str(booking_id), "Deleted booking")
    return {"deleted": True}


@app.get("/api/trackables")
def get_trackables(tenant_id: Optional[str] = None, _: str = Depends(require_admin)):
    return {"items": db.list_trackables(tenant_id=tenant_id)}


@app.post("/api/trackables")
def save_trackable(payload: TrackablePayload, user: str = Depends(require_admin)):
    item = db.upsert_trackable(
        tracking_code=payload.tracking_code,
        customer_name=payload.customer_name,
        item_title=payload.item_title,
        category=payload.category,
        status=payload.status,
        eta=payload.eta,
        last_update=payload.last_update,
        tenant_id=payload.tenant_id or "default",
    )
    db.add_audit_log(user, "trackable_upsert", "tracking", payload.tracking_code, f"Status: {payload.status}")
    return {"item": item}


@app.delete("/api/trackables/{item_id}")
def remove_trackable(item_id: int, user: str = Depends(require_admin)):
    if not db.delete_trackable(item_id):
        raise HTTPException(status_code=404, detail="Tracking item not found")
    db.add_audit_log(user, "trackable_delete", "tracking", str(item_id), "Deleted trackable item")
    return {"deleted": True}


@app.get("/api/settings")
def read_settings(_: str = Depends(require_admin)):
    return {"settings": db.get_settings()}


@app.put("/api/settings")
def write_settings(payload: SettingsPayload, user: str = Depends(require_admin)):
    updates: Dict[str, str] = {
        k: v for k, v in payload.model_dump().items() if v is not None
    }
    updated = db.update_settings(updates)
    db.add_audit_log(user, "settings_update", "settings", None, f"Updated keys: {', '.join(updates.keys())}")
    return {"settings": updated}


@app.get("/api/audit-logs")
def get_audit_logs(limit: int = 100, _: str = Depends(require_admin)):
    return {"logs": db.list_audit_logs(limit=limit)}
