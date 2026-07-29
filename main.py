import asyncio
import json
import logging
import os
from typing import List, Optional

from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import User, AuthToken, ScrapeHistory
from auth import (
    create_jwt,
    verify_telegram_hash,
    get_current_user,
    get_admin_user,
    check_quota,
)
from ultra_stealth_fetcher import fetch, proxy_fetch, _scraperapi_fetch
from smart_adaptor import SmartAdaptor
from memory_safe_cache import ResponseCache, DomainRateLimiter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("ultra_scraper")

app = FastAPI(title="Ultra Scraper", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_cache: Optional[ResponseCache] = None
_limiter: Optional[DomainRateLimiter] = None


@app.on_event("startup")
async def startup():
    global _cache, _limiter
    init_db()
    _cache = ResponseCache(ttl=3600)
    _limiter = DomainRateLimiter(capacity=10, refill_rate=2.0)
    log.info("Ultra Scraper started")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled: %s", exc)
    return JSONResponse(status_code=200, content={
        "url": str(request.url), "status": 418, "cached": False,
        "method_used": "failed_safely", "impersonation": None,
        "data": {}, "error_message": "Anti-bot system was too aggressive. Try again later.",
    })


# ---------- Schemas ----------

class TelegramAuthPayload(BaseModel):
    id: int
    first_name: str = ""
    username: str = ""
    photo_url: str = ""
    auth_date: int
    hash: str


class TrialAuthPayload(BaseModel):
    token: str


class AuthResponse(BaseModel):
    token: str
    user_id: int
    username: str = ""


class ProxyConfig(BaseModel):
    provider: str
    api_key: str


class ScrapeRequest(BaseModel):
    url: HttpUrl
    selectors: List[str] = []
    proxy_config: Optional[ProxyConfig] = None


class ScrapeResponse(BaseModel):
    url: str
    status: int
    cached: bool
    method_used: str
    impersonation: Optional[str] = None
    data: dict
    error_message: str = ""


class GenerateTokenPayload(BaseModel):
    quota_limit: int = 50


class TokenInfo(BaseModel):
    token_string: str
    quota_limit: int
    quota_used: int
    is_active: bool
    created_at: str


class UserInfo(BaseModel):
    id: int
    username: str = ""
    is_admin: bool
    quota_used: int
    quota_limit: int


class HistoryEntry(BaseModel):
    target_url: str
    selectors_used: str = ""
    status: int
    method_used: str
    created_at: str


class AiAnalyzeResponse(BaseModel):
    selectors: List[str]


# ---------- Auth Endpoints ----------

@app.post("/auth/telegram", response_model=AuthResponse)
async def auth_telegram(payload: TelegramAuthPayload, db: Session = Depends(get_db)):
    data = payload.model_dump()
    if not verify_telegram_hash(data):
        raise HTTPException(status_code=403, detail="Invalid Telegram hash")
    telegram_id = str(payload.id)
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=payload.username or payload.first_name,
            is_admin=False,
        )
        db.add(user)
        db.flush()
        token = AuthToken(user_id=user.id, quota_limit=100, quota_used=0)
        db.add(token)
        db.commit()
    jwt_token = create_jwt({"user_id": user.id})
    return AuthResponse(token=jwt_token, user_id=user.id, username=user.username or "")


@app.post("/auth/trial", response_model=AuthResponse)
async def auth_trial(payload: TrialAuthPayload, db: Session = Depends(get_db)):
    token = db.query(AuthToken).filter(
        AuthToken.token_string == payload.token,
        AuthToken.is_active == True,
    ).first()
    if not token:
        raise HTTPException(status_code=403, detail="Invalid trial token")
    if token.quota_used >= token.quota_limit:
        raise HTTPException(status_code=403, detail="Token quota exhausted")
    user = token.user
    if not user:
        user = User(username="trial_user", is_admin=False)
        db.add(user)
        db.flush()
        token.user_id = user.id
        db.commit()
    jwt_token = create_jwt({"user_id": user.id})
    return AuthResponse(token=jwt_token, user_id=user.id, username=user.username or "")


@app.get("/auth/me", response_model=UserInfo)
async def auth_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    tokens = db.query(AuthToken).filter(AuthToken.user_id == current_user.id).all()
    quota_used = sum(t.quota_used for t in tokens)
    quota_limit = sum(t.quota_limit for t in tokens)
    return UserInfo(
        id=current_user.id,
        username=current_user.username or "",
        is_admin=current_user.is_admin,
        quota_used=quota_used,
        quota_limit=quota_limit,
    )


