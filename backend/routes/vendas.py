# routes/vendas.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from datetime import datetime, timedelta, timezone

router = APIRouter(prefix="/api/vendas", tags=["vendas"])
ML_API = "https://api.mercadolibre.com"

# Brasília = UTC-3 (fixo, sem horário de verão)
BRT = timezone(timedelta(hours=-3))
IMPOSTOGLOBAL = 4.0  # percentual padrão — ajustável futuramente por usuário

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

    print(f"[DEBUG] date_closed start_brt={start_brt.isoformat()} end_brt={end_brt.isoformat()}")
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
            "order.date_closed.from":    date_from.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
            "order.date_closed.to":      date_to.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
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
    """Busca pedidos pagos + cancelados + em processamento do período."""
    async with httpx.AsyncClient(timeout=30) as client:
        pagos        = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "paid")
        cancelados   = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "cancelled")
        em_processo  = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "payment_in_process")
        confirmados  = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "confirmed")
        pag_required = await buscar_pedidos_por_status(client, headers, seller_id, date_from, date_to, "payment_required")

    todos = pagos + cancelados + em_processo + confirmados + pag_required

    # Deduplicar por order_id
    vistos = set()
    unicos = []
    for o in todos:
        oid = o.get("id")
        if oid not in vistos:
            vistos.add(oid)
            unicos.append(o)

    print(f"[DEBUG] pagos={len(pagos)} cancelados={len(cancelados)} em_processo={len(em_processo)} confirmados={len(confirmados)} pag_required={len(pag_required)} total_unico={len(unicos)}")
    return sorted(unicos, key=lambda o: o.get("date_created", ""), reverse=True)


