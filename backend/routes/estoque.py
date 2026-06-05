# routes/estoque.py — Nexora Gestão Marketplace ML
import httpx
import asyncio
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from database import get_pool

router = APIRouter(prefix="/api/estoque", tags=["estoque"])
ML_API = "https://api.mercadolibre.com"


async def buscar_inventory_item(client, headers, item_id):
    """Busca estoques por tipo: available, not_available (em transferência), etc."""
    try:
        resp = await client.get(
            f"{ML_API}/inventories/items/{item_id}/stock/fulfillment",
            headers=headers
        )
        if resp.status_code == 200:
            data = resp.json()
            # available = estoque disponível no Full
            # not_available = em transferência ou reservado
            total          = data.get("total", 0)
            available      = data.get("available_quantity", 0)
            not_available  = data.get("not_available_quantity", 0)
            # not_available_detail pode ter: transfer (em transferência), reserved, damaged
            details        = data.get("not_available_detail", [])
            transfer = sum(d.get("quantity", 0) for d in details if d.get("status") == "transfer")
            return {
                "full":      available,
                "transf":    transfer,
                "not_avail": not_available,
            }
    except Exception as e:
        print(f"[EST] inventory error item={item_id}: {e}")
    return {"full": 0, "transf": 0, "not_avail": 0}


@router.get("")
async def get_estoque(ml_user_id: str = Query(...)):
    token   = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Buscar ID do vendedor
    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    # Buscar todos os anúncios ativos (paginando)
    item_ids = []
    offset = 0
    while True:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{ML_API}/users/{seller_id}/items/search",
                headers=headers,
                params={"status": "active", "limit": 100, "offset": offset}
            )
        results = resp.json().get("results", [])
        item_ids.extend(results)
        if len(results) < 100:
            break
        offset += 100

    if not item_ids:
        return {"produtos": [], "total": 0}

    # Buscar detalhes dos itens em batch (máx 20 por vez)
    produtos = []
    semaphore = asyncio.Semaphore(5)

    async def buscar_batch(batch):
        ids = ",".join(batch)
        async with semaphore:
            async with httpx.AsyncClient(timeout=15) as client:
                det = await client.get(
                    f"{ML_API}/items",
                    headers=headers,
                    params={"ids": ids}
                )
        return det.json()

    batches = [item_ids[i:i+20] for i in range(0, len(item_ids), 20)]
    all_details = await asyncio.gather(*[buscar_batch(b) for b in batches])

    # Montar lista de produtos e buscar inventory em paralelo
    items_flat = []
    for batch_result in all_details:
        for entry in batch_result:
            if entry.get("code") != 200:
                continue
            item = entry["body"]
            seller_sku = item.get("seller_sku") or ""
            if not seller_sku:
                for attr in (item.get("attributes") or []):
                    if attr.get("id") == "SELLER_SKU":
                        seller_sku = attr.get("value_name") or ""
                        break
            items_flat.append((seller_sku, item))

    # Buscar inventory (Full + Transferência) para todos em paralelo
    async def buscar_inv(seller_sku_item):
        seller_sku, item = seller_sku_item
        async with httpx.AsyncClient(timeout=10) as client:
            inv = await buscar_inventory_item(client, headers, item["id"])
        return seller_sku, item, inv

    inv_results = await asyncio.gather(*[buscar_inv(x) for x in items_flat])

    for seller_sku, item, inv in inv_results:
        # available_quantity = estoque físico + fulfillment disponível
        # Se o item tem fulfillment, usar inv["full"] para estoque Full
        # e available_quantity - inv["full"] para estoque físico (aproximação)
        estoque_full   = inv["full"]
        estoque_transf = inv["transf"]

        print(f"[EST] {item['id']} sku={repr(seller_sku)} full={estoque_full} transf={estoque_transf}")

        produtos.append({
            "sku":           seller_sku or item["id"],
            "ml_item_id":    item["id"],
            "nome":          item.get("title", ""),
            "preco":         float(item.get("price", 0)),
            "estoque":       estoque_full,
            "estoque_transf": estoque_transf,
            "estoque_total": item.get("available_quantity", 0),
            "status":        item.get("status", ""),
            "thumbnail":     item.get("thumbnail", ""),
            "permalink":     item.get("permalink", ""),
        })

    return {"produtos": produtos, "total": len(produtos)}


@router.get("/full")
async def get_estoque_full(ml_user_id: str = Query(...)):
    token   = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{ML_API}/users/{seller_id}/fulfillment/stock",
            headers=headers
        )

    if resp.status_code != 200:
        return {"full": [], "mensagem": "Full não disponível para esta conta"}

    return {"full": resp.json()}


@router.post("/historico")
async def registrar_movimentacao(payload: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        produto = await conn.fetchrow(
            "SELECT id FROM produtos WHERE ml_item_id = $1",
            payload.get("ml_item_id")
        )
        produto_id = produto["id"] if produto else None
        await conn.execute("""
            INSERT INTO estoque_historico (usuario_id, produto_id, tipo, quantidade, observacao)
            VALUES ($1, $2, $3, $4, $5)
        """,
            payload.get("usuario_id"),
            produto_id,
            payload.get("tipo"),
            payload.get("quantidade"),
            payload.get("observacao", ""),
        )
    return {"ok": True}
