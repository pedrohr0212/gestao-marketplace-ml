# database.py — Nexora Gestão Marketplace ML
import asyncpg
from config import get_settings

settings = get_settings()
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=10)
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id          SERIAL PRIMARY KEY,
                ml_user_id  TEXT UNIQUE NOT NULL,
                nickname    TEXT,
                email       TEXT,
                access_token  TEXT,
                refresh_token TEXT,
                token_expires TIMESTAMPTZ,
                criado_em   TIMESTAMPTZ DEFAULT NOW(),
                atualizado_em TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS vendas (
                id              SERIAL PRIMARY KEY,
                ml_order_id     BIGINT UNIQUE NOT NULL,
                usuario_id      INTEGER REFERENCES usuarios(id),
                status          TEXT,
                valor_total     NUMERIC(12,2),
                data_venda      TIMESTAMPTZ,
                comprador_id    BIGINT,
                via_publicidade BOOLEAN DEFAULT FALSE,
                dados_raw       JSONB,
                criado_em       TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS produtos (
                id             SERIAL PRIMARY KEY,
                ml_item_id     TEXT UNIQUE NOT NULL,
                usuario_id     INTEGER REFERENCES usuarios(id),
                titulo         TEXT,
                sku            TEXT,
                preco          NUMERIC(12,2),
                custo          NUMERIC(12,2) DEFAULT 0,
                estoque        INTEGER DEFAULT 0,
                estoque_full   INTEGER DEFAULT 0,
                dados_raw      JSONB,
                atualizado_em  TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS estoque_historico (
                id          SERIAL PRIMARY KEY,
                usuario_id  INTEGER REFERENCES usuarios(id),
                produto_id  INTEGER REFERENCES produtos(id),
                tipo        TEXT,
                quantidade  INTEGER,
                observacao  TEXT,
                criado_em   TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS pedidos_fornecedor (
                id              SERIAL PRIMARY KEY,
                usuario_id      INTEGER REFERENCES usuarios(id),
                fornecedor      TEXT,
                status          TEXT DEFAULT 'pendente',
                valor_total     NUMERIC(12,2),
                previsao_chegada DATE,
                dados           JSONB,
                criado_em       TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE INDEX IF NOT EXISTS idx_vendas_usuario   ON vendas(usuario_id);
            CREATE INDEX IF NOT EXISTS idx_vendas_data      ON vendas(data_venda);
            CREATE INDEX IF NOT EXISTS idx_produtos_usuario ON produtos(usuario_id);
        """)
    print("✅ Banco de dados inicializado")
