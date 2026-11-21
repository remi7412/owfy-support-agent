import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).resolve().parent.parent / "support_agent.db"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(cur: sqlite3.Cursor, table: str, column: str, definition: str) -> None:
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db() -> None:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        visitor_name TEXT DEFAULT 'Guest',
        visitor_email TEXT DEFAULT '',
        status TEXT DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        sources_json TEXT DEFAULT '[]',
        escalated INTEGER DEFAULT 0,
        feedback INTEGER DEFAULT 0,
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS knowledge_docs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        category TEXT DEFAULT 'General',
        mode_scope TEXT DEFAULT 'all',
        content TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        customer_name TEXT DEFAULT 'Guest',
        customer_email TEXT NOT NULL,
        subject TEXT NOT NULL,
        summary TEXT NOT NULL,
        status TEXT DEFAULT 'open',
        email_sent INTEGER DEFAULT 0,
        email_note TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        customer_name TEXT NOT NULL,
        customer_contact TEXT NOT NULL,
        service_type TEXT NOT NULL,
        preferred_date TEXT NOT NULL,
        preferred_time TEXT NOT NULL,
        call_mode TEXT DEFAULT 'voice_callback',
        notes TEXT DEFAULT '',
        status TEXT DEFAULT 'pending',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS trackables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tracking_code TEXT UNIQUE NOT NULL,
        customer_name TEXT NOT NULL,
        item_title TEXT NOT NULL,
        category TEXT DEFAULT 'order',
        status TEXT DEFAULT 'in_progress',
        eta TEXT DEFAULT 'TBD',
        last_update TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS tenants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        business_mode TEXT DEFAULT 'hybrid',
        support_email TEXT DEFAULT '',
        system_prompt TEXT DEFAULT '',
        widget_color TEXT DEFAULT '#141413',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        admin_user TEXT NOT NULL,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        details TEXT,
        ip_address TEXT,
        created_at TEXT NOT NULL
    )
    """)

    # Safe migrations for existing databases
    _ensure_column(cur, "sessions", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "messages", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "knowledge_docs", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "tickets", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "bookings", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "trackables", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "settings", "tenant_id", "TEXT DEFAULT 'default'")
    _ensure_column(cur, "messages", "feedback", "INTEGER DEFAULT 0")
    _ensure_column(cur, "knowledge_docs", "mode_scope", "TEXT DEFAULT 'all'")
    _ensure_column(cur, "sessions", "human_active", "INTEGER DEFAULT 0")
    _ensure_column(cur, "bookings", "voice_dispatched", "INTEGER DEFAULT 0")

    cur.execute("SELECT COUNT(*) FROM tenants")
    if cur.fetchone()[0] == 0:
        cur.execute(
            """INSERT INTO tenants (slug, name, business_mode, support_email, system_prompt, widget_color, created_at)
               VALUES ('default', 'Owfy Default', 'hybrid', ?, '', '#141413', ?)""",
            (os.getenv("SUPPORT_EMAIL", "support@owfy.io"), utc_now()),
        )

    default_settings = {
        "business_name": "Owfy",
        "groq_model": os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        "support_email": os.getenv("SUPPORT_EMAIL", "support@owfy.io"),
        "agent_mode": "hybrid",
        "webhook_tickets_url": os.getenv("WEBHOOK_TICKETS_URL", ""),
        "webhook_bookings_url": os.getenv("WEBHOOK_BOOKINGS_URL", ""),
        "webhook_voice_callback_url": os.getenv("WEBHOOK_VOICE_CALLBACK_URL", ""),
        "webhook_secret": os.getenv("WEBHOOK_SECRET", ""),
        "system_prompt": (
            "You are the AI Customer & Technical Support Specialist for {business_name}. "
            "Answer clearly, concisely, and accurately using the provided Business Knowledge Base and available tools. "
            "If the knowledge base does not contain the answer, or if the customer asks for a human agent, "
            "acknowledge the gap honestly and include the exact tag [ESCALATE] at the end of your reply. "
            "Never invent pricing, policies, or order statuses."
        ),
    }
    for k, v in default_settings.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    cur.execute("SELECT COUNT(*) FROM knowledge_docs")
    if cur.fetchone()[0] == 0:
        now = utc_now()
        starter_docs = [
            (
                "About Owfy & Core Services",
                "Company",
                "business_booking",
                "Owfy provides AI-powered customer support automation, appointment & voice-callback scheduling, and custom web platforms. "
                "Customers can book consultations, schedule local service visits, and track active orders in real time.",
                now,
            ),
            (
                "Support Hours & SLA",
                "Support",
                "all",
                "AI support is available 24/7. Human specialist escalation is monitored Sunday through Thursday, 9:00 AM to 6:00 PM (AST / UTC+3). "
                "Standard email tickets receive a human response within 4 business hours; urgent P1 IT issues are prioritized within 1 hour.",
                now,
            ),
            (
                "Pricing & Onboarding",
                "Billing",
                "business_booking",
                "Owfy offers a Starter plan for small teams, a Growth plan with custom knowledge-base syncing and analytics, and Enterprise custom deployments. "
                "Onboarding typically takes 3 to 5 business days including knowledge-base setup and brand styling.",
                now,
            ),
            (
                "IT Helpdesk & Network Triage SOP",
                "IT Support",
                "it_support",
                "For VPN, Wi-Fi, or Active Directory issues: verify network link, flush DNS (ipconfig /flushdns), check MFA token sync, and capture exact error codes. "
                "P1 outages (system down for all users) are escalated immediately to L2 infrastructure engineers.",
                now,
            ),
            (
                "Data Privacy & Security",
                "Security",
                "all",
                "All customer conversations are isolated by session, PII is masked before external LLM inference, and admin controls are protected by token authentication.",
                now,
            ),
        ]
        cur.executemany(
            "INSERT INTO knowledge_docs (title, category, mode_scope, content, updated_at) VALUES (?, ?, ?, ?, ?)",
            starter_docs,
        )

    cur.execute("SELECT COUNT(*) FROM trackables")
    if cur.fetchone()[0] == 0:
        now = utc_now()
        starter_trackables = [
            ("OWF-1001", "Demo Client", "AI Support Widget Deployment", "project", "in_progress", "Tomorrow 2:00 PM", "Staging environment verified; awaiting DNS cutover.", now),
            ("IT-2045", "Demo Staff", "VPN & Active Directory Access", "it_ticket", "scheduled", "Today 4:30 PM", "L2 engineer assigned for MFA token reset.", now),
        ]
        cur.executemany(
            "INSERT INTO trackables (tracking_code, customer_name, item_title, category, status, eta, last_update, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            starter_trackables,
        )

    conn.commit()
    conn.close()


# --- Tenants ---
def list_tenants() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM tenants ORDER BY id ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tenant(slug_or_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    val = str(slug_or_id).strip()
    row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (val,)).fetchone()
    if not row and val.isdigit():
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (int(val),)).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_tenant(
    slug: str,
    name: str,
    business_mode: str = "hybrid",
    support_email: str = "",
    system_prompt: str = "",
    widget_color: str = "#141413",
) -> Dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    clean_slug = slug.strip().lower().replace(" ", "-")
    conn.execute(
        """INSERT INTO tenants (slug, name, business_mode, support_email, system_prompt, widget_color, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(slug) DO UPDATE SET
               name = excluded.name,
               business_mode = excluded.business_mode,
               support_email = excluded.support_email,
               system_prompt = excluded.system_prompt,
               widget_color = excluded.widget_color""",
        (clean_slug, name.strip(), business_mode, support_email.strip(), system_prompt.strip(), widget_color.strip() or "#141413", now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (clean_slug,)).fetchone()
    conn.close()
    return dict(row)


def delete_tenant(tenant_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM tenants WHERE id = ? AND slug != 'default'", (tenant_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# --- Settings ---
def get_settings() -> Dict[str, str]:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def update_settings(updates: Dict[str, str]) -> Dict[str, str]:
    conn = get_conn()
    for k, v in updates.items():
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (k, str(v)),
        )
    conn.commit()
    conn.close()
    return get_settings()


# --- Sessions & Memory ---
def ensure_session(
    session_id: str,
    visitor_name: str = "Guest",
    visitor_email: str = "",
    tenant_id: str = "default",
) -> None:
    conn = get_conn()
    now = utc_now()
    existing = conn.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not existing:
        conn.execute(
            """INSERT INTO sessions (id, visitor_name, visitor_email, status, tenant_id, human_active, created_at, updated_at)
               VALUES (?, ?, ?, 'active', ?, 0, ?, ?)""",
            (session_id, visitor_name, visitor_email, tenant_id, now, now),
        )
    else:
        conn.execute("UPDATE sessions SET updated_at = ?, tenant_id = ? WHERE id = ?", (now, tenant_id, session_id))
    conn.commit()
    conn.close()


def get_session(session_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_session_human_takeover(session_id: str, active: bool) -> bool:
    conn = get_conn()
    now = utc_now()
    cur = conn.cursor()
    val = 1 if active else 0
    cur.execute(
        """UPDATE sessions
           SET human_active = ?, status = CASE WHEN ? = 1 THEN 'human_takeover' ELSE 'active' END, updated_at = ?
           WHERE id = ?""",
        (val, val, now, session_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def add_message(
    session_id: str,
    role: str,
    content: str,
    sources: Optional[List[Dict[str, Any]]] = None,
    escalated: bool = False,
    tenant_id: str = "default",
) -> int:
    conn = get_conn()
    now = utc_now()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO messages (session_id, role, content, sources_json, escalated, feedback, tenant_id, created_at)
           VALUES (?, ?, ?, ?, ?, 0, ?, ?)""",
        (session_id, role, content, json.dumps(sources or []), 1 if escalated else 0, tenant_id, now),
    )
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    msg_id = cur.lastrowid
    conn.commit()
    conn.close()
    return msg_id


