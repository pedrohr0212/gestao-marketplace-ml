# routes/publicidade.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/publicidade", tags=["publicidade"])
ML_API  = "https://api.mercadolibre.com"
SITE_ID = "MLB"

BRT = timezone(timedelta(hours=-3))

def get_date_range(periodo: str):
    now = datetime.now(BRT)
    if periodo == "hoje":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    elif periodo == "ontem":
        d = now - timedelta(days=1)
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
    headers_v1 = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Api-Version": "1"}
    headers_v2 = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "api-version": "2"}

    date_from, date_to = get_date_range(periodo)

    # ── 1. Buscar advertiser_id (diferente do user_id)
    async with httpx.AsyncClient(timeout=15) as client:
        resp_adv = await client.get(
            f"{ML_API}/advertising/advertisers",
            headers=headers_v1,
            params={"product_id": "PADS"}
        )
    print(f"[ADS] advertisers status={resp_adv.status_code} body={resp_adv.text[:300]}")

    if resp_adv.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Erro ao buscar advertiser_id (status {resp_adv.status_code}): {resp_adv.text[:100]}",
        }

    advertisers = resp_adv.json().get("advertisers", [])
    # Filtrar pelo site_id MLB
    adv_mlb = [a for a in advertisers if a.get("site_id") == SITE_ID]
    if not adv_mlb:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Nenhum advertiser MLB encontrado. Advertisers: {advertisers}",
        }

    advertiser_id = adv_mlb[0]["advertiser_id"]
    print(f"[ADS] advertiser_id={advertiser_id}")

    # ── 2. Buscar campanhas (API v2)
    async with httpx.AsyncClient(timeout=15) as client:
        resp_camp = await client.get(
            f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{advertiser_id}/product_ads/campaigns/search",
            headers=headers_v2,
            params={"limit": 50, "offset": 0}
        )
    print(f"[ADS] campaigns v2 status={resp_camp.status_code} body={resp_camp.text[:300]}")

    if resp_camp.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Erro ao buscar campanhas (status {resp_camp.status_code}): {resp_camp.text[:200]}",
        }

    camp_list = resp_camp.json().get("results", [])
    print(f"[ADS] {len(camp_list)} campanhas encontradas")

    # ── 3. Buscar métricas por campanha
    campanhas    = []
    investimento = 0.0
    receita_ads  = 0.0
    impressoes   = 0
    cliques      = 0

    for c in camp_list:
        camp_id = c.get("id")
        if not camp_id:
            continue

        async with httpx.AsyncClient(timeout=15) as client:
            resp_m = await client.get(
                f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{advertiser_id}/product_ads/campaigns/{camp_id}/metrics",
                headers=headers_v2,
                params={"date_from": date_from, "date_to": date_to, "granularity": "TOTAL"}
            )
        print(f"[ADS] metrics camp={camp_id} status={resp_m.status_code} body={resp_m.text[:200]}")

        m_data = resp_m.json() if resp_m.status_code == 200 else {}
        results = m_data.get("results", [m_data] if m_data else [])

        inv = sum(float(r.get("cost", r.get("spend", 0)) or 0) for r in results)
        rec = sum(float(r.get("revenue", r.get("sales", 0)) or 0) for r in results)
        imp = sum(int(r.get("impressions", r.get("prints", 0)) or 0) for r in results)
        cli = sum(int(r.get("clicks", 0) or 0) for r in results)

        investimento += inv
        receita_ads  += rec
        impressoes   += imp
        cliques      += cli

        campanhas.append({
            "id":           camp_id,
            "nome":         c.get("name", f"Campanha {camp_id}"),
            "status":       c.get("status", "active"),
            "investimento": round(inv, 2),
            "receita":      round(rec, 2),
            "impressoes":   imp,
            "cliques":      cli,
            "roas":         round(rec / inv, 2) if inv > 0 else 0,
            "acos":         round(inv / rec * 100, 2) if rec > 0 else 0,
        })

    roas = receita_ads / investimento if investimento > 0 else 0
    acos = (investimento / receita_ads * 100) if receita_ads > 0 else 0

    return {
        "campanhas":    campanhas,
        "investimento": round(investimento, 2),
        "receita_ads":  round(receita_ads, 2),
        "impressoes":   impressoes,
        "cliques":      cliques,
        "roas":         round(roas, 2),
        "acos":         round(acos, 2),
        "tacos":        round(acos, 2),
        "advertiser_id": advertiser_id,
        "date_from":    date_from,
        "date_to":      date_to,
    }
