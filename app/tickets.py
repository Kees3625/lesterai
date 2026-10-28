"""Create tickets in osTicket via its JSON API.

The API key is server-side only (never sent to the browser). osTicket ties the
key to the calling server's IP, so the container's outbound IP must be allowed
for the key in osTicket admin.
"""

import httpx

from . import config


class TicketTimeout(Exception):
    """osTicket didn't respond in time. The ticket was very likely still created
    server-side — we just never got the confirmation/number back."""


def is_configured() -> bool:
    return bool(config.TICKET_API_URL and config.TICKET_API_KEY)


def create_ticket(name: str, email: str, subject: str, message: str,
                  location: str | None = None, phone: str | None = None,
                  priority: str | None = None, remote: str | None = None) -> str:
    """Create a ticket and return its osTicket ticket number. Raises on failure."""
    if not is_configured():
        raise RuntimeError("Ticketing is not configured")

    # Start from configured defaults (required custom fields), then core fields win.
    payload = dict(config.TICKET_DEFAULTS)
    payload.update({
        "name": name or email or "Lester User",
        "email": email,
        "subject": subject[:255] or "IT Help Request",
        "message": message,
    })
    if config.TICKET_TOPIC_ID:
        payload["topicId"] = config.TICKET_TOPIC_ID
    if location:  # user-selected resort/location overrides the default
        payload["locationall"] = location
    if phone:  # user-entered callback phone overrides the placeholder default
        payload["callbackphone"] = phone
    if priority:  # urgency-driven priority overrides the default (osTicket renders High/Emergency in red)
        payload["priority"] = priority
    if remote:  # user-selected remote/in-office overrides the default
        payload["remoteuser"] = remote

    try:
        resp = httpx.post(
            config.TICKET_API_URL,
            json=payload,
            headers={"X-API-Key": config.TICKET_API_KEY, "Content-Type": "application/json"},
            timeout=httpx.Timeout(45.0, connect=10.0),
        )
    except httpx.TimeoutException as e:
        # osTicket is slow but almost always still creates the ticket; surface a
        # distinct exception so the caller can tell the user it's likely fine.
        raise TicketTimeout(str(e)) from e
    if resp.status_code in (200, 201):
        return resp.text.strip().strip('"')
    raise RuntimeError(f"osTicket returned {resp.status_code}: {resp.text[:300]}")
