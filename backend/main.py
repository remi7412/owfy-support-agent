import os
from pathlib import Path
from typing import Dict, Optional
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from backend import database as db
from backend.emailer import send_escalation_email
from backend.llm import generate_support_reply
from backend.rag import retrieve_relevant_chunks
from backend.security import (
    check_rate_limit,
    create_access_token,
    require_admin,
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


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    mode: Optional[str] = None
    visitor_name: Optional[str] = "Guest"
    visitor_email: Optional[str] = ""


class FeedbackRequest(BaseModel):
    message_id: int
    rating: int = Field(..., ge=-1, le=1)


class KnowledgePayload(BaseModel):
    title: str = Field(..., min_length=1)
    category: str = "General"
    mode_scope: str = "all"
    content: str = Field(..., min_length=1)


class TicketPayload(BaseModel):
    session_id: str
    customer_name: str = "Guest"
    customer_email: str = Field(..., min_length=3)
    subject: str = Field(..., min_length=2)
    summary: Optional[str] = ""


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


class SettingsPayload(BaseModel):
    business_name: Optional[str] = None
    groq_model: Optional[str] = None
    support_email: Optional[str] = None
    agent_mode: Optional[str] = None
    system_prompt: Optional[str] = None


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
    if not verify_admin_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"access_token": create_access_token(payload.username), "token_type": "bearer"}


# --- Public Customer Routes ---
@app.get("/api/health")
def health_check():
    return {"status": "ok"}


@app.get("/api/public/settings")
def read_public_settings():
    settings = db.get_settings()
    return {
        "business_name": settings.get("business_name", "Owfy"),
        "agent_mode": settings.get("agent_mode", "hybrid"),
    }


@app.get("/api/chat/history/{session_id}")
def get_public_session_history(session_id: str):
    return {"session_id": session_id, "messages": db.get_session_messages(session_id, limit=50)}


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    check_rate_limit(request, "chat", max_requests=20, window_sec=60)
    db.ensure_session(req.session_id, req.visitor_name or "Guest", req.visitor_email or "")
    history = db.get_session_messages(req.session_id, limit=12)
    settings = db.get_settings()

    effective_mode = req.mode if req.mode in ("it_support", "business_booking", "hybrid") else settings.get("agent_mode", "hybrid")
    docs = db.list_knowledge_docs(mode_filter=effective_mode)

    chunks = retrieve_relevant_chunks(req.message, docs, top_k=3)
    sources = [{"title": c["title"], "category": c["category"], "score": c["score"]} for c in chunks]

    reply, should_escalate, action_card = await generate_support_reply(
        session_id=req.session_id,
        user_message=req.message,
        history=history,
        retrieved_chunks=chunks,
        settings=settings,
        mode_override=effective_mode,
    )

    db.add_message(req.session_id, "user", req.message)
    msg_id = db.add_message(req.session_id, "assistant", reply, sources=sources, escalated=should_escalate)

    return {
        "session_id": req.session_id,
        "message_id": msg_id,
        "reply": reply,
        "sources": sources,
        "suggest_escalation": should_escalate,
        "action_card": action_card,
    }


@app.post("/api/feedback")
def submit_feedback(payload: FeedbackRequest):
    if not db.set_message_feedback(payload.message_id, payload.rating):
        raise HTTPException(status_code=404, detail="Message not found")
    return {"ok": True}


@app.post("/api/bookings")
def create_public_booking(payload: BookingPayload, request: Request):
    check_rate_limit(request, "bookings", max_requests=10, window_sec=60)
    db.ensure_session(payload.session_id, payload.customer_name, payload.customer_contact)
    booking = db.create_booking(
        session_id=payload.session_id,
        customer_name=payload.customer_name,
        customer_contact=payload.customer_contact,
        service_type=payload.service_type,
        preferred_date=payload.preferred_date,
        preferred_time=payload.preferred_time,
        call_mode=payload.call_mode,
        notes=payload.notes or "",
    )
    return {"booking": booking}


@app.get("/api/track/{tracking_code}")
def lookup_public_tracking(tracking_code: str, request: Request):
    check_rate_limit(request, "track", max_requests=30, window_sec=60)
    item = db.get_trackable_by_code(tracking_code)
    if not item:
        raise HTTPException(status_code=404, detail=f"No record found for tracking code '{tracking_code}'")
    return {"item": item}


