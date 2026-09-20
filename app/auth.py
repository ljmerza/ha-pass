"""Admin authentication: bcrypt password hashing and session cookie management."""
import asyncio

import bcrypt
from fastapi import HTTPException, Request, status

from app import database as db
from app.config import settings
from app.ingress import is_ingress_request

# Hash the configured password once at import time so comparisons are fast.
# Guard: no hash when password is empty (add-on mode with ingress-only access).
_hashed: bytes | None = (
    bcrypt.hashpw(settings.admin_password.encode(), bcrypt.gensalt())
    if settings.admin_password
    else None
)

SESSION_COOKIE = "homepass_admin_session"
INGRESS_SENTINEL = "__ingress__"


async def verify_password(plain: str) -> bool:
    if _hashed is None:
        return False
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, bcrypt.checkpw, plain.encode(), _hashed)


async def require_admin(request: Request) -> str:
    """FastAPI dependency — raises 401 if no valid session cookie.

    Ingress requests (pre-authenticated by HA Supervisor) bypass the
    session check and return a sentinel value.
    """
    if is_ingress_request(request):
        return INGRESS_SENTINEL
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    row = await db.get_admin_session(session_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")
    return session_id
