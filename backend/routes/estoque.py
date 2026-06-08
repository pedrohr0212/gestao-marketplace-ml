import httpx
import asyncio
from fastapi import APIRouter, HTTPException
from auth import get_valid_token

router = APIRouter()

ML_API = "https://api.mercadolibre.com"

async def fetch_json(client: httpx.AsyncClient, url: str, token: str):
    r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 200:
        return r.json()
    return {}

async def get_skus_for_item(client: httpx.AsyncClient, item_id: str, token: str) -> dict:
    """Busca SKU de cada variação via /items/{id}"""
    data = await fetch_json(client, f"{ML_API}/items/{item_id}?attributes=id,variations,seller_sku,attributes", token)
    result = {}

    # SKU raiz
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

@router.get("/api/estoque")
async def get_estoque(ml_user_id: str):
    token = await get_valid_token(ml_user_id)

    async with httpx.AsyncClient() as client:

        # ── 1. Buscar TODOS os IDs de itens ativos (com paginação correta) ──
        all_item_ids = []
        offset = 0
        limit  = 100
        total  = None

        while True:
            url  = f"{ML_API}/users/{ml_user_id}/items/search?status=active&limit={limit}&offset={offset}"
            data = await fetch_json(client, url, token)

            if total is None:
                total = data.get("paging", {}).get("total", 0)
                print(f"[ESTOQUE] total itens ativos no ML: {total}")

            ids = data.get("results", [])
            if not ids:
                break

            all_item_ids.extend(ids)
            offset += len(ids)

            if offset >= total:
                break
            # Segurança: ML limita offset a 1000
            if offset >= 1000:
                print(f"[ESTOQUE] atenção: limite ML de 1000 atingido, total real={total}")
                break

        print(f"[ESTOQUE] IDs coletados: {len(all_item_ids)} de {total}")

        if not all_item_ids:
            return {"produtos": []}

        # ── 2. Buscar detalhes em lotes de 20 ────────────────────────────
        raw_items = []
        lote_size = 20
        for i in range(0, len(all_item_ids), lote_size):
            lote    = all_item_ids[i:i+lote_size]
            ids_str = ",".join(lote)
            url     = f"{ML_API}/items?ids={ids_str}&attributes=id,title,price,available_quantity,seller_sku,attributes,variations,status"
            resp    = await fetch_json(client, url, token)

            if isinstance(resp, list):
                for item in resp:
                    if item.get("code") == 200:
                        raw_items.append(item.get("body", {}))
            elif isinstance(resp, dict) and "results" in resp:
                raw_items.extend(resp["results"])

        print(f"[ESTOQUE] detalhes recebidos: {len(raw_items)}")

        # ── 3. Buscar SKUs de variações em paralelo ───────────────────────
        tasks_coroutines = []
        tasks_items      = []
        for item in raw_items:
            if item.get("variations"):
                tasks_items.append((item, True))
                tasks_coroutines.append(get_skus_for_item(client, item.get("id",""), token))
            else:
                tasks_items.append((item, False))

        sku_results = await asyncio.gather(*tasks_coroutines, return_exceptions=True)

        # ── 4. Montar lista de produtos ───────────────────────────────────
        produtos = []
        var_idx  = 0

        for item, has_var in tasks_items:
            item_id   = item.get("id", "")
            title     = item.get("title", "")
            price     = item.get("price", 0)
            avail_qty = item.get("available_quantity", 0)
            status    = item.get("status", "active")

            # SKU raiz do item
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
