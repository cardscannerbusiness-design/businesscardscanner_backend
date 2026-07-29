# NameCardScan — Backend API

Standalone **Python-only** FastAPI service. All integrations (Zoho CRM, OCR, WhatsApp, email, PostgreSQL) run here — there is no Node/Express backend.

The React app in `frontend/` talks to this API over HTTP. Vite dev proxies `/api`, `/contacts`, `/health`, etc. to port 5000. Card OCR runs in the browser, not on this API.

## Structure

```
backend/
  main.py              # FastAPI app + /health
  run.py               # Local dev entry (uvicorn)
  requirements.txt
  config/
    settings.py        # CORS + app metadata
  api/
    schemas.py         # Pydantic request/response models
    outreach.py        # WhatsApp/email scheduling helpers
    routes/
      ocr.py           # POST /api/ocr (AWS Textract online OCR)
      contacts.py      # Contact CRUD + Zoho sync
      leads.py         # /api/leads/* (Zoho CRM)
      integrations.py  # WhatsApp/email queues + thank-you
      admin.py         # POST /admin/wipe-all-data
  services/
    zoho_service.py    # Zoho OAuth + leads API
    textract_service.py  # AWS Textract OCR (online mode)
    whatsapp_service.py
    email_service.py
    local_db_service.py  # PostgreSQL (psycopg2, same schema as Prisma)
  utils/
  scripts/
```

## Quick start

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# Edit .env — FRONTEND_BASE_URL, BACKEND_BASE_URL, DATABASE_URL, JWT, SMTP, AWS, etc.
python run.py
```

URLs are env-driven (`HOST`/`PORT`, `BACKEND_BASE_URL`). After start:
- API: `{BACKEND_BASE_URL}`
- Swagger: `{BACKEND_BASE_URL}/docs`
- Health: `{BACKEND_BASE_URL}/health`

## Run frontend + backend together

```powershell
cd BusinessCardScanner_Frontend
npm install
# Set VITE_API_URL in .env to match BACKEND_BASE_URL
npm run dev
```

Vite proxies `/api` (and related paths) to `VITE_API_URL`.

## Zoho CRM

Configure in `.env`:

```
ZOHO_CLIENT_ID=
ZOHO_CLIENT_SECRET=
ZOHO_REFRESH_TOKEN=
ZOHO_API_DOMAIN=https://www.zohoapis.com
```

Endpoints used by the frontend:

| Action | Route |
|--------|-------|
| Create lead | `POST /api/leads/create` |
| Sync from local payload | `POST /api/leads/sync-from-local` |
| Sync stored contact | `POST /contacts/{id}/sync-to-zoho` |
| Sync all pending | `POST /contacts/sync-pending-to-zoho` |
| List leads | `GET /api/leads` |

CLI: `python scripts/sync-one-to-zoho.py [contact_id]`

## PostgreSQL

Set `DATABASE_URL` and `CONTACT_STORAGE=postgresql`. Apply schema once from `main/`:

```powershell
cd main
npm run db:push
```

Contact CRUD is served by Python (`/api/contacts`) — no separate Node local-db server.

## Scripts

| Script | Purpose |
|--------|---------|
| `scripts/sync_env_from_main.py` | Copy `main/.env` → `backend/.env` |
| `scripts/sync_env_to_frontend.py` | Copy VITE vars → `frontend/.env` |
| `scripts/sync-one-to-zoho.py` | Sync one contact to Zoho manually |
| `scripts/wipe_all_data.py` | Wipe DB + optional Zoho (`npm run wipe:all` from frontend) |
| `scripts/test_email_send.py` | Test email delivery |
| `scripts/test_whatsapp_send.py` | Test WhatsApp delivery |

## Production notes

- Card OCR: **AWS Textract** (online, `POST /api/ocr`) + **PaddleOCR** (offline, browser).
- Backend handles Zoho sync, WhatsApp, email (SMTP), and PostgreSQL when configured.
- Copy `BusinessCardScanner_Backend/.env.production.example` → server `.env`, fill secrets, restart:
  - `sudo systemctl restart business-card`
- That template already sets:
  - `FRONTEND_BASE_URL=https://www.namecardscan.com`
  - `BACKEND_BASE_URL=https://api.namecardscan.com`
  - `ALLOWED_ORIGINS=https://www.namecardscan.com,https://namecardscan.com`
- Do not use Netlify URLs; they are rejected by URL helpers.

## Removed (do not use)

- `main/backend/` — old Express + Node Zoho/OCR stack (deleted)
- `main/server/local-db.ts` — Node Prisma API on :3001 (replaced by Python `/api/contacts`)
