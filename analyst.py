"""Capa de análisis con LLM: solo redacta sobre números ya calculados.

Contrato del módulo: el modelo **no calcula nada**. Recibe la tabla de
candidatos (ya evaluada en Python) y devuelve JSON validado. Si el modelo falla,
alucina ids o no hay API key, se devuelve `None` y la aplicación sigue
funcionando con la tabla determinista.
"""
from __future__ import annotations

import json
import os
import re

SYSTEM_PROMPT = (
    "Eres analista de opciones sobre índices europeos (Eurex: ODAX y OESX). "
    "Recibes una tabla de estructuras YA CALCULADA. Reglas estrictas:\n"
    "1) No inventes ni recalcules cifras: usa exclusivamente las que aparecen en la tabla.\n"
    "2) Si una cifra no está en la tabla, no la menciones.\n"
    "3) No propongas estructuras ni strikes que no estén en la tabla.\n"
    "4) Puedes descartar candidatos y explicar por qué, con criterio de riesgo.\n"
    "5) La tabla puede traer estructuras de crédito (cobras prima) y de débito (pagas prima). "
    "No hay una dirección preferida: elige por las cifras, no por costumbre.\n"
    "6) Cuando el beneficio sea ilimitado el ROC no aplica: no lo compares con verticales.\n"
    "7) Responde SOLO con JSON válido, sin markdown ni texto adicional."
)

RESPONSE_SCHEMA = """{
  "summary": "2-4 frases sobre el contexto de mercado y la lectura de la tabla",
  "picks": [
    {
      "id": "id exacto de la tabla",
      "why": "por qué esta estructura, citando sus cifras",
      "risks": ["riesgo concreto 1", "riesgo concreto 2"],
      "management": "plan de gestión (cuándo cerrar, rol, stop)"
    }
  ],
  "avoid": ["motivos para no operar hoy, si los hay"]
}"""


def build_prompt(payload: dict, max_rows: int = 10) -> str:
    """Construye el mensaje de usuario con la tabla recortada a lo esencial."""
    lines: list[str] = []
    lines.append("CONTEXTO DE MERCADO")
    for idx, market in payload.get("markets", {}).items():
        spot = market["spot"]
        iv = market["atm_iv"]
        lines.append(
            f"- {idx}: spot={spot['value']} ({spot['source']}) | "
            f"IV ATM={None if iv['value'] is None else round(iv['value'] * 100, 1)}% ({iv['source']})"
        )
        for w in market.get("warnings", []):
            lines.append(f"  AVISO: {w}")

    lines.append("")
    lines.append("VENCIMIENTOS")
    for idx, info in payload.get("contracts", {}).items():
        lines.append(
            f"- {idx}: {info['ticker']} vence {info['expiry']} ({info['dte_days']:.2f} días, "
            f"EM 1σ ±{info['expected_move']:.0f} pts)"
        )

    lines.append("")
    lines.append("CANDIDATOS (calculados; prima y métricas en puntos de índice y EUR)")
    for idx, rows in payload.get("candidates", {}).items():
        if not rows:
            lines.append(f"- {idx}: ninguna estructura supera los filtros")
            continue
        for row in rows[:max_rows]:
            legs = " / ".join(f"{leg['side'][0].upper()}{leg['kind'][0].upper()}{leg['strike']:.0f}" for leg in row["legs"])
            max_p = "ilimitado" if row["max_profit_eur"] is None else f"{row['max_profit_eur']} EUR"
            roc = "n/a" if row["roc"] is None else f"{row['roc']:.1%}"
            lines.append(
                f"- id={row['id']} | {row['name']} | {row['direction']} | {legs} | "
                f"prima={row['premium_pts']} pts ({row['premium_eur']} EUR) | maxP={max_p} | "
                f"maxL={row['max_loss_eur']} EUR | POP={row['pop']:.0%} | EV={row['ev_pts']} pts "
                f"({row['ev_eur']} EUR) | ROC={roc} | BE={row['breakevens']} | score={row['score']}"
            )
    lines.append("")
    lines.append("Responde con este JSON exacto:")
    lines.append(RESPONSE_SCHEMA)
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    # por si el modelo envuelve en ```json ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def validate(payload: dict, response: dict) -> dict:
    """Se queda solo con los picks que apuntan a ids existentes."""
    valid_ids = {
        row["id"] for rows in payload.get("candidates", {}).values() for row in rows
    }
    picks = []
    dropped = []
    for pick in response.get("picks", []) or []:
        pid = str(pick.get("id", "")).strip()
        if pid in valid_ids:
            picks.append(
                {
                    "id": pid,
                    "why": str(pick.get("why", ""))[:800],
                    "risks": [str(r)[:300] for r in (pick.get("risks") or [])][:5],
                    "management": str(pick.get("management", ""))[:500],
                }
            )
        else:
            dropped.append(pid)
    return {
        "summary": str(response.get("summary", ""))[:1500],
        "picks": picks,
        "avoid": [str(a)[:300] for a in (response.get("avoid") or [])][:5],
        "dropped_picks": dropped,
    }


def analyse(payload: dict, api_key: str | None = None, model: str | None = None) -> dict | None:
    api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=api_key,
            base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
            timeout=float(os.getenv("LLM_TIMEOUT", "90")),
        )
        completion = client.chat.completions.create(
            model=model or os.getenv("LLM_MODEL", "deepseek-chat"),
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(payload)},
            ],
            temperature=0.2,
        )
        raw = completion.choices[0].message.content or ""
        parsed = _extract_json(raw)
        if parsed is None:
            return {"error": "respuesta no parseable como JSON", "raw": raw[:800]}
        return validate(payload, parsed)
    except Exception as exc:  # el LLM nunca debe tumbar la aplicación
        return {"error": f"{exc.__class__.__name__}: {exc}"}
