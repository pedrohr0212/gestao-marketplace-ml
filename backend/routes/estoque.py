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

    # Buscar todos os anúncios ativos + pausados (para ver ruptura)
    item_ids = []
    for status in ["active", "paused"]:
        offset = 0
        while True:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{ML_API}/users/{seller_id}/items/search",
                    headers=headers,
                    params={"status": status, "limit": 100, "offset": offset}
                )
            results = resp.json().get("results", [])
            item_ids.extend(results)
            if len(results) < 100:
                break
            offset += 100

    if not item_ids:
        return {"produtos": [], "total": 0}

    # Deduplicar item_ids
    item_ids = list(dict.fromkeys(item_ids))

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
            variations = item.get("variations") or []

            if variations:
                # Produto com variações — criar uma linha por variação com SKU próprio
                for var in variations:
                    var_sku = var.get("seller_custom_field") or ""
                    if not var_sku:
                        for attr in (var.get("attribute_combinations") or []):
                            if attr.get("id") == "SELLER_SKU":
                                var_sku = attr.get("value_name") or ""
                                break
                    # Fallback: SKU do item pai + sufixo da variação
                    if not var_sku:
                        parent_sku = item.get("seller_sku") or ""
                        var_id = str(var.get("id",""))
                        var_sku = f"{parent_sku}-{var_id}" if parent_sku else item["id"]

                    var_avail = var.get("available_quantity", 0)
                    var_nome  = item.get("title","")
                    # Adicionar atributos da variação ao nome
                    combos = var.get("attribute_combinations") or []
                    combo_str = " / ".join([c.get("value_name","") for c in combos if c.get("value_name")])
                    if combo_str:
                        var_nome = f"{var_nome} — {combo_str}"

                    print(f"[EST] {item['id']} var={var.get('id')} sku={repr(var_sku)} avail={var_avail} custom_field={repr(var.get('seller_custom_field'))} combos={var.get('attribute_combinations',[])[0] if var.get('attribute_combinations') else 'none'}")

                    produtos.append({
                        "sku":            var_sku,
                        "ml_item_id":     item["id"],
                        "ml_variation_id": str(var.get("id","")),
                        "nome":           var_nome,
                        "preco":          float(var.get("price") or item.get("price", 0)),
                        "estoque":        var_avail,
                        "estoque_transf": 0,
                        "estoque_total":  var_avail,
                        "status":         item.get("status", ""),
                        "thumbnail":      item.get("thumbnail", ""),
                        "permalink":      item.get("permalink", ""),
                    })
            else:
                # Produto simples — extrair SKU normalmente
                seller_sku = item.get("seller_sku") or ""
                if not seller_sku:
                    for attr in (item.get("attributes") or []):
                        if attr.get("id") == "SELLER_SKU":
                            seller_sku = attr.get("value_name") or ""
                            break

                avail = item.get("available_quantity", 0)
                print(f"[EST] {item['id']} sku={repr(seller_sku)} avail={avail}")

                produtos.append({
                    "sku":            seller_sku or item["id"],
                    "ml_item_id":     item["id"],
                    "ml_variation_id": "",
                    "nome":           item.get("title", ""),
                    "preco":          float(item.get("price", 0)),
                    "estoque":        avail,
                    "estoque_transf": 0,
                    "estoque_total":  avail,
                    "status":         item.get("status", ""),
                    "thumbnail":      item.get("thumbnail", ""),
                    "permalink":      item.get("permalink", ""),
                })

    return {"produtos": produtos, "total": len(produtos)}
