import json
import os
from typing import Any, Dict, List, Optional, Tuple
import httpx

from backend import database as db
from backend.security import mask_pii

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

MODE_INSTRUCTIONS = {
    "it_support": (
        "MODE: IT SUPPORT SPECIALIST & 4-STAGE 'AI-FIRST, TICKET-LAST' DIAGNOSTIC ENGINE.\n"
        "Guidelines:\n"
        "1. For P1 department-wide / critical infrastructure outages (e.g., entire office offline, core server down, payment gateway outage): "
        "call `render_it_diagnostic_flow` with is_p1_outage=true, severity='P1', and append [ESCALATE] to offer immediate human ticket escalation.\n"
        "2. For L1/L2 individual user technical issues (Wi-Fi/Ethernet connectivity, VPN failure, DNS resolution, printer offline, Outlook/email sync, slow PC, software crashes): "
        "ALWAYS invoke the `render_it_diagnostic_flow` tool first with 3-5 ordered, actionable diagnostic steps before suggesting a ticket. "
        "Withhold [ESCALATE] while walking the user through the diagnostic wizard steps.\n"
        "3. Step design in `render_it_diagnostic_flow`: each step must contain a clear step title, plain-language instruction, an optional safe Windows command (e.g. 'ipconfig /all', 'ping 8.8.8.8 -n 4', 'tracert 8.8.8.8', 'nslookup google.com', 'ipconfig /flushdns', 'netsh wlan show interfaces'), and expected normal result.\n"
        "4. Command output diagnosis: when the user pastes command output or error messages, analyze the technical root cause:\n"
        "   - APIPA address (169.254.x.x) -> DHCP server failed to assign an IP. Advise 'ipconfig /release' then 'ipconfig /renew', or checking router DHCP scope.\n"
        "   - Successful ping to 8.8.8.8 but 'nslookup' fails -> DNS failure. Recommend setting DNS to 1.1.1.1 / 8.8.8.8 or running 'ipconfig /flushdns'.\n"
        "   - Gateway unreachable or 'Destination host unreachable' -> Local Wi-Fi or Ethernet cable link failure.\n"
        "   - 'Request timed out' to gateway -> Local subnet/firewall block or router offline.\n"
        "5. Escalation: only append [ESCALATE] if all troubleshooting steps have failed or if the user explicitly insists on human handoff.\n"
        "Do NOT bring up unrelated salon or retail store bookings."
    ),
    "business_booking": (
        "MODE: LOCAL BUSINESS & BOOKING CONCIERGE. Focus on helping customers book appointments or voice callbacks, "
        "check order/project tracking codes, and answer service, hours, and pricing questions clearly."
    ),
    "hybrid": (
        "MODE: HYBRID ENTERPRISE AGENT. You handle both IT technical triage AND business service bookings, "
        "voice callbacks, and order/ticket tracking. Use lookup_tracking, create_booking, or render_it_diagnostic_flow tools whenever appropriate.\n"
        "When handling IT technical issues, follow the 4-stage AI-First diagnostic engine: call `render_it_diagnostic_flow` first for L1/L2 technical issues and withhold [ESCALATE] until diagnostic steps are exhausted."
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
    {
        "type": "function",
        "function": {
            "name": "render_it_diagnostic_flow",
            "description": "Render an interactive visual flowchart and step-by-step diagnostic wizard for IT, network, VPN, printer, OS, or hardware/software issues.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_title": {
                        "type": "string",
                        "description": "Short clear summary of the issue (e.g., 'Internet / Wi-Fi Connectivity', 'VPN Gateway Timeout', 'Printer Spooler Offline')",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["P1", "P2", "P3", "P4"],
                        "description": "P1 (Critical Outage, department-wide), P2 (High impact, workflow blocked), P3 (Medium, single user degraded), P4 (Low, minor inquiry)",
                    },
                    "is_p1_outage": {
                        "type": "boolean",
                        "description": "True if department-wide or critical infrastructure outage allowing immediate human ticket escalation",
                    },
                    "steps": {
                        "type": "array",
                        "description": "Ordered diagnostic steps for guided user troubleshooting",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step_number": {"type": "integer"},
                                "title": {"type": "string", "description": "Short step title, e.g. 'Check Wi-Fi Link' or 'Ping Gateway'"},
                                "instruction": {"type": "string", "description": "Actionable instructions for the user"},
                                "command": {"type": "string", "description": "Optional safe CLI command, e.g. 'ipconfig /all' or 'ping 8.8.8.8'"},
                                "expected_result": {"type": "string", "description": "What normal or healthy output looks like"},
                            },
                            "required": ["step_number", "title", "instruction", "expected_result"],
                        },
                    },
                },
                "required": ["issue_title", "severity", "is_p1_outage", "steps"],
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

    if name == "render_it_diagnostic_flow":
        return json.dumps({"rendered": True, "flow": args}), {"type": "it_diagnostic", "data": args}

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
        if effective_mode in ("it_support", "hybrid") and any(
            k in user_message.lower()
            for k in ["internet", "wifi", "wi-fi", "network", "offline", "vpn", "printer", "dns", "ping", "slow", "down", "broken", "connect"]
        ):
            demo_flow = {
                "issue_title": "Network & Internet Connectivity Diagnostic",
                "severity": "P3",
                "is_p1_outage": False,
                "steps": [
                    {
                        "step_number": 1,
                        "title": "Check Physical & Wi-Fi Link",
                        "instruction": "Verify that Wi-Fi is toggled ON in Windows Quick Settings or Ethernet cable is firmly seated with a solid link light.",
                        "command": "netsh wlan show interfaces",
                        "expected_result": "State: connected with a valid SSID and signal strength > 70%."
                    },
                    {
                        "step_number": 2,
                        "title": "Verify IP Address & Gateway (DHCP)",
                        "instruction": "Open Command Prompt and check your assigned IPv4 address. If it begins with 169.254.x.x, DHCP has failed.",
                        "command": "ipconfig /all",
                        "expected_result": "Valid IPv4 address (e.g. 192.168.1.x or 10.x.x.x) and Default Gateway listed."
                    },
                    {
                        "step_number": 3,
                        "title": "Test Gateway & Public Reachability",
                        "instruction": "Ping Google Public DNS (8.8.8.8) to verify outbound IP packets can leave your local router.",
                        "command": "ping 8.8.8.8 -n 4",
                        "expected_result": "4 packets sent, 4 packets received, 0% packet loss with latency < 50ms."
                    },
                    {
                        "step_number": 4,
                        "title": "Test Domain Name Resolution (DNS)",
                        "instruction": "Verify that domain names can be translated to IP addresses by querying your configured DNS server.",
                        "command": "nslookup google.com",
                        "expected_result": "Non-authoritative answer showing valid public IP addresses."
                    }
                ]
            }
            return (
                "**[AI Diagnostic Engine]** I have initiated a structured 4-step diagnostic wizard for your connectivity issue. "
                "Please follow the active step in the interactive card below or download the 1-click Windows Net Check script.",
                False,
                {"type": "it_diagnostic", "data": demo_flow}
            )

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

            if not raw_reply and action_card:
                if action_card.get("type") == "it_diagnostic":
                    raw_reply = (
                        f"I've initialized a guided diagnostic flow for **{action_card['data'].get('issue_title', 'your issue')}**. "
                        "Follow each step in the visual wizard below, copy the suggested commands, and check the expected results."
                    )
                elif action_card.get("type") == "tracking":
                    raw_reply = f"Here is the status for tracking code **{action_card['data'].get('tracking_code')}**."
                elif action_card.get("type") == "booking":
                    raw_reply = f"Your booking #{action_card['data'].get('id')} has been created."

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
