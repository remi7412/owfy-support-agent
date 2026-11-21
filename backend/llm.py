import json
import os
from typing import Any, Dict, List, Optional, Tuple
import httpx

from backend import database as db
from backend.security import mask_pii

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MODE_INSTRUCTIONS = {
    "it_support": (
        "MODE: IT SUPPORT SPECIALIST. Focus strictly on technical troubleshooting, networking, OS, security, and IT ticket triage. "
        "Ask concise diagnostic questions (OS, exact error, recent changes), give numbered resolution steps, and assess severity (P1-P4). "
        "Do NOT bring up unrelated business pricing or salon/store bookings."
    ),
    "business_booking": (
        "MODE: LOCAL BUSINESS & BOOKING CONCIERGE. Focus on helping customers book appointments or voice callbacks, "
        "check order/project tracking codes, and answer service, hours, and pricing questions clearly."
    ),
    "hybrid": (
        "MODE: HYBRID ENTERPRISE AGENT. You handle both IT technical triage AND business service bookings, "
        "voice callbacks, and order/ticket tracking. Use lookup_tracking or create_booking tools whenever appropriate."
    ),
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_tracking",
            "description": "Look up live status, ETA, and latest update for an order, project, or IT ticket tracking code (e.g., OWF-1001, IT-2045).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tracking_code": {"type": "string", "description": "The tracking ID such as OWF-1001 or IT-2045"}
                },
                "required": ["tracking_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_booking",
            "description": "Book an appointment, consultation, or automated voice callback when the user provides their name, contact (phone/email), service, date, and time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string"},
                    "customer_contact": {"type": "string"},
                    "service_type": {"type": "string"},
                    "preferred_date": {"type": "string"},
                    "preferred_time": {"type": "string"},
                    "call_mode": {"type": "string", "enum": ["voice_callback", "video", "in_person"]},
                    "notes": {"type": "string"},
                },
                "required": ["customer_name", "customer_contact", "service_type", "preferred_date", "preferred_time"],
            },
        },
    },
]


def _execute_tool(name: str, args: Dict[str, Any], session_id: str, tenant_id: str = "default") -> Tuple[str, Optional[Dict[str, Any]]]:
    if name == "lookup_tracking":
        code = args.get("tracking_code", "")
        item = db.get_trackable_by_code(code, tenant_id=tenant_id)
        if not item:
            return json.dumps({"found": False, "tracking_code": code}), None
        return json.dumps({"found": True, "item": item}), {"type": "tracking", "data": item}

    if name == "create_booking":
        booking = db.create_booking(
            session_id=session_id,
            customer_name=args.get("customer_name", "Guest"),
            customer_contact=args.get("customer_contact", ""),
            service_type=args.get("service_type", "Consultation"),
            preferred_date=args.get("preferred_date", "Next available"),
            preferred_time=args.get("preferred_time", "Morning"),
            call_mode=args.get("call_mode", "voice_callback"),
            notes=args.get("notes", ""),
            tenant_id=tenant_id,
        )
        return json.dumps({"booked": True, "booking": booking}), {"type": "booking", "data": booking}

    return json.dumps({"error": "Unknown tool"}), None


async def generate_support_reply(
    session_id: str,
    user_message: str,
    history: List[Dict[str, Any]],
    retrieved_chunks: List[Dict[str, Any]],
    settings: Dict[str, str],
    mode_override: Optional[str] = None,
    tenant_id: str = "default",
) -> Tuple[str, bool, Optional[Dict[str, Any]]]:
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    business_name = settings.get("business_name", "Owfy")
    model = settings.get("groq_model", "openai/gpt-oss-20b")
    effective_mode = mode_override if mode_override in MODE_INSTRUCTIONS else settings.get("agent_mode", "hybrid")
    mode_text = MODE_INSTRUCTIONS.get(effective_mode, MODE_INSTRUCTIONS["hybrid"])
    base_prompt = settings.get("system_prompt", "").format(business_name=business_name)

    if retrieved_chunks:
        kb_context = "\n\n".join(
            f"[{c['title']} | {c['category']}]\n{c['content']}"
            for c in retrieved_chunks
        )
    else:
        kb_context = "No matching articles found in the knowledge base."

    system_content = (
        f"{base_prompt}\n\n{mode_text}\n\n"
        f"--- BUSINESS KNOWLEDGE BASE ---\n{kb_context}\n--- END KNOWLEDGE BASE ---"
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_content}]
    for msg in history[-10:]:
        if msg["role"] in ("user", "assistant"):
            content = mask_pii(msg["content"]) if msg["role"] == "user" else msg["content"]
            messages.append({"role": msg["role"], "content": content})

    messages.append({"role": "user", "content": mask_pii(user_message)})

    if not api_key or api_key.startswith("gsk_your_"):
        if retrieved_chunks:
            top = retrieved_chunks[0]
            return f"**[Demo Mode]** Based on **{top['title']}**: {top['content']}", False, None
        return "**[Demo Mode]** Add `GROQ_API_KEY` in `.env` for live Groq responses.", True, None

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": messages,
        "tools": TOOLS,
        "tool_choice": "auto",
        "temperature": 0.3,
        "max_tokens": 700,
    }

    action_card: Optional[Dict[str, Any]] = None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(GROQ_API_URL, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            choice_msg = data["choices"][0]["message"]

            if choice_msg.get("tool_calls"):
                messages.append(choice_msg)
                for tc in choice_msg["tool_calls"]:
                    fn_name = tc["function"]["name"]
                    fn_args = json.loads(tc["function"].get("arguments") or "{}")
                    result_str, card = _execute_tool(fn_name, fn_args, session_id, tenant_id=tenant_id)
                    if card:
                        action_card = card
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result_str,
                    })

                resp2 = await client.post(
                    GROQ_API_URL,
                    headers=headers,
                    json={"model": model, "messages": messages, "temperature": 0.3, "max_tokens": 700},
                )
                resp2.raise_for_status()
                raw_reply = (resp2.json()["choices"][0]["message"].get("content") or "").strip()
            else:
                raw_reply = (choice_msg.get("content") or "").strip()

    except httpx.HTTPStatusError as e:
        return (
            f"I ran into an API error from Groq ({e.response.status_code}). "
            "Please check your `GROQ_API_KEY` or try again—or open an email ticket below.",
            True,
            None,
        )
    except Exception:
        return (
            "I'm having trouble connecting to the AI service right now. "
            "You can submit an email ticket below and our team will follow up.",
            True,
            None,
        )

    should_escalate = "[ESCALATE]" in raw_reply
    clean_reply = raw_reply.replace("[ESCALATE]", "").strip()
    return clean_reply, should_escalate, action_card
