import csv
import io
import socket
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import auth, chat, config, db, ingest, tickets

STATIC_DIR = Path(__file__).resolve().parent / "static"

import json as _json
_LOC_FILE = Path(__file__).resolve().parent / "locations.json"
LOCATIONS = _json.loads(_LOC_FILE.read_text(encoding="utf-8")) if _LOC_FILE.exists() else []

app = FastAPI(title="Capital Vacations Lester the Virtual IT Assistant")
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SECRET_KEY,
    https_only=config.COOKIE_SECURE,
    same_site="lax",
)
app.include_router(auth.router)

db.init_db()

# Serve static assets (JS libraries, etc.) at /static
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------- Pages ----------

@app.get("/")
def index(request: Request):
    if auth.current_user(request) is None:
        return RedirectResponse("/login")
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/admin")
def admin_page(request: Request):
    user = auth.current_user(request)
    if user is None:
        return RedirectResponse("/login")
    if not auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return FileResponse(STATIC_DIR / "admin.html", headers={"Cache-Control": "no-store"})


# ---------- API ----------

@app.get("/api/me")
def me(user: dict = Depends(auth.require_user)):
    return {
        "name": user.get("name", ""),
        "email": user.get("email", ""),
        "is_admin": auth.is_admin(user),
        "ticketing": tickets.is_configured(),
    }


@app.get("/api/locations")
def get_locations(user: dict = Depends(auth.require_user)):
    return LOCATIONS


def _read_status() -> dict:
    row = db.get_setting("system_status")
    data = {"state": "ok", "message": ""}
    if row:
        try:
            data.update(_json.loads(row["value"]))
        except Exception:
            pass
        data["updated_at"] = row.get("updated_at")
        data["updated_by"] = row.get("updated_by")
    if data.get("state") not in ("ok", "down"):
        data["state"] = "ok"
    return data


@app.get("/api/status")
def get_status(user: dict = Depends(auth.require_user)):
    return _read_status()


class StatusUpdate(BaseModel):
    state: str = "ok"          # "ok" (green) | "down" (red)
    message: str = ""


@app.put("/api/status")
def set_status(body: StatusUpdate, user: dict = Depends(auth.require_admin)):
    state = body.state if body.state in ("ok", "down") else "ok"
    payload = _json.dumps({"state": state, "message": body.message.strip()[:1000]})
    db.set_setting("system_status", payload, user.get("email"))
    return _read_status()


class TicketRequest(BaseModel):
    question: str
    answer: str = ""
    location: str | None = None
    phone: str | None = None
    details: str | None = None
    computer: str | None = None
    urgency: str | None = None
    remote: str | None = None
    contact: str | None = None


# Urgency picker -> osTicket priority (1=Low, 2=Normal, 3=High, 4=Emergency;
# High/Emergency render big and red in osTicket). banner is prepended to the
# subject and message so it stands out in the queue. ASCII only — osTicket's DB
# is MySQL utf8 (3-byte), so a 4-byte emoji silently truncates the whole field.
URGENCY = {
    "none":      {"priority": "2", "label": "1 - No",                    "banner": ""},
    "wait":      {"priority": "1", "label": "2 - It can wait a bit",     "banner": ""},
    "hurt":      {"priority": "4", "label": "3 - People could get hurt", "banner": "!! PEOPLE COULD GET HURT !!"},
    "lives":     {"priority": "4", "label": "4 - Lives are on the line", "banner": "!! LIVES ARE ON THE LINE !!"},
    "revenue":   {"priority": "3", "label": "5 - REVENUE IMPACTED",      "banner": "!! REVENUE IMPACTED !!"},
}


