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

async def build_sku_map_from_orders(ml_user_id: str, token: str) -> tuple:
    """Constrói mapa variation_id → sku a partir dos pedidos dos últimos 90d"""
    sku_map = {}      # variation_id → sku
    item_sku_map = {} # ml_item_id → sku

    headers = {"Authorization": f"Bearer {token}"}
    offset  = 0

    async with httpx.AsyncClient() as client:
        while offset < 2000:
            from datetime import datetime, timedelta, timezone
            data_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000-00:00")
            url = (
                f"{ML_API}/orders/search?seller={ml_user_id}"
                f"&order.status=paid&order.date_created.from={data_90d}"
                f"&limit=50&offset={offset}&sort=date_desc"
            )
            r = await client.get(url, headers=headers, timeout=30)
            if r.status_code != 200:
                print(f"[ESTOQUE] orders/search erro: {r.status_code} url={url[:150]}")
                break
            data    = r.json()
            total_pg = data.get("paging", {}).get("total", 0)
            results = data.get("results", [])
            print(f"[ESTOQUE] orders offset={offset} total={total_pg} results={len(results)}")
            if not results:
                break

            for order in results:
                for item in order.get("order_items", []):
                    item_info    = item.get("item", {})
                    ml_item_id   = str(item_info.get("id", ""))
                    variation_id = str(item_info.get("variation_id", "") or "")

                    raw_sku = item_info.get("seller_sku", "")
                    if not raw_sku:
                        for attr in item_info.get("variation_attributes", []):
                            if attr.get("id") == "SELLER_SKU":
                                raw_sku = attr.get("value_name", "")
                                break
                    if not raw_sku:
                        for attr in item_info.get("attributes", []):
                            if attr.get("id") == "SELLER_SKU":
                                raw_sku = attr.get("value_name", "")
                                break

                    if raw_sku:
                        if variation_id:
                            sku_map[variation_id] = raw_sku
                        if ml_item_id:
                            item_sku_map[ml_item_id] = raw_sku

            total   = data.get("paging", {}).get("total", 0)
            offset += len(results)
            if offset >= total:
                break

    print(f"[ESTOQUE] mapa SKU via pedidos: {len(sku_map)} variações, {len(item_sku_map)} itens")
    return sku_map, item_sku_map

async def fetch_all_ids_by_status(client, ml_user_id, token, status):
    ids = []
    offset = 0
    total  = None
    while True:
        url  = f"{ML_API}/users/{ml_user_id}/items/search?status={status}&limit=100&offset={offset}"
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

        # 1. Buscar IDs (active + paused) e mapa SKU em paralelo
        (ids_active, ids_paused), (var_sku_map, item_sku_map) = await asyncio.gather(
            asyncio.gather(
                fetch_all_ids_by_status(client, ml_user_id, token, "active"),
                fetch_all_ids_by_status(client, ml_user_id, token, "paused"),
            ),
            build_sku_map_from_orders(ml_user_id, token),
        )

        all_item_ids = list(dict.fromkeys(ids_active + ids_paused))
        print(f"[ESTOQUE] total IDs: {len(all_item_ids)}")

        if not all_item_ids:
            return {"produtos": []}

        # 2. Buscar detalhes em lotes de 20
        raw_items = []
        for i in range(0, len(all_item_ids), 20):
            lote = all_item_ids[i:i+20]
            url  = f"{ML_API}/items?ids={','.join(lote)}&attributes=id,title,price,available_quantity,seller_sku,attributes,variations,status"
            resp = await fetch_json(client, url, token)
            if isinstance(resp, list):
                raw_items.extend(item.get("body", {}) for item in resp if item.get("code") == 200)
            elif isinstance(resp, dict) and "results" in resp:
                raw_items.extend(resp["results"])

        print(f"[ESTOQUE] detalhes: {len(raw_items)}")

        # 3. Montar lista de produtos
        produtos = []

        for item in raw_items:
            item_id   = item.get("id", "")
            title     = item.get("title", "")
            price     = item.get("price", 0)
            avail_qty = item.get("available_quantity", 0)
            status    = item.get("status", "active")

            # SKU raiz: API > mapa de pedidos
            item_sku = item.get("seller_sku") or ""
            if not item_sku:
                for attr in item.get("attributes", []):
                    if attr.get("id") == "SELLER_SKU":
                        item_sku = attr.get("value_name", "")
                        break
            if not item_sku:
                item_sku = item_sku_map.get(item_id, "")

            variations = item.get("variations", [])

            if variations:
                for v in variations:
                    vid   = str(v.get("id", ""))
                    v_qty = v.get("available_quantity", 0)

                    # SKU da variação: mapa de pedidos > SKU raiz > fallback legível
                    v_sku = var_sku_map.get(vid, "")
                    if not v_sku:
                        v_sku = item_sku
                    if not v_sku:
                        # Fallback: item_id + atributo principal
                        attr_val = ""
                        for attr in v.get("attribute_combinations", []):
                            val = attr.get("value_name", "")
                            if val:
                                attr_val = val.upper().replace(" ", "-")
                                break
                        v_sku = f"{item_id}-{attr_val}" if attr_val else f"{item_id}-{vid}"

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
                    "sku":             item_sku or item_id,
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


@router.get("/api/estoque/debug_item")
async def debug_item(ml_user_id: str, item_id: str):
    token = await get_valid_token(ml_user_id)
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{ML_API}/items/{item_id}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
        data = r.json()
        result = {
            "id": data.get("id"),
            "seller_sku": data.get("seller_sku"),
            "attributes": [a for a in data.get("attributes", []) if "SKU" in a.get("id","").upper()],
            "variations": []
        }
        for v in data.get("variations", []):
            result["variations"].append({
                "id": v.get("id"),
                "seller_custom_field": v.get("seller_custom_field"),
                "attribute_combinations": v.get("attribute_combinations", []),
                "attributes": [a for a in v.get("attributes", []) if "SKU" in a.get("id","").upper()],
            })
        return result
