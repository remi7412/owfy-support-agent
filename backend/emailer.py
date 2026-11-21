import os
import smtplib
from email.message import EmailMessage
from typing import Tuple


def _send_smtp(msg: EmailMessage, smtp_host: str, smtp_port: int, smtp_user: str, smtp_pass: str) -> None:
    clean_pass = smtp_pass.replace(" ", "")
    if smtp_port == 465:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
            server.login(smtp_user, clean_pass)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, clean_pass)
            server.send_message(msg)


def send_escalation_email(
    ticket_id: int,
    customer_name: str,
    customer_email: str,
    subject: str,
    summary: str,
    support_email: str,
    business_name: str,
) -> Tuple[bool, str]:
    """
    Sends an escalation email to the support team and an auto-confirmation
    email to the customer via SMTP (supports both port 465 SSL and 587 STARTTLS).
    """
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "465"))
    smtp_user = os.getenv("SMTP_USER", "").strip()
    smtp_pass = os.getenv("SMTP_PASS", "").strip()

    if not smtp_host or not smtp_user or not smtp_pass or "your_app_password" in smtp_pass:
        return False, "SMTP not configured; ticket saved in Admin Dashboard."

    team_target = support_email or smtp_user

    # 1. Notification email to support team
    team_msg = EmailMessage()
    team_msg["Subject"] = f"[{business_name} Support #{ticket_id}] {subject}"
    team_msg["From"] = smtp_user
    team_msg["To"] = team_target
    team_msg["Reply-To"] = customer_email
    team_msg.set_content(
        f"New Customer Support Escalation (#{ticket_id})\n"
        f"------------------------------------------\n"
        f"Customer: {customer_name} <{customer_email}>\n"
        f"Subject: {subject}\n\n"
        f"Conversation / Issue Summary:\n{summary}\n"
    )

    try:
        _send_smtp(team_msg, smtp_host, smtp_port, smtp_user, smtp_pass)
    except Exception as exc:
        return False, f"Email delivery failed ({exc}); ticket saved in Admin Dashboard."

    # 2. Auto-confirmation email to customer
    if customer_email and "@" in customer_email:
        cust_msg = EmailMessage()
        cust_msg["Subject"] = f"[{business_name} Support #{ticket_id}] We received your request"
        cust_msg["From"] = smtp_user
        cust_msg["To"] = customer_email
        cust_msg["Reply-To"] = team_target
        cust_msg.set_content(
            f"Hi {customer_name},\n\n"
            f"Thanks for contacting {business_name} Support. We've logged your request as Ticket #{ticket_id} "
            f"regarding \"{subject}\".\n\n"
            f"Our team will review your conversation and reply to this email shortly.\n\n"
            f"Best regards,\n{business_name} Support"
        )
        try:
            _send_smtp(cust_msg, smtp_host, smtp_port, smtp_user, smtp_pass)
        except Exception:
            return True, f"Email sent to {team_target} (customer confirmation skipped)."

    return True, f"Email sent to {team_target} & confirmation sent to {customer_email}."
