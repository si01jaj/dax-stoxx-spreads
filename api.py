import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import logging
from openai import OpenAI
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import main as engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dax-stoxx-api")

app = FastAPI(title="DAX & STOXX 50 - Spread Recomendador", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_last_context: str = ""
_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=engine.DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", timeout=60.0)
    return _client


class AnalyzeRequest(BaseModel):
    dax_price: float | None = None
    dax_iv: float | None = None
    stoxx_price: float | None = None
    stoxx_iv: float | None = None


class ChatRequest(BaseModel):
    message: str


@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    global _last_context
    try:
        result = engine.run_analysis(
            dax_price=req.dax_price,
            dax_iv=req.dax_iv,
            stoxx_price=req.stoxx_price,
            stoxx_iv=req.stoxx_iv,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        # Guardar contexto para el chat
        chains = result.get("chains", [])
        ctx = ""
        for c in chains:
            ctx += f"{c['name']} ({c['contract']}): precio={c['price']}, IV={c['iv_pct']}%, DTE={c['dte']}\n"
        _last_context = ctx + "\n" + result["response"]
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/defaults")
async def defaults():
    try:
        info = {}
        for name, cfg in engine.INDICES.items():
            market = engine.fetch_market_data(name, cfg)
            info[name] = {
                "yahoo_price": market["price"],
                "yahoo_iv_pct": round(market["iv_pct"] * 100, 1) if market["iv_pct"] else None,
            }
        expiry = engine.next_weekly_expiry()
        dte = (expiry - engine.date.today()).days
        return {
            "expiry": expiry.isoformat(),
            "dte": dte,
            "defaults": info,
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    global _last_context
    if not _last_context:
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
            {
                "role": "assistant",
                "content": f"Este es el análisis que acabamos de hacer:\n\n{_last_context}",
            },
            {"role": "user", "content": req.message},
        ]
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=messages,
        )
        reply = response.choices[0].message.content
        _last_context += f"\nUsuario: {req.message}\nExperto: {reply}"
        return {"response": reply}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
