import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# "anthropic" (Claude API) or "ollama" (free, self-hosted)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-8")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
# Local embedding model used for semantic search (runs on the same Ollama server).
OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")

SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-insecure-secret")
DATABASE_PATH = str(BASE_DIR / os.getenv("DATABASE_PATH", "knowledge.db"))

# Mark session cookies Secure (HTTPS-only). Enable in production behind HTTPS.
COOKIE_SECURE = _bool("COOKIE_SECURE")

AUTH_ENABLED = _bool("AUTH_ENABLED")
AZURE_TENANT_ID = os.getenv("AZURE_TENANT_ID", "")
AZURE_CLIENT_ID = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
AUTH_REDIRECT_URI = os.getenv("AUTH_REDIRECT_URI", "http://localhost:8000/auth/callback")

ADMIN_EMAILS = {
    e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()
}

# --- Ticketing (osTicket API) ---
# Full endpoint, e.g. https://support.capitalvacations.com/api/tickets.json
TICKET_API_URL = os.getenv("TICKET_API_URL", "")
TICKET_API_KEY = os.getenv("TICKET_API_KEY", "")
TICKET_TOPIC_ID = os.getenv("TICKET_TOPIC_ID", "")  # optional osTicket Help Topic id
# Default values for required osTicket custom fields, as a JSON object keyed by field
# variable, e.g. {"category": "General IT", "priority": "Normal", ...}
try:
    TICKET_DEFAULTS = json.loads(os.getenv("TICKET_DEFAULTS") or "{}")
except json.JSONDecodeError:
    TICKET_DEFAULTS = {}

# Entra ID (Azure AD) security group Object ID whose members are admins.
# Members of this group can upload/manage documents. Leave blank to use ADMIN_EMAILS only.
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID", "").strip()