def _workstation(request: Request) -> str | None:
    """Best-effort workstation identity: reverse-DNS name + IP from the request.

    The browser can't report the OS computer name, so we capture the client IP
    (via Caddy's X-Forwarded-For) and try a reverse-DNS (PTR) lookup. The name
    only resolves when the container can reach internal DNS; otherwise we return
    just the IP, which is still useful to IT.
    """
    xff = request.headers.get("x-forwarded-for", "")
    client_ip = (xff.split(",")[0].strip() if xff else "") or (request.client.host if request.client else "")
    if not client_ip:
        return None
    # Run the (potentially slow) reverse lookup in a thread we can abandon after
    # 2.5s, so a sluggish DNS reply never delays the ticket POST.
    result = {"host": None}

    def _lookup():
        try:
            result["host"] = socket.gethostbyaddr(client_ip)[0]
        except Exception:
            pass

    t = threading.Thread(target=_lookup, daemon=True)
    t.start()
    t.join(2.5)
    return f"{result['host']} ({client_ip})" if result["host"] else client_ip


@app.post("/api/ticket")
def create_ticket(body: TicketRequest, request: Request, user: dict = Depends(auth.require_user)):
    if not tickets.is_configured():
        raise HTTPException(status_code=503, detail="Ticketing isn't set up")
    email = user.get("email") or ""
    name = user.get("name") or email or "Lester User"
    u = URGENCY.get((body.urgency or "none").strip(), URGENCY["none"])
    base_subject = ("IT Help: " + body.question.strip())[:80] if body.question.strip() else "IT Help Request"
    subject = f"{u['banner']} — {base_subject}" if u["banner"] else base_subject
    loc_label = next((l["label"] for l in LOCATIONS if l["value"] == body.location), None)
    phone = (body.phone or "").strip()
    workstation = _workstation(request)
    remote = "Yes" if (body.remote or "").strip().lower() in ("yes", "true", "1") else "No"
    contact = {"call": "Call", "email": "Email", "teams": "Microsoft Teams"}.get(
        (body.contact or "").strip().lower(), "Email")

    lines = []
    if u["banner"]:
        lines += [u["banner"], ""]
    lines += ["Submitted via Lester (the virtual IT assistant).", ""]
    lines.append(f"Urgency: {u['label']}")
    lines.append(f"Best way to reach them: {contact}")
    lines.append(f"Working remotely: {remote}")
    lines.append(f"Location: {loc_label or 'not specified'}")
    if workstation:
        lines.append(f"Workstation (detected): {workstation}")
    if body.computer and body.computer.strip():
        lines.append(f"Computer name (user-entered): {body.computer.strip()}")
    if phone:
        lines.append(f"Callback phone: {phone}")
    lines += ["", "User's question:", body.question.strip(),
              "", "Lester's answer:", body.answer.strip() or "(no answer was given)"]
    if body.details and body.details.strip():
        lines += ["", "Additional details from the user:", body.details.strip()]
    message = "\n".join(lines)

    try:
        number = tickets.create_ticket(name, email, subject, message,
                                       location=body.location, phone=phone or None,
                                       priority=u["priority"], remote=remote)
        return {"ok": True, "ticket": number}
    except tickets.TicketTimeout:
        # Ticket was almost certainly created; we just didn't get the number back.
        return {"ok": True, "ticket": None, "pending": True}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class ChatRequest(BaseModel):
    messages: list[dict]  # [{role: "user"|"assistant", content: str}]


@app.post("/api/chat")
def api_chat(body: ChatRequest, user: dict = Depends(auth.require_user)):
    if config.LLM_PROVIDER == "anthropic" and not config.ANTHROPIC_API_KEY:
        raise HTTPException(status_code=500, detail="ANTHROPIC_API_KEY is not configured")
    if not body.messages or body.messages[-1].get("role") != "user":
        raise HTTPException(status_code=400, detail="Last message must be from the user")

    question = (body.messages[-1].get("content") or "").strip()
    user_email = user.get("email")

    def logged_stream():
        """Pass the chat stream through to the client while capturing the answer
        and sources, then log the whole exchange for metrics (even on disconnect)."""
        answer_parts: list[str] = []
        sources: list[str] = []
        answered = False
        try:
            for line in chat.stream_chat(body.messages):
                try:
                    ev = _json.loads(line)
                    if ev.get("type") == "text":
                        answer_parts.append(ev.get("text", ""))
                        answered = True
                    elif ev.get("type") == "sources":
                        sources = ev.get("sources", []) or []
                except Exception:
                    pass
                yield line
        finally:
            try:
                db.log_query(user_email, question[:4000], "".join(answer_parts)[:8000],
                             ", ".join(sources)[:1000], answered)
            except Exception:
                pass

    return StreamingResponse(logged_stream(), media_type="application/x-ndjson")