async def buscar_frete_vendedor(headers: dict, shipping_id: int) -> float:
    """Busca frete cobrado do vendedor via shipments API.
    frete_vendedor = list_cost - frete_comprador (shipping_option.cost)
    """
    if not shipping_id:
        return 0.0
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{ML_API}/shipments/{shipping_id}", headers=headers)
        if resp.status_code != 200:
            return 0.0
        s = resp.json()
        opt = s.get("shipping_option") or {}
        list_cost       = float(opt.get("list_cost") or 0)
        frete_comprador = float(opt.get("cost") or 0)
        frete_vendedor  = round(max(list_cost - frete_comprador, 0), 2)
        return frete_vendedor
    except Exception as e:
        print(f"[WARN] Erro ao buscar shipment {shipping_id}: {e}")
        return 0.0


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

    # Buscar fretes dos pedidos em paralelo com limite de concorrência
    import asyncio

    semaphore = asyncio.Semaphore(10)  # máx 10 requisições simultâneas

    async def enriquecer_order(order):
        async with semaphore:
            shipping_id = (order.get("shipping") or {}).get("id")
            frete_vendedor = await buscar_frete_vendedor(headers, shipping_id)
            return order, frete_vendedor

    enriched = await asyncio.gather(*[enriquecer_order(o) for o in orders], return_exceptions=False)
    print(f"[DEBUG] orders={len(orders)} enriched={len(enriched)}")

    vendas = []
    for order, frete_vendedor in enriched:
        cancelada = order.get("status") == "cancelled"
        n_items   = len(order.get("order_items", [])) or 1

        for item in order.get("order_items", []):
            qtde  = item.get("quantity", 1)
            valor = round(float(item.get("unit_price", 0)) * qtde, 2)

            # sale_fee: comissão ML por item — multiplicar pela quantidade
            tarifa = round(float(item.get("sale_fee") or 0) * qtde, 2)

            # Frete proporcional por item
            frete_item = round(frete_vendedor / n_items, 2)

            # Imposto (4% padrão — ajustável futuramente)
            imposto = round(valor * (IMPOSTOGLOBAL / 100), 2)

            mc    = round(valor - tarifa - frete_item - imposto, 2)
            mcPct = round((mc / valor * 100), 1) if valor else 0

            vendas.append({
                "id":          order["id"],
                "sku":         item["item"].get("seller_sku", ""),
                "nome":        item["item"].get("title", ""),
                "valor":       valor,
                "qtde":        qtde,
                "tarifa":      tarifa,
                "frete":       frete_item,
                "imposto":     imposto,
                "mc":          mc,
                "mcPct":       mcPct,
                "data":        order.get("date_closed") or order.get("date_created", ""),
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


# Adicionar ao routes/vendas.py — endpoint /api/vendas/dre

@router.get("/dre")
async def get_dre(
    ml_user_id: str = Query(...),
    mes: int        = Query(...),   # 1-12
    ano: int        = Query(...),
):
    """Retorna dados agregados para o DRE mensal."""
    from datetime import datetime, timezone, timedelta

    BRT = timezone(timedelta(hours=-3))
    token = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Calcular range do mês
    start_brt = datetime(ano, mes, 1, 0, 0, 0, tzinfo=BRT)
    if mes == 12:
        end_brt = datetime(ano + 1, 1, 1, 0, 0, 0, tzinfo=BRT) - timedelta(seconds=1)
    else:
        end_brt = datetime(ano, mes + 1, 1, 0, 0, 0, tzinfo=BRT) - timedelta(seconds=1)

    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    if me.status_code != 200:
        raise HTTPException(status_code=401, detail="Erro ao autenticar com o ML")
    seller_id = me.json().get("id")

    start_utc = start_brt.astimezone(timezone.utc)
    end_utc   = end_brt.astimezone(timezone.utc)

    orders = await buscar_todos_pedidos(headers, seller_id, start_utc, end_utc)

    import asyncio
    semaphore = asyncio.Semaphore(10)

    async def enriquecer(order):
        async with semaphore:
            shipping_id = (order.get("shipping") or {}).get("id")
            frete = await buscar_frete_vendedor(headers, shipping_id)
            return order, frete

    enriched = await asyncio.gather(*[enriquecer(o) for o in orders])

    faturamento    = 0.0
    cancelamentos  = 0.0
    tarifa_ml      = 0.0
    frete_vendas   = 0.0
    imposto_total  = 0.0
    pub_ml         = 0.0
    total_vendas   = 0
    total_cancelados = 0

    for order, frete_vendedor in enriched:
        cancelada = order.get("status") == "cancelled"
        n_items   = len(order.get("order_items", [])) or 1

        for item in order.get("order_items", []):
            qtde  = item.get("quantity", 1)
            valor = round(float(item.get("unit_price", 0)) * qtde, 2)
            tarifa = round(float(item.get("sale_fee") or 0) * qtde, 2)
            frete_item = round(frete_vendedor / n_items, 2)
            imposto = round(valor * (IMPOSTOGLOBAL / 100), 2)

            if cancelada:
                cancelamentos += valor
                total_cancelados += 1
            else:
                faturamento   += valor
                tarifa_ml     += tarifa
                frete_vendas  += frete_item
                imposto_total += imposto
                total_vendas  += 1

                # Publicidade ML (pedidos via canal advertising)
                if (order.get("context") or {}).get("channel") == "advertising":
                    pub_ml += valor * 0.0  # investimento ADS não disponível aqui

    # Buscar publicidade separado
    pub_data = {"investimento": 0, "receita_ads": 0}
    try:
        periodo_str = f"{ano}-{mes:02d}"
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{ML_API}/advertising/product_ads/metrics",
                headers=headers,
                params={
                    "user_id": seller_id,
                    "date_from": start_brt.strftime("%Y-%m-%d"),
                    "date_to": end_brt.strftime("%Y-%m-%d"),
                    "granularity": "MONTHLY",
                    "metrics": "SPEND,SALES"
                }
            )
            if r.status_code == 200:
                rdata = r.json()
                results = rdata.get("results", [])
                if results:
                    pub_data["investimento"] = sum(float(x.get("SPEND", 0)) for x in results)
                    pub_data["receita_ads"]  = sum(float(x.get("SALES", 0)) for x in results)
    except Exception as e:
        print(f"[DRE] ADS error: {e}")

    rec_liq = faturamento - cancelamentos

    return {
        "mes":           mes,
        "ano":           ano,
        "faturamento":   round(faturamento, 2),
        "cancelamentos": round(cancelamentos, 2),
        "devolucoes":    0.0,   # não disponível via API ML
        "rec_liq":       round(rec_liq, 2),
        "tarifa_ml":     round(tarifa_ml, 2),
        "frete_vendas":  round(frete_vendas, 2),
        "imposto":       round(imposto_total, 2),
        "pub_ml":        round(pub_data["investimento"], 2),
        "receita_ads":   round(pub_data["receita_ads"], 2),
        "total_vendas":  total_vendas,
        "total_cancelados": total_cancelados,
    }


@router.get("/debug-order")
async def debug_order(
    ml_user_id: str = Query(...),
    order_id: str   = Query(...),
):
    """Retorna pedido + shipment completos para debug financeiro."""
    token = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(timeout=15) as client:
        order_resp = await client.get(f"{ML_API}/orders/{order_id}", headers=headers)
    if order_resp.status_code != 200:
        raise HTTPException(status_code=order_resp.status_code, detail="Pedido não encontrado")
    order = order_resp.json()

    # Buscar dados do shipment (frete cobrado do vendedor)
    shipping_id = (order.get("shipping") or {}).get("id")
    shipment_data = None
    if shipping_id:
        async with httpx.AsyncClient(timeout=15) as client:
            ship_resp = await client.get(f"{ML_API}/shipments/{shipping_id}", headers=headers)
        if ship_resp.status_code == 200:
            s = ship_resp.json()
            shipment_data = {
                "id":                    s.get("id"),
                "status":                s.get("status"),
                "base_cost":             s.get("base_cost"),
                "cost":                  s.get("cost"),
                "order_cost":            s.get("order_cost"),
                "receiver_shipping_cost":s.get("receiver_shipping_cost"),
                "sender_cost":           s.get("sender_cost"),
                "cost_components":       s.get("cost_components"),
                "shipping_option":       s.get("shipping_option"),
                "logistic_type":         s.get("logistic_type"),
                "raw_keys":              list(s.keys()),
            }

    return {
        "order":    order,
        "shipment": shipment_data,
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
