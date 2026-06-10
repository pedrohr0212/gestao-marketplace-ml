import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import date
import asyncpg
from auth import get_valid_token
from config import get_settings

router = APIRouter()
settings = get_settings()
ML_API  = "https://api.mercadolibre.com"

# ── Modelos ────────────────────────────────────────────────────────────────────
class CustoIn(BaseModel):
    ml_user_id: str
    sku:        str
    nome:       str
    custo:      float
    data_inicio: date
    data_fim:    Optional[date] = None  # None = vigente até hoje

# ── Helpers ───────────────────────────────────────────────────────────────────
async def get_conn():
    return await asyncpg.connect(settings.database_url)

async def ensure_table(conn):
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS produto_custos (
            id          SERIAL PRIMARY KEY,
            ml_user_id  TEXT NOT NULL,
            sku         TEXT NOT NULL,
            nome        TEXT,
            custo       NUMERIC(12,2) NOT NULL,
            data_inicio DATE NOT NULL,
            data_fim    DATE,
            criado_em   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pc_user_sku
        ON produto_custos(ml_user_id, sku)
    """)

# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/custos")
async def salvar_custo(body: CustoIn):
    """Salva ou atualiza vigência de custo de um SKU."""
    conn = await get_conn()
    try:
        await ensure_table(conn)
        # Fechar vigência aberta anterior do mesmo SKU se houver
        await conn.execute("""
            UPDATE produto_custos
               SET data_fim = $1
             WHERE ml_user_id = $2
               AND sku        = $3
               AND data_fim IS NULL
               AND data_inicio < $1
        """, body.data_inicio, body.ml_user_id, body.sku)

        # Inserir novo registro
        await conn.execute("""
            INSERT INTO produto_custos
                   (ml_user_id, sku, nome, custo, data_inicio, data_fim)
            VALUES ($1, $2, $3, $4, $5, $6)
        """, body.ml_user_id, body.sku, body.nome,
             body.custo, body.data_inicio, body.data_fim)

        return {"ok": True}
    finally:
        await conn.close()


@router.get("/api/custos")
async def listar_custos(ml_user_id: str):
    """Lista todo o histórico de custos do vendedor agrupado por SKU."""
    conn = await get_conn()
    try:
        await ensure_table(conn)
        rows = await conn.fetch("""
            SELECT sku, nome, custo, data_inicio, data_fim
              FROM produto_custos
             WHERE ml_user_id = $1
          ORDER BY sku, data_inicio DESC
        """, ml_user_id)
        result = {}
        for r in rows:
            if r["sku"] not in result:
                result[r["sku"]] = {"sku": r["sku"], "nome": r["nome"], "historico": []}
            result[r["sku"]]["historico"].append({
                "custo":       float(r["custo"]),
                "data_inicio": r["data_inicio"].isoformat(),
                "data_fim":    r["data_fim"].isoformat() if r["data_fim"] else None,
            })
        return {"custos": list(result.values())}
    finally:
        await conn.close()


@router.delete("/api/custos/{ml_user_id}/{sku}")
async def deletar_custo(ml_user_id: str, sku: str, data_inicio: str):
    """Remove um registro específico de custo."""
    conn = await get_conn()
    try:
        await conn.execute("""
            DELETE FROM produto_custos
             WHERE ml_user_id  = $1
               AND sku         = $2
               AND data_inicio = $3
        """, ml_user_id, sku, date.fromisoformat(data_inicio))
        return {"ok": True}
    finally:
        await conn.close()


@router.get("/api/dre/cmv")
async def calcular_cmv(ml_user_id: str, mes: int, ano: int):
    """
    Calcula o CMV do mês:
    - Busca todos os pedidos pagos do mês (00:00 dia 1 até 23:59 último dia)
    - Cruza cada item vendido com o custo vigente na data do pedido
    - Retorna CMV total + detalhamento por SKU + lista de SKUs sem custo
    """
    from datetime import datetime, timezone, timedelta
    import calendar

    token = await get_valid_token(ml_user_id)
    conn  = await get_conn()

    try:
        await ensure_table(conn)

        # Período do mês em UTC (BRT = UTC-3)
        brt = timezone(timedelta(hours=-3))
        d_ini = datetime(ano, mes, 1, 0, 0, 0, tzinfo=brt)
        ultimo_dia = calendar.monthrange(ano, mes)[1]
        d_fim = datetime(ano, mes, ultimo_dia, 23, 59, 59, tzinfo=brt)
        d_ini_utc = d_ini.astimezone(timezone.utc)
        d_fim_utc = d_fim.astimezone(timezone.utc)

        # Buscar pedidos do mês
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(timeout=30) as client:
            me = await client.get(f"{ML_API}/users/me", headers=headers)
            seller_id = me.json().get("id") if me.status_code == 200 else int(ml_user_id)

            pedidos = []
            offset  = 0
            while True:
                params = {
                    "seller":                 seller_id,
                    "order.status":           "paid",
                    "order.date_closed.from": d_ini_utc.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
                    "order.date_closed.to":   d_fim_utc.strftime("%Y-%m-%dT%H:%M:%S.000-00:00"),
                    "sort":  "date_asc",
                    "limit": 50,
                    "offset": offset,
                }
                r = await client.get(f"{ML_API}/orders/search", headers=headers, params=params)
                if r.status_code != 200:
                    break
                data   = r.json()
                total  = data.get("paging", {}).get("total", 0)
                lote   = data.get("results", [])
                pedidos.extend(lote)
                offset += len(lote)
                if offset >= total or not lote:
                    break

        # Buscar todos os custos do vendedor
        rows_custo = await conn.fetch("""
            SELECT sku, custo, data_inicio, data_fim
              FROM produto_custos
             WHERE ml_user_id = $1
          ORDER BY sku, data_inicio
        """, ml_user_id)

        # Indexar custos por SKU → lista de vigências
        custos_por_sku = {}
        for r in rows_custo:
            sku = r["sku"]
            if sku not in custos_por_sku:
                custos_por_sku[sku] = []
            custos_por_sku[sku].append({
                "custo":       float(r["custo"]),
                "data_inicio": r["data_inicio"],
                "data_fim":    r["data_fim"],
            })

        def custo_na_data(sku: str, dt: date) -> Optional[float]:
            vigencias = custos_por_sku.get(sku, [])
            for v in reversed(vigencias):
                ini = v["data_inicio"]
                fim = v["data_fim"] if v["data_fim"] else date.today()
                if ini <= dt <= fim:
                    return v["custo"]
            # fallback: custo mais recente disponível
            if vigencias:
                return vigencias[-1]["custo"]
            return None

        # Calcular CMV
        cmv_total   = 0.0
        detalhes    = {}
        sem_custo   = set()

        for pedido in pedidos:
            data_pedido = pedido.get("date_closed") or pedido.get("date_created", "")
            try:
                dt = datetime.fromisoformat(data_pedido[:10]).date()
            except Exception:
                dt = date.today()

            for item in pedido.get("order_items", []):
                info  = item.get("item", {})
                qtde  = item.get("quantity", 1)

                # Extrair SKU (mesmo padrão do vendas.py)
                sku = info.get("seller_sku", "")
                if not sku:
                    for attr in (info.get("variation_attributes") or []):
                        if attr.get("id") == "SELLER_SKU":
                            sku = attr.get("value_name", "")
                            break
                if not sku:
                    sku = str(info.get("id", ""))

                custo = custo_na_data(sku, dt)
                if custo is None:
                    sem_custo.add(sku)
                    continue

                valor = round(custo * qtde, 2)
                cmv_total += valor

                if sku not in detalhes:
                    detalhes[sku] = {"sku": sku, "qtde": 0, "custo_unitario": custo, "total": 0.0}
                detalhes[sku]["qtde"]  += qtde
                detalhes[sku]["total"] += valor

        return {
            "mes":       mes,
            "ano":       ano,
            "cmv_total": round(cmv_total, 2),
            "detalhes":  list(detalhes.values()),
            "sem_custo": list(sem_custo),
        }

    finally:
        await conn.close()