def set_message_feedback(msg_id: int, rating: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE messages SET feedback = ? WHERE id = ?", (rating, msg_id))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def get_feedback_stats(tenant_id: Optional[str] = None) -> Dict[str, int]:
    conn = get_conn()
    if tenant_id:
        up = conn.execute("SELECT COUNT(*) FROM messages WHERE feedback = 1 AND tenant_id = ?", (tenant_id,)).fetchone()[0]
        down = conn.execute("SELECT COUNT(*) FROM messages WHERE feedback = -1 AND tenant_id = ?", (tenant_id,)).fetchone()[0]
    else:
        up = conn.execute("SELECT COUNT(*) FROM messages WHERE feedback = 1").fetchone()[0]
        down = conn.execute("SELECT COUNT(*) FROM messages WHERE feedback = -1").fetchone()[0]
    conn.close()
    total = up + down
    pct = round((up / total) * 100) if total else 100
    return {"up": up, "down": down, "csat_pct": pct}


def get_resolution_analytics(tenant_id: Optional[str] = None) -> Dict[str, Any]:
    conn = get_conn()
    if tenant_id:
        total_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()[0]
        escalated_sessions = conn.execute(
            """SELECT COUNT(DISTINCT session_id) FROM tickets
               WHERE session_id IS NOT NULL AND session_id != '' AND tenant_id = ?""",
            (tenant_id,),
        ).fetchone()[0]
        takeover_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE human_active = 1 AND tenant_id = ?",
            (tenant_id,),
        ).fetchone()[0]
    else:
        total_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        escalated_sessions = conn.execute(
            """SELECT COUNT(DISTINCT session_id) FROM tickets
               WHERE session_id IS NOT NULL AND session_id != ''"""
        ).fetchone()[0]
        takeover_sessions = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE human_active = 1"
        ).fetchone()[0]
    conn.close()

    total_escalated = min(total_sessions, escalated_sessions)
    resolved_sessions = max(0, total_sessions - total_escalated)
    resolution_rate_pct = round((resolved_sessions / total_sessions) * 100) if total_sessions > 0 else 100

    return {
        "total_sessions": total_sessions,
        "escalated_sessions": total_escalated,
        "takeover_sessions": takeover_sessions,
        "resolved_sessions": resolved_sessions,
        "resolution_rate_pct": resolution_rate_pct,
    }


