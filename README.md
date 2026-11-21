# Owfy Enterprise AI Customer Support Platform

Enterprise AI customer support platform built with **Python FastAPI**, **Groq API** (`openai/gpt-oss-20b` or `llama-3.3-70b-versatile`) with function calling, **SQLite** conversation memory & storage, **mode-scoped Knowledge Base RAG**, **automatic PII masking**, **HMAC Bearer Token Admin Authentication**, **SMTP email escalation**, and a quiet-tech frontend with embeddable widget.

---

## Architecture & Features

- **Multi-Mode Support Routing**:
  - `it_support`: Diagnostic triage, network/VPN troubleshooting, severity classification (P1-P4), IT ticket tracking.
  - `business_booking`: Appointment & voice callback scheduling, real-time order tracking, hours & pricing Q&A.
  - `hybrid`: Unified agent supporting technical triage, booking workflows, and automated escalation.
- **Privacy & Security**:
  - Automatic regex-based PII redaction (email, phone, credit card numbers) before external LLM inference.
  - HMAC-signed Bearer tokens for admin dashboard and REST APIs.
  - In-memory per-IP sliding window rate limiting.
  - Configurable CORS origin restriction.
- **Tool Calling**:
  - `lookup_tracking`: Real-time order and IT ticket tracking (e.g. `OWF-1001`, `IT-2045`).
  - `create_booking`: Direct booking creation into SQLite database.
- **Channels & Frontend**:
  - `/` — Quiet-Tech landing page with 3 mode showcases and interactive demo launcher.
  - `/chat` — Dedicated chat UI with Markdown, Web Speech mic input / TTS voice replies, action cards, and CSAT feedback.
  - `/admin` — Protected administrative dashboard managing Tickets, Bookings & Calls, Tracking, Memory, Knowledge Base, and System Settings.
  - `/widget.js` — Single-line embeddable floating chat widget for external websites.

---

## Local Development

### 1. Prerequisites & Environment
Ensure Python 3.10+ (or Python 3.12/3.15) is installed:
```bash
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure `.env`
Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```
Fill in your credentials:
- `GROQ_API_KEY`: Get a free key at [console.groq.com/keys](https://console.groq.com/keys).
- `ADMIN_USER` & `ADMIN_PASS`: Set your administrator credentials.
- `ADMIN_SECRET`: Set a secure random string for HMAC token signing.
- `SMTP_USER` & `SMTP_PASS`: (Optional) Gmail App Password or SMTP provider for email tickets.

> **CRITICAL**: Never commit `.env` or `support_agent.db`. They are excluded by `.gitignore`.

### 4. Run the Server
```bash
python -m uvicorn backend.main:app --reload --port 8000
```

### 5. Access Endpoints
- **Landing Page:** [http://localhost:8000/](http://localhost:8000/)
- **IT Support Chat:** [http://localhost:8000/chat?mode=it_support](http://localhost:8000/chat?mode=it_support)
- **Business Booking Chat:** [http://localhost:8000/chat?mode=business_booking](http://localhost:8000/chat?mode=business_booking)
- **Hybrid Chat:** [http://localhost:8000/chat?mode=hybrid](http://localhost:8000/chat?mode=hybrid)
- **Admin Dashboard:** [http://localhost:8000/admin](http://localhost:8000/admin)
- **Floating Widget Script:** [http://localhost:8000/widget.js](http://localhost:8000/widget.js)

---

## Embedding the Widget

Add this single line before the closing `</body>` tag on any webpage:
```html
<script src="https://your-domain.com/widget.js"></script>
```

---

## Deployment Options

### Option A: Render (Free Tier Web Service)
1. Push your repository to GitHub.
2. In Render Dashboard, click **New +** -> **Web Service**.
3. Select your GitHub repository.
4. Set Environment to **Docker** (Render detects the `Dockerfile` automatically).
5. Add Environment Variables in Render:
   - `GROQ_API_KEY`
   - `ADMIN_USER`
   - `ADMIN_PASS`
   - `ADMIN_SECRET`
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SUPPORT_EMAIL`
   - `ALLOWED_ORIGINS` (set to `https://<your-render-subdomain>.onrender.com`)
6. Deploy!

### Option B: Linux VPS with Docker Compose & Nginx / Caddy (HTTPS)
1. Clone the repository onto your VPS.
2. Create `.env` from `.env.example`.
3. Launch with Docker Compose:
   ```bash
   docker compose up -d --build
   ```
4. Set up Caddy or Nginx reverse proxy with SSL to `http://localhost:8000`.
