"""Microsoft Entra ID (M365) sign-in via MSAL, with a dev bypass for local testing."""

import msal
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from . import config

router = APIRouter()

SCOPES = ["User.Read"]

_DEV_USER = {"name": "Dev User", "email": "dev@local"}


def _msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        config.AZURE_CLIENT_ID,
        client_credential=config.AZURE_CLIENT_SECRET,
        authority=f"https://login.microsoftonline.com/{config.AZURE_TENANT_ID}",
    )


def current_user(request: Request) -> dict | None:
    if not config.AUTH_ENABLED:
        return _DEV_USER
    return request.session.get("user")


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not signed in")
    return user


def is_admin(user: dict | None) -> bool:
    """Admin if auth is off (dev), or the user's email is in ADMIN_EMAILS, or the
    user belongs to the configured Entra admin group."""
    if user is None:
        return False
    if not config.AUTH_ENABLED:
        return True
    if (user.get("email") or "").lower() in config.ADMIN_EMAILS:
        return True
    if config.ADMIN_GROUP_ID and config.ADMIN_GROUP_ID in (user.get("groups") or []):
        return True
    return False


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not is_admin(user):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("/login")
def login(request: Request):
    if not config.AUTH_ENABLED:
        return RedirectResponse("/")
    flow = _msal_app().initiate_auth_code_flow(
        SCOPES, redirect_uri=config.AUTH_REDIRECT_URI
    )
    request.session["auth_flow"] = flow
    return RedirectResponse(flow["auth_uri"])


@router.get("/auth/callback")
def auth_callback(request: Request):
    flow = request.session.pop("auth_flow", None)
    if not flow:
        return RedirectResponse("/login")
    result = _msal_app().acquire_token_by_auth_code_flow(
        flow, dict(request.query_params)
    )
    if "error" in result:
        raise HTTPException(status_code=401, detail=result.get("error_description", "Sign-in failed"))
    claims = result.get("id_token_claims", {})
    request.session["user"] = {
        "name": claims.get("name", ""),
        "email": (claims.get("preferred_username") or claims.get("email") or "").lower(),
        "groups": claims.get("groups", []),
    }
    return RedirectResponse("/")


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(
        f"https://login.microsoftonline.com/{config.AZURE_TENANT_ID}/oauth2/v2.0/logout"
        if config.AUTH_ENABLED else "/"
    )
