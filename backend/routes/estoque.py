# routes/estoque.py — Nexora Gestão Marketplace ML
import httpx
import asyncio
from fastapi import APIRouter, Query
from auth import get_valid_token

router = APIRouter(prefix="/api/estoque", tags=["estoque"])
ML_API = "https://api.mercadolibre.com"


async def buscar_variacao_sku(client, headers, item_id, var_id):
    """Busca SKU de uma variação específica."""
    try:
        resp = await client.get(
            f"{ML_API}/items/{item_id}/variations/{var_id}",
            headers=headers
        )
        if resp.status_code == 200:
            data = resp.json()
            # Tentar seller_custom_field primeiro
            sku = data.get("seller_custom_field") or ""
            if not sku:
                for attr in (data.get("attribute_combinations") or []):
                    if attr.get("id") == "SELLER_SKU":
                        sku = attr.get("value_name") or ""
                        break
            return sku
    except Exception as e:
        print(f"[EST] var sku error {item_id}/{var_id}: {e}")
    return ""


@router.get("")
async def get_estoque(ml_user_id: str = Query(...)):
    token   = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Buscar ID do vendedor
    async with httpx.AsyncClient(timeout=15) as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    # Buscar todos os anúncios ativos + pausados
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

    item_ids = list(dict.fromkeys(item_ids))

    # Buscar mapa variation_id -> seller_sku via pedidos recentes
    var_sku_map = {}
    try:
        from datetime import datetime, timedelta, timezone
        BRT = timezone(timedelta(hours=-3))
        dt_from = (datetime.now(BRT) - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00.000-03:00")
        dt_to   = datetime.now(BRT).strftime("%Y-%m-%dT23:59:59.999-03:00")
        offset_p = 0
        while True:
            async with httpx.AsyncClient(timeout=20) as client:
                rp = await client.get(
                    f"{ML_API}/orders/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"seller": seller_id, "sort": "date_desc",
                            "offset": offset_p, "limit": 50,
                            "order.status": "paid",
                            "order.date_created.from": dt_from,
                            "order.date_created.to":   dt_to}
                )
            orders = rp.json().get("results", []) if rp.status_code == 200 else []
            for order in orders:
                for oi in order.get("order_items", []):
                    it     = oi.get("item", {})
                    var_id = str(it.get("variation_id", "") or "")
                    sku    = it.get("seller_sku", "") or ""
                    if not sku:
                        for attr in (it.get("variation_attributes") or []):
                            if attr.get("id") == "SELLER_SKU":
                                sku = attr.get("value_name", "") or ""
                                break
                    if var_id and sku:
                        var_sku_map[var_id] = sku
            if len(orders) < 50:
                break
            offset_p += 50
            if offset_p > 2000:
                break
        print(f"[EST] var_sku_map: {len(var_sku_map)} variacoes mapeadas")
    except Exception as e:
        print(f"[EST] var_sku_map error: {e}")

        # Buscar detalhes em batch
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
                # Produto com variações — buscar SKU de cada variação individualmente
                for var in variations:
                    var_id    = var.get("id", "")
                    var_avail = var.get("available_quantity", 0)

                    # Tentar SKU direto da variação primeiro
                    var_sku = var.get("seller_custom_field") or ""
                    if not var_sku:
                        for attr in (var.get("attribute_combinations") or []):
                            if attr.get("id") == "SELLER_SKU":
                                var_sku = attr.get("value_name") or ""
                                break

                    # Se ainda vazio, buscar individualmente
                    if not var_sku and var_id:
                        async with httpx.AsyncClient(timeout=10) as client:
                            var_sku = await buscar_variacao_sku(client, headers, item["id"], var_id)

                    # Montar nome com atributos da variação
                    combos = var.get("attribute_combinations") or []
                    combo_str = " / ".join([
                        c.get("value_name", "")
                        for c in combos
                        if c.get("value_name") and c.get("id") != "SELLER_SKU"
                    ])
                    var_nome = item.get("title", "")
                    if combo_str:
                        var_nome = f"{var_nome} — {combo_str}"

                    print(f"[EST] {item['id']} var={var_id} sku={repr(var_sku)} avail={var_avail}")

                    produtos.append({
                        "sku":             var_sku or f"{item['id']}-{var_id}",
                        "ml_item_id":      item["id"],
                        "ml_variation_id": str(var_id),
                        "nome":            var_nome,
                        "preco":           float(var.get("price") or item.get("price", 0)),
                        "estoque":         var_avail,
                        "estoque_transf":  0,
                        "estoque_total":   var_avail,
                        "status":          item.get("status", ""),
                        "thumbnail":       item.get("thumbnail", ""),
                        "permalink":       item.get("permalink", ""),
                    })
            else:
                # Produto simples
                seller_sku = item.get("seller_sku") or ""
                if not seller_sku:
                    for attr in (item.get("attributes") or []):
                        if attr.get("id") == "SELLER_SKU":
                            seller_sku = attr.get("value_name") or ""
                            break

                avail = item.get("available_quantity", 0)
                print(f"[EST] {item['id']} sku={repr(seller_sku)} avail={avail}")

                produtos.append({
                    "sku":             seller_sku or item["id"],
                    "ml_item_id":      item["id"],
                    "ml_variation_id": "",
                    "nome":            item.get("title", ""),
                    "preco":           float(item.get("price", 0)),
                    "estoque":         avail,
                    "estoque_transf":  0,
                    "estoque_total":   avail,
                    "status":          item.get("status", ""),
                    "thumbnail":       item.get("thumbnail", ""),
                    "permalink":       item.get("permalink", ""),
                })

    return {"produtos": produtos, "total": len(produtos)}