class Feedback(BaseModel):
    question: str
    answer: str = ""
    rating: str  # "up" | "down"
    sources: list[str] = []


@app.post("/api/feedback")
def submit_feedback(body: Feedback, user: dict = Depends(auth.require_user)):
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="Invalid rating")
    fid = db.add_feedback(
        user.get("email"), body.rating,
        body.question[:2000], body.answer[:8000], ", ".join(body.sources)[:1000],
    )
    return {"ok": True, "id": fid}


@app.get("/api/feedback")
def get_feedback(user: dict = Depends(auth.require_admin)):
    return {"counts": db.feedback_counts(), "items": db.list_feedback()}


@app.delete("/api/feedback/{fid}")
def remove_feedback(fid: int, user: dict = Depends(auth.require_admin)):
    if not db.delete_feedback(fid):
        raise HTTPException(status_code=404, detail="Feedback not found")
    return {"ok": True}


@app.get("/api/metrics")
def get_metrics(user: dict = Depends(auth.require_admin)):
    return {"stats": db.query_stats(), "items": db.list_queries(50)}


@app.get("/api/metrics/export")
def export_metrics(user: dict = Depends(auth.require_admin)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "timestamp_utc", "user", "question", "answer", "sources", "answered"])
    for r in db.all_queries():
        w.writerow([r["id"], r["created_at"], r["user"], r["question"],
                    r["answer"], r["sources"], "yes" if r["answered"] else "no"])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=lester-questions.csv"},
    )


@app.get("/api/documents")
def get_documents(user: dict = Depends(auth.require_user)):
    return db.list_documents()


@app.post("/api/documents")
async def upload_documents(files: list[UploadFile], user: dict = Depends(auth.require_admin)):
    results = []
    for f in files:
        data = await f.read()
        try:
            text = ingest.extract_text(f.filename or "file", data)
        except ingest.UnsupportedFileType as e:
            results.append({"filename": f.filename, "ok": False, "error": str(e)})
            continue
        chunks = ingest.chunk_text(text)
        if not chunks:
            results.append({"filename": f.filename, "ok": False,
                            "error": "No readable text found (scanned image PDF?)"})
            continue
        title = Path(f.filename or "Document").stem.replace("_", " ").replace("-", " ").strip()
        doc_id = db.add_document(title, f.filename, "file", user.get("email"), chunks, content=text)
        results.append({"filename": f.filename, "ok": True, "id": doc_id, "chunks": len(chunks)})
    return results


class TextDocument(BaseModel):
    title: str
    content: str


@app.post("/api/documents/text")
def add_text_document(body: TextDocument, user: dict = Depends(auth.require_admin)):
    chunks = ingest.chunk_text(body.content)
    if not body.title.strip() or not chunks:
        raise HTTPException(status_code=400, detail="Title and content are required")
    doc_id = db.add_document(body.title.strip(), None, "text", user.get("email"), chunks, content=body.content)
    return {"ok": True, "id": doc_id, "chunks": len(chunks)}


@app.get("/api/documents/{doc_id}")
def get_document(doc_id: int, user: dict = Depends(auth.require_admin)):
    doc = db.get_document(doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@app.put("/api/documents/{doc_id}")
def update_document(doc_id: int, body: TextDocument, user: dict = Depends(auth.require_admin)):
    chunks = ingest.chunk_text(body.content)
    if not body.title.strip() or not chunks:
        raise HTTPException(status_code=400, detail="Title and content are required")
    if not db.update_document(doc_id, body.title.strip(), body.content, chunks):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True, "id": doc_id, "chunks": len(chunks)}


@app.delete("/api/documents/{doc_id}")
def remove_document(doc_id: int, user: dict = Depends(auth.require_admin)):
    if not db.delete_document(doc_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return {"ok": True}
