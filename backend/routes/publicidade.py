# routes/publicidade.py — Nexora Gestão Marketplace ML
import httpx
import asyncio
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



async def buscar_pedidos_ml(headers, seller_id, date_from, date_to):
    """Busca todos os pedidos pagos do período para cruzar com campanhas."""
    all_orders = []
    offset = 0
    limit  = 50
    df_str = date_from.strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
    dt_str = date_to.strftime("%Y-%m-%dT%H:%M:%S.999-03:00")
    while True:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                f"{ML_API}/orders/search",
                headers=headers,
                params={
                    "seller": seller_id, "sort": "date_desc",
                    "offset": offset, "limit": limit,
                    "order.status": "paid",
                    "order.date_created.from": df_str,
                    "order.date_created.to":   dt_str,
                }
            )
        if resp.status_code != 200:
            break
        data    = resp.json()
        results = data.get("results", [])
        all_orders.extend(results)
        if len(results) < limit:
            break
        offset += limit
        if offset > 2000:
            break
    return all_orders

async def buscar_todos_ad_items(client, headers, advertiser_id):
    """Busca todos os itens anunciados do advertiser com seus campaign_ids."""
    item_camp_map = {}  # item_id -> [campaign_ids]
    offset = 0
    while True:
        try:
            resp = await client.get(
                f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{advertiser_id}/product_ads/search",
                headers=headers,
                params={"limit": 100, "offset": offset}
            )
            print(f"[ADS] ad_items status={resp.status_code} body={resp.text[:200]}")
            if resp.status_code != 200:
                break
            data    = resp.json()
            results = data.get("results", [])
            for r in results:
                iid   = str(r.get("item_id", "")).replace("MLB", "").strip()
                cid   = str(r.get("campaign_id", ""))
                if iid:
                    if iid not in item_camp_map:
                        item_camp_map[iid] = []
                    if cid and cid not in item_camp_map[iid]:
                        item_camp_map[iid].append(cid)
            if len(results) < 100:
                break
            offset += 100
        except Exception as e:
            print(f"[ADS] ad_items error: {e}")
            break
    return item_camp_map


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
    headers_v1 = {**headers, "api-version": "1"}

    date_from, date_to = get_date_range(periodo)

    # ── 1. Buscar advertiser_id
    async with httpx.AsyncClient(timeout=15) as client:
        resp_adv = await client.get(
            f"{ML_API}/advertising/advertisers",
            headers=headers_v1,
            params={"product_id": "PADS"}
        )
    if resp_adv.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Erro ao buscar advertiser ({resp_adv.status_code})",
        }

    advertisers   = resp_adv.json().get("advertisers", [])
    adv_mlb       = [a for a in advertisers if a.get("site_id") == SITE_ID]
    if not adv_mlb:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": "Nenhum advertiser MLB encontrado",
        }
    advertiser_id = adv_mlb[0]["advertiser_id"]

    # ── 2. Buscar campanhas + métricas + vendas em paralelo
    async with httpx.AsyncClient(timeout=30) as client:
        resp_camp, resp_vendas = await asyncio.gather(
            client.get(
                f"{ML_API}/marketplace/advertising/{SITE_ID}/advertisers/{advertiser_id}/product_ads/campaigns/search",
                headers=headers,
                params={
                    "limit": 50, "offset": 0,
                    "date_from": date_from, "date_to": date_to,
                    "metrics": METRICS_FIELDS, "metrics_summary": "true",
                }
            ),
            client.get(
                f"{ML_API}/users/me",
                headers={"Authorization": f"Bearer {token}"}
            )
        )

    print(f"[ADS] campaigns status={resp_camp.status_code}")
    if resp_camp.status_code != 200:
        return {
            "campanhas": [], "investimento": 0, "receita_ads": 0,
            "impressoes": 0, "cliques": 0, "roas": 0, "acos": 0,
            "mensagem": f"Erro ao buscar campanhas ({resp_camp.status_code}): {resp_camp.text[:200]}",
        }

    seller_id = resp_vendas.json().get("id") if resp_vendas.status_code == 200 else None
    camp_list = resp_camp.json().get("results", [])

    # ── 3. Buscar itens de cada campanha + vendas em paralelo
    date_from_dt = datetime.strptime(date_from, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, tzinfo=BRT)
    date_to_dt   = datetime.strptime(date_to, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=BRT)

    headers_v1_auth = {"Authorization": f"Bearer {token}"}

    # Buscar todos os itens anunciados e seus campaign_ids em uma única chamada
    async with httpx.AsyncClient(timeout=30) as client:
        item_camp_map = await buscar_todos_ad_items(client, headers, advertiser_id)
    # Montar: camp_id -> [item_ids]
    camp_items_map = {}
    for iid, cids in item_camp_map.items():
        for cid in cids:
            if cid not in camp_items_map:
                camp_items_map[cid] = []
            camp_items_map[cid].append(iid)

    # Buscar pedidos diretamente da API ML para cruzamento
    orders = []
    if seller_id:
        try:
            df_utc = date_from_dt.astimezone(timezone.utc)
            dt_utc = date_to_dt.astimezone(timezone.utc)
            orders = await buscar_pedidos_ml(headers_v1_auth, seller_id, df_utc, dt_utc)
        except Exception as e:
            print(f"[ADS] erro ao buscar vendas: {e}")

    # Montar mapa item_id → faturamento total no período
    fat_por_item = {}
    for order in orders:
        if order.get("status") != "paid":
            continue
        for item in order.get("order_items", []):
            iid = str(item.get("item", {}).get("id", "")).replace("MLB", "").strip()
            val = float(item.get("unit_price", 0)) * int(item.get("quantity", 1))
            fat_por_item[iid] = fat_por_item.get(iid, 0) + val

    # ── 4. Montar campanhas com TACOS real
    campanhas    = []
    investimento = 0.0
    receita_ads  = 0.0
    impressoes   = 0
    cliques      = 0

    for c in camp_list:
        item_ids = camp_items_map.get(str(c.get("id","")), [])
        m   = c.get("metrics") or c.get("metrics_summary") or {}
        inv = float(m.get("cost", 0) or 0)
        rec = float(m.get("total_amount", m.get("direct_amount", 0)) or 0)
        imp = int(m.get("prints", 0) or 0)
        cli = int(m.get("clicks", 0) or 0)

        investimento += inv
        receita_ads  += rec
        impressoes   += imp
        cliques      += cli

        # TACOS real: investimento / faturamento total dos produtos da campanha
        fat_campanha = sum(fat_por_item.get(iid, 0) for iid in item_ids)
        tacos_real   = round(inv / fat_campanha * 100, 2) if fat_campanha > 0 else 0

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
            "tacos":        tacos_real,
            "fat_produtos": round(fat_campanha, 2),
        })

    roas = receita_ads / investimento if investimento > 0 else 0
    acos = (investimento / receita_ads * 100) if receita_ads > 0 else 0
    fat_total = sum(fat_por_item.values())
    tacos_conta = round(investimento / fat_total * 100, 2) if fat_total > 0 else 0

    return {
        "campanhas":     campanhas,
        "investimento":  round(investimento, 2),
        "receita_ads":   round(receita_ads, 2),
        "impressoes":    impressoes,
        "cliques":       cliques,
        "roas":          round(roas, 2),
        "acos":          round(acos, 2),
        "tacos":         tacos_conta,
        "fat_total":     round(fat_total, 2),
        "advertiser_id": advertiser_id,
        "date_from":     date_from,
        "date_to":       date_to,
    }
