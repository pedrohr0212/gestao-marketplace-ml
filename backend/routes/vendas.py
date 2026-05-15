# routes/vendas.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/vendas", tags=["vendas"])
ML_API = "https://api.mercadolibre.com"

# Brasília = UTC-3
BRT = timezone(timedelta(hours=-3))

def get_date_range(periodo: str):
    now = datetime.now(BRT)

    if periodo == "hoje":
        start = now.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "ontem":
        d     = now - timedelta(days=1)
        start = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = d.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "7d":
        start = (now - timedelta(days=6)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "30d":
        start = (now - timedelta(days=29)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "mes":
        start = now.replace(day=1, hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "mesant":
        last = now.replace(day=1) - timedelta(days=1)
        start = last.replace(day=1, hour=0,  minute=0,  second=0,  microsecond=0)
        end   = last.replace(hour=23, minute=59, second=59, microsecond=0)

    else:
        start = (now - timedelta(days=29)).replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end   = now.replace(hour=23, minute=59, second=59, microsecond=0)

    # Retorna em UTC para a API do ML
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


async def buscar_todos_pedidos(headers: dict, seller_id: int, date_from: datetime, date_to: datetime) -> list:
    """Busca TODOS os pedidos do período paginando automaticamente (50 por vez)."""
    todos = []
    offset = 0
    limit  = 50

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params = {
                "seller":                    seller_id,
                "order.status":              "paid",
                "order.date_created.from":   date_from.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
                "order.date_created.to":     date_to.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
                "sort":                      "date_desc",
                "offset":                    offset,
                "limit":                     limit,
            }
            resp = await client.get(f"{ML_API}/orders/search", headers=headers, params=params)
            if resp.status_code != 200:
                break

            data    = resp.json()
            results = data.get("results", [])
            total   = data.get("paging", {}).get("total", 0)
            todos.extend(results)

            # Se já buscamos todos, para
            if offset + limit >= total or not results:
                break
            offset += limit

    return todos


@router.get("")
async def get_vendas(
    ml_user_id: str = Query(...),
    periodo: str    = Query("30d"),
):
    token = await get_valid_token(ml_user_id)
    date_from, date_to = get_date_range(periodo)
    headers = {"Authorization": f"Bearer {token}"}

    # Buscar ID do vendedor
    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    if me.status_code != 200:
        raise HTTPException(status_code=401, detail="Erro ao autenticar com o ML")
    seller_id = me.json().get("id")

    # Buscar TODOS os pedidos do período (paginação automática)
    orders = await buscar_todos_pedidos(headers, seller_id, date_from, date_to)

    # Formatar — ordenado do mais recente para o mais antigo
    vendas = []
    for order in sorted(orders, key=lambda o: o.get("date_created", ""), reverse=True):
        for item in order.get("order_items", []):
            valor = float(item.get("unit_price", 0))
            vendas.append({
                "id":          order["id"],
                "sku":         item["item"].get("seller_sku", ""),
                "nome":        item["item"].get("title", ""),
                "valor":       valor,
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
        "total":   len(vendas),
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

    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    orders      = await buscar_todos_pedidos(headers, seller_id, date_from, date_to)
    faturamento = sum(float(o.get("total_amount", 0)) for o in orders)

    return {
        "faturamento":  faturamento,
        "total_vendas": len(orders),
        "periodo":      periodo,
    }