def get_session_messages(session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT id, session_id, role, content, sources_json, escalated, feedback, tenant_id, created_at
           FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?""",
        (session_id, limit),
    ).fetchall()
    conn.close()
    result = []
    for r in reversed(rows):
        result.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "role": r["role"],
            "content": r["content"],
            "sources": json.loads(r["sources_json"] or "[]"),
            "escalated": bool(r["escalated"]),
            "feedback": r["feedback"],
            "tenant_id": r["tenant_id"] if "tenant_id" in r.keys() else "default",
            "created_at": r["created_at"],
        })
    return result


def list_sessions(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    if tenant_id:
        rows = conn.execute("""
            SELECT s.*, COUNT(m.id) AS message_count,
                   MAX(m.content) AS last_message
            FROM sessions s
            LEFT JOIN messages m ON s.id = m.session_id
            WHERE s.tenant_id = ?
            GROUP BY s.id
            ORDER BY s.updated_at DESC
        """, (tenant_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT s.*, COUNT(m.id) AS message_count,
                   MAX(m.content) AS last_message
            FROM sessions s
            LEFT JOIN messages m ON s.id = m.session_id
            GROUP BY s.id
            ORDER BY s.updated_at DESC
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Knowledge Base ---
def list_knowledge_docs(mode_filter: Optional[str] = None, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    clauses = []
    params = []
    if mode_filter and mode_filter in ("it_support", "business_booking"):
        clauses.append("mode_scope IN ('all', ?)")
        params.append(mode_filter)
    if tenant_id:
        clauses.append("tenant_id IN ('default', ?)")
        params.append(tenant_id)
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(f"SELECT * FROM knowledge_docs {where_sql} ORDER BY id DESC", tuple(params)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_knowledge_doc(
    title: str,
    category: str,
    content: str,
    mode_scope: str = "all",
    tenant_id: str = "default",
) -> Dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO knowledge_docs (title, category, mode_scope, content, tenant_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (title, category, mode_scope, content, tenant_id, now),
    )
    doc_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM knowledge_docs WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return dict(row)


def update_knowledge_doc(
    doc_id: int,
    title: str,
    category: str,
    content: str,
    mode_scope: str = "all",
    tenant_id: str = "default",
) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    now = utc_now()
    conn.execute(
        """UPDATE knowledge_docs
           SET title = ?, category = ?, mode_scope = ?, content = ?, tenant_id = ?, updated_at = ?
           WHERE id = ?""",
        (title, category, mode_scope, content, tenant_id, now, doc_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM knowledge_docs WHERE id = ?", (doc_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_knowledge_doc(doc_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM knowledge_docs WHERE id = ?", (doc_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


# --- Tickets ---
def create_ticket(
    session_id: str,
    customer_name: str,
    customer_email: str,
    subject: str,
    summary: str,
    email_sent: bool,
    email_note: str,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO tickets (session_id, customer_name, customer_email, subject, summary, status, email_sent, email_note, tenant_id, created_at)
           VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)""",
        (session_id, customer_name, customer_email, subject, summary, 1 if email_sent else 0, email_note, tenant_id, now),
    )
    conn.execute(
        "UPDATE sessions SET status = 'escalated', visitor_name = ?, visitor_email = ?, tenant_id = ? WHERE id = ?",
        (customer_name, customer_email, tenant_id, session_id),
    )
    ticket_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row)


