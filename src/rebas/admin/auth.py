"""管理后台鉴权：scrypt 口令散列（stdlib）+ HS256 长效 JWT（PyJWT）。

- 口令只存 scrypt(salt, pw) 散列，参数 n=2^14/r=8/p=1（交互登录档位）；
- JWT 密钥落 .secrets/admin_jwt.secret（gitignored、0600、首次自动生成）——
  换密钥即吊销所有已发 token；
- token 有效期 180 天（用户要求长期；挂 Cloudflare 后再叠加边缘访问控制）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

import jwt

from rebas.config import PROJECT_ROOT

SECRET_PATH = PROJECT_ROOT / ".secrets" / "admin_jwt.secret"
TOKEN_TTL_DAYS = 180
_SCRYPT = dict(n=2**14, r=8, p=1)


def _secret() -> str:
    if SECRET_PATH.exists():
        return SECRET_PATH.read_text(encoding="utf-8").strip()
    SECRET_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    s = secrets.token_urlsafe(48)
    SECRET_PATH.write_text(s, encoding="utf-8")
    SECRET_PATH.chmod(0o600)
    return s


def hash_password(password: str) -> tuple[str, str]:
    """→ (salt_b64, hash_b64)"""
    salt = os.urandom(16)
    h = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return base64.b64encode(salt).decode(), base64.b64encode(h).decode()


def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    try:
        salt = base64.b64decode(salt_b64)
        h = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
        return hmac.compare_digest(h, base64.b64decode(hash_b64))
    except Exception:  # noqa: BLE001 —— 库里散列损坏一律当验证失败
        return False


def issue_token(email: str) -> str:
    now = int(time.time())
    return jwt.encode({"sub": email, "iat": now, "exp": now + TOKEN_TTL_DAYS * 86400},
                      _secret(), algorithm="HS256")


def verify_token(token: str) -> str | None:
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])["sub"]
    except jwt.PyJWTError:
        return None
