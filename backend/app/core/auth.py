"""Xác thực một người dùng quản trị: mật khẩu từ env -> token ký HMAC có hạn."""
import hashlib
import hmac
import time

from fastapi import Header, HTTPException, Query

from .config import settings


def _sign(payload: str) -> str:
    return hmac.new(settings.session_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def check_password(password: str) -> bool:
    if not settings.admin_password:
        return False
    return hmac.compare_digest(password.encode(), settings.admin_password.encode())


def make_token(now: float | None = None) -> str:
    exp = int((now or time.time()) + settings.session_ttl_seconds)
    return f"{exp}.{_sign(str(exp))}"


def verify_token(token: str, now: float | None = None) -> bool:
    if not settings.session_secret:
        return False
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(exp_s))


def require_auth(
    authorization: str | None = Header(default=None), token: str | None = Query(default=None)
) -> None:
    # `token` trên query chỉ dành cho SSE (EventSource không gửi được header)
    t = token
    if authorization and authorization.lower().startswith("bearer "):
        t = authorization[7:]
    if not t or not verify_token(t):
        raise HTTPException(status_code=401, detail="Unauthorized")
