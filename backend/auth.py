# auth.py — OAuth2 Mercado Livre · Nexora
import httpx
import redis.asyncio as aioredis
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import RedirectResponse
from config import get_settings
from database import get_pool

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()

ML_AUTH_URL   = "https://auth.mercadolivre.com.br/authorization"
ML_TOKEN_URL  = "https://api.mercadolibre.com/oauth/token"
ML_ME_URL     = "https://api.mercadolibre.com/users/me"

# ── Redis para cache de tokens ────────────────────────────
async def get_redis():
    return aioredis.from_url(settings.redis_url, decode_responses=True)

# ── Gera URL de autorização ───────────────────────────────
@router.get("/login")
async def login():
    url = (
        f"{ML_AUTH_URL}"
        f"?response_type=code"
        f"&client_id={settings.ml_client_id}"
        f"&redirect_uri={settings.ml_redirect_uri}"
    )
    return RedirectResponse(url)

# ── Callback — troca code por token ──────────────────────
@router.get("/callback")
async def callback(code: str):
    async with httpx.AsyncClient() as client:
        resp = await client.post(ML_TOKEN_URL, data={
            "grant_type":    "authorization_code",
            "client_id":     settings.ml_client_id,
            "client_secret": settings.ml_client_secret,
            "code":          code,
            "redirect_uri":  settings.ml_redirect_uri,
        })

    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Falha ao obter token do ML")

    token_data = resp.json()
    access_token  = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in    = token_data.get("expires_in", 21600)
    ml_user_id    = str(token_data["user_id"])

    # Buscar dados do usuário no ML
    async with httpx.AsyncClient() as client:
        me = await client.get(ML_ME_URL, headers={"Authorization": f"Bearer {access_token}"})
    user_data = me.json()

    # Salvar/atualizar usuário no banco
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO usuarios (ml_user_id, nickname, email, access_token, refresh_token, token_expires)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (ml_user_id) DO UPDATE SET
                access_token  = EXCLUDED.access_token,
                refresh_token = EXCLUDED.refresh_token,
                token_expires = EXCLUDED.token_expires,
                atualizado_em = NOW()
        """,
            ml_user_id,
            user_data.get("nickname"),
            user_data.get("email"),
            access_token,
            refresh_token,
            datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )

    # Cache do token no Redis (expira junto com o token)
    r = await get_redis()
    await r.setex(f"token:{ml_user_id}", expires_in, access_token)

    # Redireciona ao frontend com token + nickname
    nickname = user_data.get("nickname", ml_user_id)
    frontend_url = "https://pedrohr0212.github.io/gestao-marketplace-ml/index.html"
    return RedirectResponse(f"{frontend_url}?ml_user_id={ml_user_id}&token={access_token}&nickname={nickname}")

# ── Refresh de token ──────────────────────────────────────
async def refresh_token_ml(ml_user_id: str, refresh_token: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(ML_TOKEN_URL, data={
            "grant_type":    "refresh_token",
            "client_id":     settings.ml_client_id,
            "client_secret": settings.ml_client_secret,
            "refresh_token": refresh_token,
        })

    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Token expirado. Faça login novamente.")

    token_data    = resp.json()
    access_token  = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in    = token_data.get("expires_in", 21600)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE usuarios SET
                access_token  = $1,
                refresh_token = $2,
                token_expires = $3,
                atualizado_em = NOW()
            WHERE ml_user_id = $4
        """, access_token, refresh_token,
            datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            ml_user_id)

    r = await get_redis()
    await r.setex(f"token:{ml_user_id}", expires_in, access_token)
    return access_token

# ── Obtém token válido (do Redis ou banco) ────────────────
async def get_valid_token(ml_user_id: str) -> str:
    r = await get_redis()
    token = await r.get(f"token:{ml_user_id}")
    if token:
        return token

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT access_token, refresh_token, token_expires FROM usuarios WHERE ml_user_id = $1",
            ml_user_id
        )

    if not row:
        raise HTTPException(status_code=401, detail="Usuário não encontrado. Faça login.")

    if row["token_expires"] > datetime.now(timezone.utc):
        return row["access_token"]

    return await refresh_token_ml(ml_user_id, row["refresh_token"])
