# routes/publicidade.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/publicidade", tags=["publicidade"])
ML_API  = "https://api.mercadolibre.com"
SITE_ID = "MLB"
BRT = timezone(timedelta(hours=-3))

METRICS_FIELDS = "clicks,prints,ctr,cost,cpc,acos,roas,direct_amount,indirect_amount,total_amount,organic_units_quantity,direct_items_quantity,indirect_items_quantity,advertising_items_quantity,units_quantity"

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
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "api-version":   "2",
    }

    date_from, date_to = get_date_range(periodo)

    # ── 1. Buscar advertiser_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp_adv = await client.get(
            f"{ML_API}/advertising/advertisers",
            headers={**headers, "Api-Version": "1"},
            params={"product_id": "PADS"}
        )
    if resp_adv.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Erro ao buscar advertiser ({resp_adv.status_code}): {resp_adv.text[:200]}",
        }

    advertisers = resp_adv.json().get("advertisers", [])
    adv_mlb = [a for a in advertisers if a.get("site_id") == SITE_ID]
    if not adv_mlb:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Nenhum advertiser MLB. Total: {advertisers}",
        }

    advertiser_id = adv_mlb[0]["advertiser_id"]

    # ── 2. Buscar campanhas COM métricas em uma única chamada
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{advertiser_id}/product_ads/campaigns",
            headers=headers,
            params={
                "limit":          50,
                "offset":         0,
                "date_from":      date_from,
                "date_to":        date_to,
                "metrics":        METRICS_FIELDS,
                "metrics_summary": "true",
            }
        )

    print(f"[ADS] campaigns+metrics status={resp.status_code} body={resp.text[:400]}")

    if resp.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Erro ao buscar campanhas ({resp.status_code}): {resp.text[:200]}",
        }

    camp_list    = resp.json().get("results", [])
    campanhas    = []
    investimento = 0.0
    receita_ads  = 0.0
    impressoes   = 0
    cliques      = 0

    for c in camp_list:
        m   = c.get("metrics") or c.get("metrics_summary") or {}
        inv = float(m.get("cost", 0) or 0)
        rec = float(m.get("total_amount", m.get("direct_amount", 0)) or 0)
        imp = int(m.get("prints", 0) or 0)
        cli = int(m.get("clicks", 0) or 0)

        investimento += inv
        receita_ads  += rec
        impressoes   += imp
        cliques      += cli

        campanhas.append({
            "id":           c.get("id"),
            "nome":         c.get("name", f"Campanha {c.get('id')}"),
            "status":       c.get("status", "active"),
            "investimento": round(inv, 2),
            "receita":      round(rec, 2),
            "impressoes":   imp,
            "cliques":      cli,
            "roas":         round(float(m.get("roas", 0) or 0), 2),
            "acos":         round(float(m.get("acos", 0) or 0), 2),
        })

    roas = receita_ads / investimento if investimento > 0 else 0
    acos = (investimento / receita_ads * 100) if receita_ads > 0 else 0

    return {
        "campanhas":     campanhas,
        "investimento":  round(investimento, 2),
        "receita_ads":   round(receita_ads, 2),
        "impressoes":    impressoes,
        "cliques":       cliques,
        "roas":          round(roas, 2),
        "acos":          round(acos, 2),
        "tacos":         round(acos, 2),
        "advertiser_id": advertiser_id,
        "date_from":     date_from,
        "date_to":       date_to,
    }
