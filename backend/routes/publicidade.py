# routes/publicidade.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/publicidade", tags=["publicidade"])
ML_API  = "https://api.mercadolibre.com"
SITE_ID = "MLB"  # Brasil

BRT = timezone(timedelta(hours=-3))

def get_date_range(periodo: str):
    now = datetime.now(BRT)
    if periodo == "hoje":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif periodo == "ontem":
        d     = now - timedelta(days=1)
        start = d.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = d.replace(hour=23, minute=59, second=59, microsecond=0)
    elif periodo == "7d":
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif periodo == "mes":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif periodo == "mesant":
        last  = now.replace(day=1) - timedelta(days=1)
        start = last.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end   = last.replace(hour=23, minute=59, second=59, microsecond=0)
    else:  # 30d
        start = (now - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")


@router.get("")
async def get_publicidade(
    ml_user_id: str = Query(...),
    periodo:    str = Query("30d"),
):
    token   = await get_valid_token(ml_user_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Api-Version": "2",
    }

    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    if me.status_code != 200:
        raise HTTPException(status_code=401, detail="Erro ao autenticar com o ML")
    seller_id = me.json().get("id")

    date_from, date_to = get_date_range(periodo)

    # ── 1. Listar campanhas (nova API v2)
    async with httpx.AsyncClient(timeout=15) as client:
        resp_camp = await client.get(
            f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{seller_id}/product_ads/campaigns",
            headers=headers,
            params={"status": "active", "limit": 50}
        )

    print(f"[ADS] campaigns status={resp_camp.status_code}")

    # Fallback para API v1 se v2 não funcionar
    if resp_camp.status_code != 200:
        headers_v1 = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=15) as client:
            resp_camp = await client.get(
                f"{ML_API}/advertising/product_ads/advertisers/{seller_id}/campaigns",
                headers=headers_v1,
            )
        print(f"[ADS] campaigns v1 fallback status={resp_camp.status_code}")

    if resp_camp.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"ADS não disponível (status {resp_camp.status_code})",
        }

    camp_data  = resp_camp.json()
    camp_list  = camp_data.get("results", camp_data if isinstance(camp_data, list) else [])

    # ── 2. Buscar métricas por campanha
    campanhas    = []
    investimento = 0.0
    receita_ads  = 0.0
    impressoes   = 0
    cliques      = 0

    for c in camp_list:
        camp_id = c.get("id")
        if not camp_id:
            continue

        # Tentar nova API de métricas
        async with httpx.AsyncClient(timeout=15) as client:
            resp_m = await client.get(
                f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{seller_id}/product_ads/campaigns/{camp_id}/metrics",
                headers=headers,
                params={"date_from": date_from, "date_to": date_to, "granularity": "TOTAL"}
            )

        if resp_m.status_code != 200:
            # Fallback v1
            async with httpx.AsyncClient(timeout=15) as client:
                resp_m = await client.get(
                    f"{ML_API}/advertising/product_ads/advertisers/{seller_id}/campaigns/{camp_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"date_from": date_from, "date_to": date_to}
                )

        print(f"[ADS] metrics camp={camp_id} status={resp_m.status_code} body={resp_m.text[:200]}")

        m_data = resp_m.json() if resp_m.status_code == 200 else {}
        # Tentar diferentes formatos de resposta
        results = m_data.get("results", [m_data] if m_data else [])
        if not results:
            results = [m_data]

        inv = 0.0
        rec = 0.0
        imp = 0
        cli = 0
        for r in results:
            inv += float(r.get("cost",        r.get("spend",       r.get("investment", 0))) or 0)
            rec += float(r.get("revenue",      r.get("sales",       r.get("income",     0))) or 0)
            imp += int(  r.get("impressions",  r.get("prints",      0)) or 0)
            cli += int(  r.get("clicks",       0) or 0)

        investimento += inv
        receita_ads  += rec
        impressoes   += imp
        cliques      += cli

        roas_c = rec / inv if inv > 0 else 0
        acos_c = (inv / rec * 100) if rec > 0 else 0

        campanhas.append({
            "id":          camp_id,
            "nome":        c.get("name", f"Campanha {camp_id}"),
            "status":      c.get("status", "active"),
            "investimento": round(inv, 2),
            "receita":     round(rec, 2),
            "impressoes":  imp,
            "cliques":     cli,
            "roas":        round(roas_c, 2),
            "acos":        round(acos_c, 2),
        })

    roas  = receita_ads / investimento if investimento > 0 else 0
    acos  = (investimento / receita_ads * 100) if receita_ads > 0 else 0

    return {
        "campanhas":    campanhas,
        "investimento": round(investimento, 2),
        "receita_ads":  round(receita_ads, 2),
        "impressoes":   impressoes,
        "cliques":      cliques,
        "roas":         round(roas, 2),
        "acos":         round(acos, 2),
        "tacos":        round(acos, 2),
        "date_from":    date_from,
        "date_to":      date_to,
    }
