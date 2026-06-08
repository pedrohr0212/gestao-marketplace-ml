import httpx
import asyncio
from fastapi import APIRouter, HTTPException
from config import get_settings

router = APIRouter()
settings = get_settings()

ML_API = "https://api.mercadolibre.com"

async def get_token(ml_user_id: str) -> str:
    import asyncpg
    conn = await asyncpg.connect(settings.database_url)
    try:
        row = await conn.fetchrow(
            "SELECT access_token FROM ml_tokens WHERE ml_user_id=$1 ORDER BY updated_at DESC LIMIT 1",
            str(ml_user_id)
        )
        if not row:
            raise HTTPException(status_code=401, detail="Usuário não encontrado")
        return row["access_token"]
    finally:
        await conn.close()

async def fetch_json(client: httpx.AsyncClient, url: str, token: str) -> dict:
    r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if r.status_code == 200:
        return r.json()
    return {}

async def get_seller_skus_from_variations(client: httpx.AsyncClient, item_id: str, token: str) -> dict:
    """Busca SKU de cada variação diretamente via /items/{id} — campo seller_custom_field ou attributes"""
    data = await fetch_json(client, f"{ML_API}/items/{item_id}?attributes=id,variations,seller_sku", token)
    result = {}
    
    # SKU no nível do item (sem variação)
    item_sku = data.get("seller_sku") or ""
    if not item_sku:
        for attr in data.get("attributes", []):
            if attr.get("id") == "SELLER_SKU":
                item_sku = attr.get("value_name", "")
                break

    variations = data.get("variations", [])
    if not variations:
        # Produto sem variação — retorna SKU do item
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
    token = await get_token(ml_user_id)

    async with httpx.AsyncClient() as client:
        # 1. Buscar todos os itens ativos do vendedor
        all_items = []
        offset = 0
        while True:
            url = f"{ML_API}/users/{ml_user_id}/items/search?status=active&limit=100&offset={offset}"
            data = await fetch_json(client, url, token)
            ids = data.get("results", [])
            if not ids:
                break
            all_items.extend(ids)
            if len(ids) < 100:
                break
            offset += 100

        if not all_items:
            return {"produtos": []}

        # 2. Buscar detalhes em lotes de 20
        produtos = []
        lote_size = 20
        for i in range(0, len(all_items), lote_size):
            lote = all_items[i:i+lote_size]
            ids_str = ",".join(lote)
            url = f"{ML_API}/items?ids={ids_str}&attributes=id,title,price,available_quantity,seller_sku,attributes,variations,status"
            data = await fetch_json(client, url, token)
            
            items_lote = []
            if isinstance(data, list):
                items_lote = [item.get("body", {}) for item in data if item.get("code") == 200]
            elif isinstance(data, dict) and "results" in data:
                items_lote = data["results"]

            # 3. Para cada item, resolver SKUs de variações
            tasks = []
            for item in items_lote:
                item_id = item.get("id", "")
                has_variations = bool(item.get("variations"))
                if has_variations:
                    tasks.append((item, get_seller_skus_from_variations(client, item_id, token)))
                else:
                    tasks.append((item, None))

            # Executar buscas de variação em paralelo
            variation_results = await asyncio.gather(
                *[t[1] for t in tasks if t[1] is not None],
                return_exceptions=True
            )

            var_idx = 0
            for item, task in tasks:
                item_id = item.get("id", "")
                title = item.get("title", "")
                price = item.get("price", 0)
                avail_qty = item.get("available_quantity", 0)
                item_status = item.get("status", "active")

                # SKU do item (sem variação)
                item_sku = item.get("seller_sku") or ""
                if not item_sku:
                    for attr in item.get("attributes", []):
                        if attr.get("id") == "SELLER_SKU":
                            item_sku = attr.get("value_name", "")
                            break

                variations = item.get("variations", [])

                if task is not None:
                    sku_map = variation_results[var_idx] if not isinstance(variation_results[var_idx], Exception) else {}
                    var_idx += 1

                    if variations:
                        for v in variations:
                            vid = str(v.get("id", ""))
                            v_qty = v.get("available_quantity", 0)
                            v_sku = sku_map.get(vid, "") or item_sku or ""
                            produtos.append({
                                "sku": v_sku,
                                "ml_item_id": item_id,
                                "ml_variation_id": vid,
                                "nome": title,
                                "preco": price,
                                "estoque": v_qty,
                                "estoque_transf": 0,
                                "estoque_total": v_qty,
                                "status": item_status,
                            })
                    else:
                        sku = sku_map.get("", "") or item_sku or ""
                        produtos.append({
                            "sku": sku,
                            "ml_item_id": item_id,
                            "ml_variation_id": "",
                            "nome": title,
                            "preco": price,
                            "estoque": avail_qty,
                            "estoque_transf": 0,
                            "estoque_total": avail_qty,
                            "status": item_status,
                        })
                else:
                    produtos.append({
                        "sku": item_sku,
                        "ml_item_id": item_id,
                        "ml_variation_id": "",
                        "nome": title,
                        "preco": price,
                        "estoque": avail_qty,
                        "estoque_transf": 0,
                        "estoque_total": avail_qty,
                        "status": item_status,
                    })

        return {"produtos": produtos}
