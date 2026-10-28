# Lester — the Virtual IT Assistant

Internal IT helpdesk chatbot for Capital Vacations employees. Lester answers
questions from an uploaded knowledge base (RAG with cited sources), creates
osTicket support tickets, and runs entirely on self-hosted infrastructure —
no per-question API costs.

**Live at:** https://lesterai.capitalvacations.com (M365 login required)

## Features

- **RAG chat** — hybrid retrieval (SQLite FTS5 keyword + semantic embeddings,
  reciprocal-rank fusion) over admin-uploaded documents; answers cite sources
  and refuse to invent policy.
- **Microsoft 365 / Entra ID login** — open to all employees; admin features
  gated by an Entra security group (`ADMIN_GROUP_ID`) plus break-glass
  `ADMIN_EMAILS`.
- **Ticket creation** — "Create a ticket" flow posts to the osTicket API with
  resort/location picker (251 locations), urgency → osTicket priority,
  remote/in-office, contact preference, phone, computer name (auto-detected
  via reverse DNS + manual field), and free-text details.
- **Admin page** — upload/view/edit/delete documents, user feedback (👍/👎)
  review, per-question metrics with CSV export, and a system-status banner
  (green/red) shown to all users.
- **Question logging** — every question/answer is recorded in the `queries`
  table for metrics.

## Architecture

```
users ──HTTPS──> Caddy ──> FastAPI app (Docker, LXC "Chatbot-container")
                             │  SQLite knowledge.db (FTS5 + vectors)
                             │
                             └──HTTP──> Ollama (LXC "lester-ollama", GPU)
                                          llama3.2:3b + nomic-embed-text
                                          NVIDIA Tesla P40 (24 GB)
```

- **App host:** Proxmox LXC running Docker Compose (`app` + `caddy`),
  HA-managed on the cluster. All state lives in `./data/knowledge.db`.
- **AI host:** dedicated Proxmox node with a Tesla P40 passed into an LXC;
  Ollama serves the chat + embedding models. The app reaches it via
  `OLLAMA_URL` — swap models/hosts without touching the app.
- osTicket is cloud-hosted; its API key is **IP-locked to the company's
  public egress IP** — if ticket creation returns "valid API key required,"
  check whether the egress IP changed.

## Configuration

Copy `.env.example` to `.env` and fill in the `CHANGE-ME` values
(session secret, Entra client secret, osTicket API key). The `.env` lives
**only on the server** — never commit it.

## Deploying changes

On the app container (`/opt/capvac-chatbot`):

```bash
./deploy.sh        # git pull + rebuild + restart the app (few seconds)
```

Notes:
- Plain `docker compose restart` does **not** reload `.env` — use
  `docker compose up -d --force-recreate app` after env changes.
- SOPs live in `sops/*.md` but the knowledge base is loaded through the
  admin page (or `reload_sops.py`); editing a file here does not update the
  live knowledge base by itself.
- osTicket content must be **ASCII only** — a 4-byte emoji silently truncates
  the ticket in osTicket's MySQL `utf8` database.
