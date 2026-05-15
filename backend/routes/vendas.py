# routes/vendas.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/vendas", tags=["vendas"])
ML_API = "https://api.mercadolibre.com"

# Brasília = UTC-3 (fixo, sem horário de verão)
BRT = timezone(timedelta(hours=-3))

def get_date_range(periodo: str):
    # Pega hora atual em UTC e converte explicitamente para BRT
    now_utc = datetime.now(timezone.utc)
    now_brt = now_utc.astimezone(BRT)

    print(f"[DEBUG] now_utc={now_utc.isoformat()} | now_brt={now_brt.isoformat()} | periodo={periodo}")

    if periodo == "hoje":
        start_brt = now_brt.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "ontem":
        d         = now_brt - timedelta(days=1)
        start_brt = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = d.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "7d":
        d         = now_brt - timedelta(days=6)
        start_brt = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "30d":
        d         = now_brt - timedelta(days=29)
        start_brt = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "mes":
        start_brt = now_brt.replace(day=1, hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    elif periodo == "mesant":
        last      = now_brt.replace(day=1) - timedelta(days=1)
        start_brt = last.replace(day=1, hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = last.replace(hour=23, minute=59, second=59, microsecond=0)

    else:
        d         = now_brt - timedelta(days=29)
        start_brt = d.replace(hour=0,  minute=0,  second=0,  microsecond=0)
        end_brt   = now_brt.replace(hour=23, minute=59, second=59, microsecond=0)

    start_utc = start_brt.astimezone(timezone.utc)
    end_utc   = end_brt.astimezone(timezone.utc)

    print(f"[DEBUG] start_brt={start_brt.isoformat()} end_brt={end_brt.isoformat()}")
    print(f"[DEBUG] start_utc={start_utc.isoformat()} end_utc={end_utc.isoformat()}")

    return start_utc, end_utc


async def buscar_pedidos_por_status(
    client: httpx.AsyncClient,
    headers: dict,
    seller_id: int,
    date_from: datetime,
    date_to: datetime,
    status: str
) -> list:
    """Busca todos os pedidos de um status específico com paginação automática."""
    todos  = []
    offset = 0
    limit  = 50

    while True:
        params = {
            "seller":                    seller_id,
            "order.status":              status,
            "order.date_created.from":   date_from.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
            "order.date_created.to":     date_to.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
            "sort":                      "date_desc",
            "offset":                    offset,
            "limit":                     limit,
        }
        resp = await client.get(f"{ML_API}/orders/search", headers=headers, params=params)
        if resp.status_code != 200:
            print(f"[DEBUG] orders/search [{status}] erro: {resp.status_code}")
            break

        data    = resp.json()
        results = data.get("results", [])
        total   = data.get("paging", {}).get("total", 0)
        todos.extend(results)

        print(f"[DEBUG] status={status} offset={offset} total={total} fetched={len(results)} accumulated={len(todos)}")

        if offset + limit >= total or not results:
            break
        offset += limit

    return todos


async def buscar_todos_pedidos(headers: dict, seller_id: int, date_from: datetime, date_to: datetime) -> list:
    """Busca pedidos pagos + cancelados do período com paginação automática."""
    async with httpx.AsyncClient(timeout=30) as client:
        pagos      = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "paid")
        cancelados = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "cancelled")

    todos = pagos + cancelados
    # Ordenar do mais recente para o mais antigo
    return sorted(todos, key=lambda o: o.get("date_created", ""), reverse=True)


@router.get("")
async def get_vendas(
    ml_user_id: str = Query(...),
    periodo: str    = Query("30d"),
):
    token = await get_valid_token(ml_user_id)
    date_from, date_to = get_date_range(periodo)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    if me.status_code != 200:
        raise HTTPException(status_code=401, detail="Erro ao autenticar com o ML")
    seller_id = me.json().get("id")

    orders = await buscar_todos_pedidos(headers, seller_id, date_from, date_to)

    vendas = []
    for order in orders:
        cancelada = order.get("status") == "cancelled"
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
                "cancelada":   cancelada,
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
    pagos       = [o for o in orders if o.get("status") == "paid"]
    faturamento = sum(float(o.get("total_amount", 0)) for o in pagos)

    return {
        "faturamento":  faturamento,
        "total_vendas": len(pagos),
        "cancelados":   len(orders) - len(pagos),
        "periodo":      periodo,
    }
