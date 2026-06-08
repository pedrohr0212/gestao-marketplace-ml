import httpx
import asyncio
from fastapi import APIRouter
from auth import get_valid_token

router = APIRouter()
ML_API = "https://api.mercadolibre.com"

async def fetch_json(client: httpx.AsyncClient, url: str, token: str):
    r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 200:
        return r.json()
    return {}

async def get_skus_for_item(client: httpx.AsyncClient, item_id: str, token: str) -> dict:
    data = await fetch_json(client, f"{ML_API}/items/{item_id}?attributes=id,variations,seller_sku,attributes", token)
    result = {}
    item_sku = data.get("seller_sku") or ""
    if not item_sku:
        for attr in data.get("attributes", []):
            if attr.get("id") == "SELLER_SKU":
                item_sku = attr.get("value_name", "")
                break
    variations = data.get("variations", [])
    if not variations:
        result[""] = item_sku
        return result
    for v in variations:
        vid = str(v.get("id", ""))
        sku = v.get("seller_custom_field") or ""
        if not sku:
            for attr in v.get("attribute_combinations", []):
                if attr.get("id") == "SELLER_SKU":
                    sku = attr.get("value_name", "")
                    break
        if not sku:
            for attr in v.get("attributes", []):
                if attr.get("id") == "SELLER_SKU":
                    sku = attr.get("value_name", "")
                    break
        result[vid] = sku or item_sku or ""
    return result

async def fetch_all_ids_by_status(client, ml_user_id, token, status):
    """Busca todos os IDs de anúncios de um determinado status"""
    ids = []
    offset = 0
    limit  = 100
    total  = None
    while True:
        url  = f"{ML_API}/users/{ml_user_id}/items/search?status={status}&limit={limit}&offset={offset}"
        data = await fetch_json(client, url, token)
        if total is None:
            total = data.get("paging", {}).get("total", 0)
            print(f"[ESTOQUE] status={status} total={total}")
        batch = data.get("results", [])
        if not batch:
            break
        ids.extend(batch)
        offset += len(batch)
        if offset >= total or offset >= 1000:
            break
    return ids

@router.get("/api/estoque")
async def get_estoque(ml_user_id: str):
    token = await get_valid_token(ml_user_id)

    async with httpx.AsyncClient() as client:

        # ── 1. Buscar IDs de active + paused (anúncios que o vendedor gerencia) ──
        ids_active, ids_paused = await asyncio.gather(
            fetch_all_ids_by_status(client, ml_user_id, token, "active"),
            fetch_all_ids_by_status(client, ml_user_id, token, "paused"),
        )
        all_item_ids = list(dict.fromkeys(ids_active + ids_paused))  # dedup preservando ordem
        print(f"[ESTOQUE] total IDs: active={len(ids_active)} paused={len(ids_paused)} total={len(all_item_ids)}")

        if not all_item_ids:
            return {"produtos": []}

        # ── 2. Buscar detalhes em lotes de 20 ────────────────────────────
        raw_items = []
        for i in range(0, len(all_item_ids), 20):
            lote    = all_item_ids[i:i+20]
            url     = f"{ML_API}/items?ids={','.join(lote)}&attributes=id,title,price,available_quantity,seller_sku,attributes,variations,status"
            resp    = await fetch_json(client, url, token)
            if isinstance(resp, list):
                raw_items.extend(item.get("body", {}) for item in resp if item.get("code") == 200)
            elif isinstance(resp, dict) and "results" in resp:
                raw_items.extend(resp["results"])

        print(f"[ESTOQUE] detalhes: {len(raw_items)}")

        # ── 3. Buscar SKUs de variações em paralelo ───────────────────────
        tasks_items = []
        tasks_coros = []
        for item in raw_items:
            if item.get("variations"):
                tasks_items.append((item, True))
                tasks_coros.append(get_skus_for_item(client, item.get("id",""), token))
            else:
                tasks_items.append((item, False))

        sku_results = await asyncio.gather(*tasks_coros, return_exceptions=True)

        # ── 4. Montar lista de produtos ───────────────────────────────────
        produtos = []
        var_idx  = 0

        for item, has_var in tasks_items:
            item_id   = item.get("id", "")
            title     = item.get("title", "")
            price     = item.get("price", 0)
            avail_qty = item.get("available_quantity", 0)
            status    = item.get("status", "active")

            item_sku = item.get("seller_sku") or ""
            if not item_sku:
                for attr in item.get("attributes", []):
                    if attr.get("id") == "SELLER_SKU":
                        item_sku = attr.get("value_name", "")
                        break

            if has_var:
                sku_map = sku_results[var_idx] if not isinstance(sku_results[var_idx], Exception) else {}
                var_idx += 1
                for v in item.get("variations", []):
                    vid   = str(v.get("id", ""))
                    v_qty = v.get("available_quantity", 0)
                    v_sku = sku_map.get(vid, "") or item_sku or ""
                    produtos.append({
                        "sku":             v_sku,
                        "ml_item_id":      item_id,
                        "ml_variation_id": vid,
                        "nome":            title,
                        "preco":           price,
                        "estoque":         v_qty,
                        "estoque_transf":  0,
                        "estoque_total":   v_qty,
                        "status":          status,
                    })
            else:
                produtos.append({
                    "sku":             item_sku,
                    "ml_item_id":      item_id,
                    "ml_variation_id": "",
                    "nome":            title,
                    "preco":           price,
                    "estoque":         avail_qty,
                    "estoque_transf":  0,
                    "estoque_total":   avail_qty,
                    "status":          status,
                })

        print(f"[ESTOQUE] produtos retornados: {len(produtos)}")
        return {"produtos": produtos}