@app.post("/api/tickets")
def submit_ticket(payload: TicketPayload, request: Request):
    check_rate_limit(request, "tickets", max_requests=5, window_sec=60)
    db.ensure_session(payload.session_id, payload.customer_name, payload.customer_email)
    settings = db.get_settings()

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
    return {"ticket": updated}


# --- Protected Admin Routes ---
@app.get("/api/sessions")
def get_sessions(_: str = Depends(require_admin)):
    return {"sessions": db.list_sessions(), "feedback": db.get_feedback_stats()}


@app.get("/api/sessions/{session_id}")
def get_session_history(session_id: str, _: str = Depends(require_admin)):
    return {"session_id": session_id, "messages": db.get_session_messages(session_id, limit=100)}


@app.get("/api/knowledge")
def get_knowledge(_: str = Depends(require_admin)):
    return {"docs": db.list_knowledge_docs()}


@app.post("/api/knowledge")
def create_knowledge(payload: KnowledgePayload, _: str = Depends(require_admin)):
    doc = db.add_knowledge_doc(payload.title, payload.category, payload.content, payload.mode_scope)
    return {"doc": doc}


@app.put("/api/knowledge/{doc_id}")
def edit_knowledge(doc_id: int, payload: KnowledgePayload, _: str = Depends(require_admin)):
    doc = db.update_knowledge_doc(doc_id, payload.title, payload.category, payload.content, payload.mode_scope)
    if not doc:
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return {"doc": doc}


@app.delete("/api/knowledge/{doc_id}")
def remove_knowledge(doc_id: int, _: str = Depends(require_admin)):
    if not db.delete_knowledge_doc(doc_id):
        raise HTTPException(status_code=404, detail="Knowledge document not found")
    return {"deleted": True}


@app.get("/api/knowledge/search")
def search_knowledge(q: str, _: str = Depends(require_admin)):
    docs = db.list_knowledge_docs()
    return {"results": retrieve_relevant_chunks(q, docs, top_k=5)}


@app.get("/api/tickets")
def get_tickets(_: str = Depends(require_admin)):
    return {"tickets": db.list_tickets()}


@app.patch("/api/tickets/{ticket_id}")
def patch_ticket(ticket_id: int, payload: TicketStatusPayload, _: str = Depends(require_admin)):
    ticket = db.update_ticket_status(ticket_id, payload.status)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"ticket": ticket}


@app.delete("/api/tickets/{ticket_id}")
def remove_ticket(ticket_id: int, _: str = Depends(require_admin)):
    if not db.delete_ticket(ticket_id):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return {"deleted": True}


@app.get("/api/bookings")
def get_bookings(_: str = Depends(require_admin)):
    return {"bookings": db.list_bookings()}


@app.patch("/api/bookings/{booking_id}")
def patch_booking(booking_id: int, payload: BookingStatusPayload, _: str = Depends(require_admin)):
    booking = db.update_booking_status(booking_id, payload.status)
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"booking": booking}


@app.delete("/api/bookings/{booking_id}")
def remove_booking(booking_id: int, _: str = Depends(require_admin)):
    if not db.delete_booking(booking_id):
        raise HTTPException(status_code=404, detail="Booking not found")
    return {"deleted": True}


@app.get("/api/trackables")
def get_trackables(_: str = Depends(require_admin)):
    return {"items": db.list_trackables()}


@app.post("/api/trackables")
def save_trackable(payload: TrackablePayload, _: str = Depends(require_admin)):
    item = db.upsert_trackable(
        tracking_code=payload.tracking_code,
        customer_name=payload.customer_name,
        item_title=payload.item_title,
        category=payload.category,
        status=payload.status,
        eta=payload.eta,
        last_update=payload.last_update,
    )
    return {"item": item}


@app.delete("/api/trackables/{item_id}")
def remove_trackable(item_id: int, _: str = Depends(require_admin)):
    if not db.delete_trackable(item_id):
        raise HTTPException(status_code=404, detail="Tracking item not found")
    return {"deleted": True}


@app.get("/api/settings")
def read_settings(_: str = Depends(require_admin)):
    return {"settings": db.get_settings()}


@app.put("/api/settings")
def write_settings(payload: SettingsPayload, _: str = Depends(require_admin)):
    updates: Dict[str, str] = {
        k: v for k, v in payload.model_dump().items() if v is not None
    }
    return {"settings": db.update_settings(updates)}
