# routes/webhooks.py — Nexora Gestão Marketplace ML
import httpx
from fastapi import APIRouter, Request, BackgroundTasks
from auth import get_valid_token
from database import get_pool
from datetime import datetime, timezone
from dateutil import parser as dateparser

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
ML_API = "https://api.mercadolibre.com"

async def processar_venda(user_id: str, resource: str):
    try:
        token   = await get_valid_token(user_id)
        headers = {"Authorization": f"Bearer {token}"}
        order_id = resource.split("/")[-1]

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{ML_API}/orders/{order_id}", headers=headers)

        if resp.status_code != 200:
            return

        order = resp.json()
        pool  = await get_pool()

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT id FROM usuarios WHERE ml_user_id = $1", user_id
            )
            if not row:
                return
            usuario_id = row["id"]

            # Converter string de data para datetime com timezone
            date_str = order.get("date_created", "")
            try:
                date_created = dateparser.parse(date_str).astimezone(timezone.utc) if date_str else None
            except Exception:
                date_created = None

            await conn.execute("""
                INSERT INTO vendas (ml_order_id, usuario_id, status, valor_total, data_venda, dados_raw)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (ml_order_id) DO UPDATE SET
                    status    = EXCLUDED.status,
                    dados_raw = EXCLUDED.dados_raw
            """,
                int(order_id),
                usuario_id,
                order.get("status"),
                float(order.get("total_amount", 0)),
                date_created,
                str(order),
            )

        print(f"✅ Venda {order_id} processada para usuário {user_id}")

    except Exception as e:
        print(f"❌ Erro ao processar venda: {e}")

@router.post("/ml")
async def webhook_ml(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    topic    = payload.get("topic", "")
    resource = payload.get("resource", "")
    user_id  = str(payload.get("user_id", ""))

    if topic == "orders_v2" and user_id:
        background_tasks.add_task(processar_venda, user_id, resource)

    return {"status": "ok"}
