"""API HTTP del recomendador ODAX / OESX.

Decisiones de robustez:
  * Endpoints síncronos (`def`): FastAPI los ejecuta en el threadpool, así que
    una llamada lenta (Yahoo, LLM) no bloquea el event loop ni al resto de
    peticiones.
  * Validación de entrada con límites reales (spot > 0, IV en (0, 3], expiries
    enumerados) en vez de aceptar cualquier float.
  * Errores tipados: ValueError → 400 con el motivo, resto → 500 sin filtrar
    trazas internas.
  * Auth opcional por token (`APP_TOKEN`); si está definido, todas las rutas
    /api/* lo exigen salvo /api/health.
  * El estado del chat es por sesión, con TTL y tope de mensajes: ni se comparte
    entre usuarios ni crece sin límite.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

import main as engine
import marketdata

VERSION = "2.2.2"
APP_TOKEN = os.getenv("APP_TOKEN", "")
_cors = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dax-stoxx-api")

app = FastAPI(title="Eurex ODAX / OESX — estructuras de crédito", version=VERSION)
if _cors:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-App-Token"],
    )


def require_token(x_app_token: str | None = Header(default=None)) -> None:
    if APP_TOKEN and x_app_token != APP_TOKEN:
        raise HTTPException(status_code=401, detail="token inválido")


ExpiryType = Literal["semanal", "mensual", "2m", "3m"]


class AnalyzeRequest(BaseModel):
    indices: list[Literal["DAX", "STOXX"]] = Field(default_factory=lambda: ["DAX", "STOXX"])
    expiry_type: ExpiryType = "semanal"
    dax_price: float | None = Field(default=None, gt=0, lt=1_000_000)
    dax_iv: float | None = Field(default=None, gt=0.01, le=3.0, description="IV ATM en tanto por uno")
    stoxx_price: float | None = Field(default=None, gt=0, lt=1_000_000)
    stoxx_iv: float | None = Field(default=None, gt=0.01, le=3.0)
    dax_rv: float | None = Field(default=None, gt=0.01, le=3.0, description="vol realizada esperada")
    stoxx_rv: float | None = Field(default=None, gt=0.01, le=3.0)
    narrative: bool = True
    min_pop: float | None = Field(default=None, ge=0.5, le=0.99)
    session_id: str = Field(default="default", max_length=64)

    @field_validator("indices")
    @classmethod
    def _non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("indica al menos un índice")
        return value


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(default="default", max_length=64)


# --------------------------------------------------------------------------- #
# Estado de sesión (acotado)
# --------------------------------------------------------------------------- #
_SESSIONS: dict[str, dict] = {}
SESSION_TTL = 3600.0
MAX_MESSAGES = 16
MAX_SESSIONS = 100


def _session(session_id: str) -> dict:
    now = time.time()
    for key in [k for k, v in _SESSIONS.items() if now - v["used"] > SESSION_TTL]:
        _SESSIONS.pop(key, None)
    if session_id not in _SESSIONS and len(_SESSIONS) >= MAX_SESSIONS:
        oldest = min(_SESSIONS, key=lambda k: _SESSIONS[k]["used"])
        _SESSIONS.pop(oldest, None)
    session = _SESSIONS.setdefault(session_id, {"payload": None, "messages": [], "used": now})
    session["used"] = now
    return session


def _context(payload: dict) -> str:
    lines = [
        f"Vencimiento objetivo: {payload['expiry_type']} · r={payload['risk_free_rate']:.2%} "
        f"· skew={payload['vol_skew']}"
    ]
    for index, info in payload["contracts"].items():
        market = payload["markets"][index]
        regime = market.get("regime", {})
        lines.append(
            f"{index} {info['ticker']} vence {info['expiry']} ({info['dte_days']:.2f} días), "
            f"spot {market['spot']['value']:.1f} (fuente {market['spot']['source']}), "
            f"IV ATM {(market['atm_iv']['value'] or 0) * 100:.1f}% (fuente {market['atm_iv']['source']}), "
            f"vol realizada esperada {regime.get('realized_vol_pct')}% (fuente {regime.get('source')}), "
            f"sesgo {regime.get('bias')}."
        )
    for rows in payload["candidates"].values():
        for row in rows:
            legs = " / ".join(f"{leg['side']} {leg['kind']} {leg['strike']:.0f}" for leg in row["legs"])
            max_p = "ilimitado" if row["max_profit_eur"] is None else f"{row['max_profit_eur']} EUR"
            roc = "n/a" if row["roc"] is None else f"{row['roc']:.1%}"
            lines.append(
                f"[{row['id']}] {row['name']} ({row['direction']}, {legs}): prima {row['premium_pts']} pts "
                f"({row['premium_eur']} EUR), maxP {max_p}, "
                f"maxL {row['max_loss_eur']} EUR, POP {row['pop']:.0%}, EV {row['ev_pts']} pts, "
                f"ROC {roc}, BE {row['breakevens']}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": VERSION,
        "auth_required": bool(APP_TOKEN),
        "iv_fallback": marketdata.ALLOW_IV_FALLBACK,
        "iv_provider": marketdata.IV_PROVIDER,
        "spot_provider": marketdata.SPOT_PROVIDER,
    }


@app.get("/api/defaults", dependencies=[Depends(require_token)])
def defaults(
    expiry_type: ExpiryType = Query(default="semanal"),
    now: str | None = Query(default=None, description="ISO datetime para simular otra fecha"),
) -> dict:
    """Datos de partida del formulario. Devuelve lo que haya, sin inventar la IV.

    No reutiliza run_analysis a propósito: aquí interesa el precio aunque falte
    la volatilidad, para que el usuario solo tenga que teclear la IV del bróker.
    """
    from datetime import datetime

    from eurex_calendar import select_contract

    kind, skip = engine.EXPIRY_MAP[expiry_type]
    ref = datetime.fromisoformat(now) if now else None
    markets: dict[str, dict] = {}
    contracts: dict[str, dict] = {}
    warnings: list[str] = []
    errors: dict[str, str] = {}

    for index in ("DAX", "STOXX"):
        spot = marketdata.get_spot(index)
        iv = marketdata.get_atm_iv(index)
        markets[index] = {"spot": spot.to_dict(), "atm_iv": iv.to_dict()}
        spot_value = spot.value
        if spot_value is None:
            errors[index] = spot.note or "sin precio"
            continue
        if not iv.ok:
            warnings.append(f"{index}: {iv.note}" if iv.note else f"{index}: IV no disponible")
        try:
            contract = select_contract(index, kind, now=ref, months_ahead=skip)
        except ValueError as exc:
            errors[index] = str(exc)
            continue
        spec = engine.SPECS[index]
        dte = contract.days_to_expiry(ref)
        em = round(spot_value * iv.value * (dte / 365.0) ** 0.5, 1) if iv.value is not None else None
        contracts[index] = {
            "ticker": contract.ticker,
            "expiry": contract.expiry.isoformat(),
            "expiry_time_cet": spec.expiry_time.strftime("%H:%M"),
            "dte_days": round(dte, 3),
            "expected_move": em,
            "multiplier": spec.multiplier,
            "dividend_yield": spec.q,
        }

    return {
        "version": VERSION,
        "expiry_type": expiry_type,
        "markets": markets,
        "contracts": contracts,
        "warnings": warnings,
        "errors": errors,
    }


@app.post("/api/analyze", dependencies=[Depends(require_token)])
def analyze(req: AnalyzeRequest) -> dict:
    cfg = engine.CandidateConfig()
    if req.min_pop is not None:
        cfg.min_pop = req.min_pop
    try:
        payload = engine.run_analysis(
            indices=tuple(req.indices),
            expiry_type=req.expiry_type,
            spot_overrides={"DAX": req.dax_price, "STOXX": req.stoxx_price},
            iv_overrides={"DAX": req.dax_iv, "STOXX": req.stoxx_iv},
            rv_overrides={"DAX": req.dax_rv, "STOXX": req.stoxx_rv},
            use_llm=req.narrative,
            config=cfg,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - red inesperada
        logger.exception("analyze falló")
        raise HTTPException(status_code=500, detail="error interno al analizar") from exc

    if all(index in payload["errors"] for index in req.indices):
        raise HTTPException(status_code=422, detail=payload["errors"])
    _session(req.session_id)["payload"] = payload
    return payload


@app.post("/api/chat", dependencies=[Depends(require_token)])
def chat(req: ChatRequest) -> dict:
    session = _session(req.session_id)
    if not session["payload"]:
        raise HTTPException(status_code=409, detail="primero ejecuta un análisis")
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise HTTPException(status_code=503, detail="DEEPSEEK_API_KEY no configurada")

    from openai import OpenAI

    client = OpenAI(api_key=key, base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"), timeout=60.0)
    context = _context(session["payload"])
    messages = [
        {
            "role": "system",
            "content": (
                "Analista de opciones sobre índices europeos (Eurex ODAX/OESX). "
                "Respondes dudas sobre el análisis ya realizado usando SOLO las cifras del contexto. "
                "Si falta un dato, lo dices en vez de inventarlo. No das consejo de inversión."
            ),
        },
        {"role": "user", "content": f"Contexto del análisis:\n{context}"},
        *session["messages"][-MAX_MESSAGES:],
        {"role": "user", "content": req.message},
    ]
    try:
        completion = client.chat.completions.create(
            model=os.getenv("LLM_MODEL", "deepseek-chat"), messages=messages, temperature=0.2
        )
    except Exception as exc:
        logger.warning("chat LLM falló: %s", exc)
        raise HTTPException(status_code=502, detail="el modelo no respondió") from exc

    reply = completion.choices[0].message.content or ""
    session["messages"] = (session["messages"] + [
        {"role": "user", "content": req.message},
        {"role": "assistant", "content": reply},
    ])[-MAX_MESSAGES:]
    return {"response": reply}


@app.post("/api/chat/reset")
def reset_chat(session_id: str = Query(default="default")) -> dict:
    _SESSIONS.pop(session_id, None)
    return {"reset": True}


static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(static_dir):
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
