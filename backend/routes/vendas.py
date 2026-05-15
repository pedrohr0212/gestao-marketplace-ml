# routes/vendas.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from database import get_pool
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/vendas", tags=["vendas"])

ML_API = "https://api.mercadolibre.com"

# Fuso horário de Brasília = UTC-3
BRT = timezone(timedelta(hours=-3))

def get_date_range(periodo: str):
    # Sempre trabalha em horário de Brasília (BRT = UTC-3)
    now_brt = datetime.now(BRT)

    if periodo == "hoje":
        start = now_brt.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "ontem":
        d     = now_brt - timedelta(days=1)
        start = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = d.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "7d":
        start = (now_brt - timedelta(days=6)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "30d":
        start = (now_brt - timedelta(days=29)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "mes":
        start = now_brt.replace(day=1, hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "mesant":
        last_day_prev = now_brt.replace(day=1) - timedelta(days=1)
        start = last_day_prev.replace(day=1, hour=0,  minute=0,  second=0,  microsecond=0)
        end   = last_day_prev.replace(hour=23, minute=59, second=59, microsecond=0)

    else:
        start = (now_brt - timedelta(days=29)).replace(hour=0, minute=0, second=0, microsecond=0)
        end   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    # Converter para UTC para enviar à API do ML
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

@router.get("")
async def get_vendas(
    ml_user_id: str = Query(...),
    periodo: str    = Query("30d"),
    offset: int     = Query(0),
    limit: int      = Query(50),
):
    token = await get_valid_token(ml_user_id)
    date_from, date_to = get_date_range(periodo)

    headers = {"Authorization": f"Bearer {token}"}

    # Buscar ID do vendedor
    async with httpx.AsyncClient() as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    # Buscar pedidos no ML — ordenado por data decrescente (mais recente primeiro)
    params = {
        "seller":                    seller_id,
        "order.status":              "paid",
        "order.date_created.from":   date_from.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
        "order.date_created.to":     date_to.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
        "sort":                      "date_desc",
        "offset":                    offset,
        "limit":                     min(limit, 50),
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ML_API}/orders/search", headers=headers, params=params)

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Erro ao buscar vendas no ML")

    data   = resp.json()
    orders = data.get("results", [])
    total  = data.get("paging", {}).get("total", 0)

    # Formatar para o frontend — ordenado por data decrescente
    vendas = []
    for order in sorted(orders, key=lambda o: o.get("date_created", ""), reverse=True):
        for item in order.get("order_items", []):
            vendas.append({
                "id":          order["id"],
                "sku":         item["item"].get("seller_sku", ""),
                "nome":        item["item"].get("title", ""),
                "valor":       float(item.get("unit_price", 0)),
                "qtde":        item.get("quantity", 1),
                "data":        order.get("date_created", ""),
                "status":      order.get("status", ""),
                "isPub":       order.get("context", {}).get("channel") == "advertising",
                "cancelada":   order.get("status") == "cancelled",
                "conta":       "ML",
                "ml_order_id": order["id"],
            })

    return {
        "vendas":  vendas,
        "total":   total,
        "offset":  offset,
        "periodo": periodo,
    }

@router.get("/resumo")
async def get_resumo(
    ml_user_id: str = Query(...),
    periodo: str    = Query("30d"),
):
    token = await get_valid_token(ml_user_id)
    date_from, date_to = get_date_range(periodo)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    params = {
        "seller":                  seller_id,
        "order.status":            "paid",
        "order.date_created.from": date_from.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
        "order.date_created.to":   date_to.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
        "limit":                   50,
    }

    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{ML_API}/orders/search", headers=headers, params=params)

    data         = resp.json()
    total_vendas = data.get("paging", {}).get("total", 0)
    faturamento  = sum(float(o.get("total_amount", 0)) for o in data.get("results", []))

    return {
        "faturamento":  faturamento,
        "total_vendas": total_vendas,
        "periodo":      periodo,
    }
