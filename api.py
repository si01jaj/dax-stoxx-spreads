import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio
import logging
from collections import deque
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel, Field

import main as engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dax-stoxx-api")

# Contexto de la conversación: acotado en tamaño y con turnos limitados.
# Antes era un str global que crecía sin tope (coste de tokens creciente y fuga
# de contexto entre usuarios distintos del mismo proceso).
MAX_CONTEXT_CHARS = 20_000
MAX_CONTEXT_TURNS = 20

ExpiryType = Literal["semanal", "mensual", "2meses", "3meses"]

app = FastAPI(title="DAX & STOXX 50 - Spread Recomendador", version="1.1.0")

# CORS cerrado por defecto: el front se sirve desde el mismo origen, así que no
# necesita cabeceras CORS. Se abre solo si se declara ALLOWED_ORIGINS.
_allowed_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

_context: deque[str] = deque(maxlen=MAX_CONTEXT_TURNS)
_context_lock = asyncio.Lock()
_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = engine.DEEPSEEK_API_KEY or os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY no configurada")
        _client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com", timeout=60.0)
    return _client


def _context_text() -> str:
    return "\n".join(_context)[-MAX_CONTEXT_CHARS:]


class AnalyzeRequest(BaseModel):
    dax_price: float | None = Field(default=None, gt=0)
    dax_iv: float | None = Field(default=None, gt=0, lt=5)
    stoxx_price: float | None = Field(default=None, gt=0)
    stoxx_iv: float | None = Field(default=None, gt=0, lt=5)
    expiry_type: ExpiryType = "semanal"


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        result = engine.run_analysis(
            dax_price=req.dax_price,
            dax_iv=req.dax_iv,
            stoxx_price=req.stoxx_price,
            stoxx_iv=req.stoxx_iv,
            expiry_type=req.expiry_type,
        )
    except Exception:
        logger.exception("fallo inesperado en /api/analyze")
        raise HTTPException(status_code=500, detail="Error interno analizando el mercado") from None

    if "error" in result:
        # fallo al traer datos de mercado: es del proveedor, no del cliente
        logger.warning("analyze sin datos: %s", result["error"])
        raise HTTPException(status_code=502, detail=result["error"])

    ctx = "\n".join(
        f"{c['name']} ({c['contract']}): precio={c['price']}, IV={c['iv_pct']}%, DTE={c['dte']}"
        for c in result.get("chains", [])
    )
    async with _context_lock:
        _context.clear()
        _context.append(ctx + "\n" + result["response"])
    return result


@app.get("/api/defaults")
async def defaults(expiry_type: ExpiryType = "semanal"):
    try:
        info = {}
        for name, cfg in engine.INDICES.items():
            market = engine.fetch_market_data(name, cfg)
            info[name] = {
                "yahoo_price": market["price"],
                "yahoo_iv_pct": round(market["iv_pct"] * 100, 1) if market["iv_pct"] else None,
            }
        exp, dte = engine.get_expiry(expiry_type)
        return {"expiry": exp.isoformat(), "dte": dte, "defaults": info}
    except Exception:
        logger.exception("fallo en /api/defaults")
        raise HTTPException(status_code=502, detail="No se pudo leer el mercado") from None


@app.post("/api/chat")
async def chat(req: ChatRequest):
    async with _context_lock:
        context = _context_text()
    if not context:
        raise HTTPException(status_code=400, detail="Primero ejecuta un análisis")

    try:
        client = get_client()
        messages = [
            {
                "role": "system",
                "content": "Eres un trader de opciones europeas (EUREX) experto en DAX y STOXX 50. "
                           "Respondes preguntas sobre el análisis ya realizado. Usas los datos del "
                           "contexto para responder con precisión numérica. Siempre incluyes "
                           "disclaimer al final.",
            },
            {"role": "assistant", "content": f"Este es el análisis que acabamos de hacer:\n\n{context}"},
            {"role": "user", "content": req.message},
        ]
        response = client.chat.completions.create(model="deepseek-chat", messages=messages)
        reply = response.choices[0].message.content
    except HTTPException:
        raise
    except Exception:
        logger.exception("fallo en /api/chat")
        raise HTTPException(status_code=502, detail="El modelo no respondió") from None

    async with _context_lock:
        _context.append(f"Usuario: {req.message}\nExperto: {reply}")
    return {"response": reply}


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.1.0"}


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
