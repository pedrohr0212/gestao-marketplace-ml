# routes/estoque.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Query, HTTPException
from auth import get_valid_token
from database import get_pool

router = APIRouter(prefix="/api/estoque", tags=["estoque"])
ML_API = "https://api.mercadolivre.com"

@router.get("")
async def get_estoque(ml_user_id: str = Query(...)):
    token   = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    # Buscar ID do vendedor
    async with httpx.AsyncClient() as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    # Buscar anúncios ativos
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{ML_API}/users/{seller_id}/items/search",
            headers=headers,
            params={"status": "active", "limit": 100}
        )

    item_ids = resp.json().get("results", [])
    if not item_ids:
        return {"produtos": [], "total": 0}

    # Buscar detalhes dos itens em batch (máx 20 por vez)
    produtos = []
    for i in range(0, len(item_ids), 20):
        batch = item_ids[i:i+20]
        ids   = ",".join(batch)
        async with httpx.AsyncClient() as client:
            det = await client.get(
                f"{ML_API}/items",
                headers=headers,
                params={"ids": ids}
            )
        for entry in det.json():
            if entry.get("code") != 200:
                continue
            item = entry["body"]
            produtos.append({
                "sku":           item.get("seller_sku") or item["id"],
                "ml_item_id":    item["id"],
                "nome":          item.get("title", ""),
                "preco":         float(item.get("price", 0)),
                "estoque":       item.get("available_quantity", 0),
                "estoque_total": item.get("initial_quantity", 0),
                "status":        item.get("status", ""),
                "thumbnail":     item.get("thumbnail", ""),
                "permalink":     item.get("permalink", ""),
            })

    return {"produtos": produtos, "total": len(produtos)}

@router.get("/full")
async def get_estoque_full(ml_user_id: str = Query(...)):
    token   = await get_valid_token(ml_user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient() as client:
        me = await client.get(f"{ML_API}/users/me", headers=headers)
    seller_id = me.json().get("id")

    async with httpx.AsyncClient() as client:
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
        # Buscar produto_id pelo ml_item_id
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