def list_tickets(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    if tenant_id:
        rows = conn.execute("SELECT * FROM tickets WHERE tenant_id = ? ORDER BY id DESC", (tenant_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM tickets ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_ticket_status(ticket_id: int, status: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))
    conn.commit()
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_ticket(ticket_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


# --- Bookings & Voice Callbacks ---
def create_booking(
    session_id: str,
    customer_name: str,
    customer_contact: str,
    service_type: str,
    preferred_date: str,
    preferred_time: str,
    call_mode: str = "voice_callback",
    notes: str = "",
    tenant_id: str = "default",
) -> Dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO bookings (session_id, customer_name, customer_contact, service_type, preferred_date, preferred_time, call_mode, notes, status, tenant_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (session_id, customer_name, customer_contact, service_type, preferred_date, preferred_time, call_mode, notes, tenant_id, now),
    )
    booking_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    conn.close()
    return dict(row)


def list_bookings(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    if tenant_id:
        rows = conn.execute("SELECT * FROM bookings WHERE tenant_id = ? ORDER BY id DESC", (tenant_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM bookings ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_booking(booking_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def update_booking_status(booking_id: int, status: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    conn.execute("UPDATE bookings SET status = ? WHERE id = ?", (status, booking_id))
    conn.commit()
    row = conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_booking_voice_dispatched(booking_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE bookings SET voice_dispatched = 1 WHERE id = ?", (booking_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def delete_booking(booking_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


# --- Tracking (Orders / Projects / IT Tickets) ---
def get_trackable_by_code(tracking_code: str, tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    clean_code = tracking_code.strip()
    if tenant_id:
        row = conn.execute(
            "SELECT * FROM trackables WHERE UPPER(tracking_code) = UPPER(?) AND tenant_id = ?",
            (clean_code, tenant_id),
        ).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM trackables WHERE UPPER(tracking_code) = UPPER(?)", (clean_code,)).fetchone()
    else:
        row = conn.execute("SELECT * FROM trackables WHERE UPPER(tracking_code) = UPPER(?)", (clean_code,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_trackables(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    if tenant_id:
        rows = conn.execute("SELECT * FROM trackables WHERE tenant_id = ? ORDER BY id DESC", (tenant_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM trackables ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_trackable(
    tracking_code: str,
    customer_name: str,
    item_title: str,
    category: str,
    status: str,
    eta: str,
    last_update: str,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    clean_code = tracking_code.strip().upper()
    conn.execute(
        """INSERT INTO trackables (tracking_code, customer_name, item_title, category, status, eta, last_update, tenant_id, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(tracking_code) DO UPDATE SET
             customer_name = excluded.customer_name,
             item_title = excluded.item_title,
             category = excluded.category,
             status = excluded.status,
             eta = excluded.eta,
             last_update = excluded.last_update,
             tenant_id = excluded.tenant_id,
             updated_at = excluded.updated_at""",
        (clean_code, customer_name, item_title, category, status, eta, last_update, tenant_id, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM trackables WHERE UPPER(tracking_code) = ?", (clean_code,)).fetchone()
    conn.close()
    return dict(row)


def delete_trackable(item_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM trackables WHERE id = ?", (item_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


# --- Security Audit Trail ---
def add_audit_log(
    admin_user: str,
    action: str,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    details: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Dict[str, Any]:
    conn = get_conn()
    now = utc_now()
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO audit_logs (admin_user, action, target_type, target_id, details, ip_address, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (admin_user, action, target_type, target_id, details, ip_address, now),
    )
    log_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM audit_logs WHERE id = ?", (log_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def list_audit_logs(limit: int = 100) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


init_db()