# ---------- Core Scrape Endpoint ----------

@app.post("/scrape", response_model=ScrapeResponse)
async def scrape_endpoint(
    body: ScrapeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    url = str(body.url)
    try:
        check_quota(current_user, db)

        # BYOK proxy path
        if body.proxy_config:
            result = await proxy_fetch(url, body.proxy_config.model_dump())
        else:
            result = await fetch(url)

        status = result["status"]
        html = result["body"]
        method_used = result.get("method_used", "unknown")
        impersonation = result.get("impersonation")

        extracted = {}
        if body.selectors:
            with SmartAdaptor(html, url=url) as adaptor:
                for sel in body.selectors:
                    texts = [adaptor.text(el) for el in adaptor.css(sel)]
                    extracted[sel] = texts

        all_empty = all(len(v) == 0 for v in extracted.values()) if extracted else False
        if all_empty and method_used == "stealthy_fallback":
            log.warning("Empty data from stealth, trying ScraperAPI rescue...")
            result = await _scraperapi_fetch(url)
            status = result["status"]
            html = result["body"]
            method_used = result["method_used"]
            impersonation = result.get("impersonation")
            extracted = {}
            if body.selectors:
                with SmartAdaptor(html, url=url) as adaptor:
                    for sel in body.selectors:
                        texts = [adaptor.text(el) for el in adaptor.css(sel)]
                        extracted[sel] = texts

        # Increment quota
        token = db.query(AuthToken).filter(
            AuthToken.user_id == current_user.id,
            AuthToken.is_active == True,
        ).first()
        if token:
            token.quota_used += 1

        # Save history
        history = ScrapeHistory(
            user_id=current_user.id,
            target_url=url,
            selectors_used=json.dumps(body.selectors),
            status=status,
            method_used=method_used,
        )
        db.add(history)
        db.commit()

        return ScrapeResponse(
            url=url, status=status, cached=False,
            method_used=method_used, impersonation=impersonation,
            data=extracted,
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error("Scrape failed: %s", e)
        return ScrapeResponse(
            url=url, status=418, cached=False,
            method_used="failed_safely", data={},
            error_message="Anti-bot system was too aggressive. Try again later.",
        )


# ---------- User Endpoints ----------

@app.get("/history", response_model=List[HistoryEntry])
async def get_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    entries = (
        db.query(ScrapeHistory)
        .filter(ScrapeHistory.user_id == current_user.id)
        .order_by(ScrapeHistory.created_at.desc())
        .limit(10)
        .all()
    )
    return [
        HistoryEntry(
            target_url=h.target_url,
            selectors_used=h.selectors_used or "",
            status=h.status,
            method_used=h.method_used,
            created_at=h.created_at.isoformat() if h.created_at else "",
        )
        for h in entries
    ]


@app.get("/user/quota")
async def get_quota(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tokens = db.query(AuthToken).filter(AuthToken.user_id == current_user.id).all()
    quota_used = sum(t.quota_used for t in tokens)
    quota_limit = sum(t.quota_limit for t in tokens)
    return {"quota_used": quota_used, "quota_limit": quota_limit}


# ---------- Admin Endpoints ----------

@app.post("/admin/generate-token", response_model=TokenInfo)
async def admin_generate_token(
    payload: GenerateTokenPayload,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    token = AuthToken(quota_limit=payload.quota_limit, quota_used=0)
    db.add(token)
    db.commit()
    db.refresh(token)
    return TokenInfo(
        token_string=token.token_string,
        quota_limit=token.quota_limit,
        quota_used=token.quota_used,
        is_active=token.is_active,
        created_at=token.created_at.isoformat() if token.created_at else "",
    )


@app.get("/admin/list-tokens", response_model=List[TokenInfo])
async def admin_list_tokens(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    tokens = db.query(AuthToken).order_by(AuthToken.created_at.desc()).all()
    return [
        TokenInfo(
            token_string=t.token_string,
            quota_limit=t.quota_limit,
            quota_used=t.quota_used,
            is_active=t.is_active,
            created_at=t.created_at.isoformat() if t.created_at else "",
        )
        for t in tokens
    ]


# ---------- AI Placeholder ----------

@app.post("/ai-analyze", response_model=AiAnalyzeResponse)
async def ai_analyze(
    body: ScrapeRequest,
    current_user: User = Depends(get_current_user),
):
    return AiAnalyzeResponse(selectors=["h1", ".product-title", ".price"])


# ---------- Health ----------

@app.get("/health")
async def health():
    return {"status": "ok"}
