from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()


def create_access_token(
    subject: str,
    token_id: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    configured_expiry = expires_delta
    if configured_expiry is None:
        if (
            settings.jwt_access_token_expire_seconds is not None
            and int(settings.jwt_access_token_expire_seconds) > 0
        ):
            configured_expiry = timedelta(
                seconds=int(settings.jwt_access_token_expire_seconds)
            )
        else:
            configured_expiry = timedelta(
                minutes=settings.jwt_access_token_expire_minutes
            )

    expire = datetime.now(timezone.utc) + configured_expiry
    to_encode = {"sub": subject, "jti": token_id, "exp": expire}
    return jwt.encode(to_encode, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise ValueError("Invalid or expired token") from exc
