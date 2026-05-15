# routes/publicidade.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/publicidade", tags=["publicidade"])
ML_API = "https://api.mercadolibre.com"

@router.get("")
async def get_publicidade(
    ml_user_id: str = Query(...),
    periodo: str    = Query("30d"),
):
    token   = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    now       = datetime.now(timezone.utc)
    days_map  = {"hoje": 1, "7d": 7, "30d": 30, "mes": now.day}
    days      = days_map.get(periodo, 30)
    date_from = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    date_to   = now.strftime("%Y-%m-%d")

    # Buscar campanhas
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ML_API}/advertising/product_ads/advertisers/{seller_id}/campaigns",
            headers=headers,
            params={"date_from": date_from, "date_to": date_to}
        )

    if resp.status_code != 200:
        return {
            "campanhas":    [],
            "investimento": 0,
            "receita_ads":  0,
            "impressoes":   0,
            "cliques":      0,
            "roas":         0,
            "acos":         0,
            "mensagem":     "ADS não disponível ou sem campanhas ativas",
        }

    campanhas_raw = resp.json().get("results", [])
    campanhas     = []
    investimento  = 0.0
    receita_ads   = 0.0
    impressoes    = 0
    cliques       = 0

    for c in campanhas_raw:
        inv  = float(c.get("cost",       0))
        rec  = float(c.get("revenue",    0))
        imp  = int(c.get("impressions",  0))
        cli  = int(c.get("clicks",       0))
        investimento += inv
        receita_ads  += rec
        impressoes   += imp
        cliques      += cli

        roas_c  = rec / inv if inv > 0 else 0
        acos_c  = (inv / rec * 100) if rec > 0 else 0
        campanhas.append({
            "id":          c.get("id"),
            "nome":        c.get("name", "Campanha"),
            "status":      c.get("status"),
            "investimento": inv,
            "receita":     rec,
            "impressoes":  imp,
            "cliques":     cli,
            "roas":        round(roas_c, 2),
            "acos":        round(acos_c, 2),
        })

    roas  = receita_ads / investimento if investimento > 0 else 0
    acos  = (investimento / receita_ads * 100) if receita_ads > 0 else 0

    return {
        "campanhas":    campanhas,
        "investimento": investimento,
        "receita_ads":  receita_ads,
        "impressoes":   impressoes,
        "cliques":      cliques,
        "roas":         round(roas, 2),
        "acos":         round(acos, 2),
        "tacos":        round(acos, 2),
    }
