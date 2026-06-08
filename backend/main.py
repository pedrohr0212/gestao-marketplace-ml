# main.py — Nexora Gestão Marketplace ML
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from config import get_settings
from database import init_db
from auth import router as auth_router
from routes.vendas import router as vendas_router
from routes.estoque import router as estoque_router
from routes.publicidade import router as publicidade_router
from routes.webhooks import router as webhooks_router

settings = get_settings()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(
    title="Nexora — Gestão Marketplace ML",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS — permite GitHub Pages e desenvolvimento local
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pedrohr0212.github.io",
        "https://pedrohr0212.github.io/gestao-marketplace-ml",
        "http://localhost:3000",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(vendas_router)
app.include_router(estoque_router)
app.include_router(publicidade_router)
app.include_router(webhooks_router)

@app.get("/")
async def root():
    return {"app": "Nexora Gestão Marketplace ML", "versao": "1.0.0", "status": "online"}

@app.get("/health")
async def health():
    return {"status": "ok"}
