import os
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from database import get_db
from models import User, AuthToken

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-me-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")

security = HTTPBearer(auto_error=False)


def create_jwt(data: dict) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_telegram_hash(payload: dict) -> bool:
    bot_token = TELEGRAM_BOT_TOKEN
    if not bot_token:
        return False
    received_hash = payload.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(payload.items()))
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    computed_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return computed_hash == received_hash


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    try:
        token_str = credentials.credentials
        if token_str.startswith("trial_"):
            token = db.query(AuthToken).filter(
                AuthToken.token_string == token_str,
                AuthToken.is_active == True,
            ).first()
            if not token:
                raise HTTPException(status_code=403, detail="Invalid token")
            if token.quota_used >= token.quota_limit:
                raise HTTPException(status_code=403, detail="Quota exceeded")
            user = token.user
            if not user:
                user = User(username="trial_user", is_admin=False)
                db.add(user)
                db.flush()
                token.user_id = user.id
                db.commit()
            return user

        payload = jwt.decode(token_str, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("user_id")
        if user_id is None:
            raise HTTPException(status_code=403, detail="Invalid token")
        user = db.query(User).filter(User.id == user_id).first()
        if user is None:
            raise HTTPException(status_code=403, detail="User not found")
        return user
    except JWTError:
        raise HTTPException(status_code=403, detail="Invalid token")


def get_admin_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return current_user


def check_quota(user: User, db: Session) -> None:
    total_used = db.query(AuthToken).filter(
        AuthToken.user_id == user.id,
        AuthToken.is_active == True,
    ).with_entities(AuthToken.quota_used).all()
    total_limit = db.query(AuthToken).filter(
        AuthToken.user_id == user.id,
        AuthToken.is_active == True,
    ).with_entities(AuthToken.quota_limit).all()
    used = sum(r[0] for r in total_used)
    limit = sum(r[0] for r in total_limit)
    if limit > 0 and used >= limit:
        raise HTTPException(status_code=403, detail="Quota exceeded")
