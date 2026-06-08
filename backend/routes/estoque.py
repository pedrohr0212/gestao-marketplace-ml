# routes/estoque.py — Nexora Gestão Marketplace ML
import httpx
import asyncio
from fastapi import APIRouter, Query
from auth import get_valid_token

router = APIRouter(prefix="/api/estoque", tags=["estoque"])
ML_API = "https://api.mercadolibre.com"


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

    # Buscar detalhes em batch (máx 20 por vez) em paralelo
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

    produtos = []
    for batch_result in all_details:
        for entry in batch_result:
            if entry.get("code") != 200:
                continue
            item = entry["body"]

            # Extrair seller_sku — campo raiz ou dentro de attributes
            seller_sku = item.get("seller_sku") or ""
            if not seller_sku:
                for attr in (item.get("attributes") or []):
                    if attr.get("id") == "SELLER_SKU":
                        seller_sku = attr.get("value_name") or ""
                        break

            avail = item.get("available_quantity", 0)
            print(f"[EST] {item['id']} sku={repr(seller_sku)} full={avail}")

            produtos.append({
                "sku":            seller_sku or item["id"],
                "ml_item_id":     item["id"],
                "nome":           item.get("title", ""),
                "preco":          float(item.get("price", 0)),
                "estoque":        avail,        # Full ML (available_quantity)
                "estoque_transf": 0,            # API não expõe — campo manual
                "estoque_total":  avail,
                "status":         item.get("status", ""),
                "thumbnail":      item.get("thumbnail", ""),
                "permalink":      item.get("permalink", ""),
            })

    return {"produtos": produtos, "total": len(produtos)}
