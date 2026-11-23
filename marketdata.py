"""Obtención de datos de mercado con estado explícito.

Regla de oro de este módulo: **nunca inventar un dato**. Si la volatilidad no
está disponible, se devuelve `None` con el motivo; el llamante decide si usa un
valor asumido (y entonces se marca como tal en la salida) o aborta.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from eurex_calendar import SPECS

# yfinance escribe a stderr cada ticker que no existe; aquí eso es esperado
# (los índices de volatilidad de Yahoo están caídos), así que se silencia.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

CACHE_TTL = float(os.getenv("MARKET_CACHE_TTL", "60"))
ALLOW_IV_FALLBACK = os.getenv("ALLOW_IV_FALLBACK", "0") == "1"
DEFAULT_IV = float(os.getenv("DEFAULT_IV", "0.20"))


@dataclass
class Quote:
    value: float | None
    source: str                 # "yahoo:^GDAXI" | "manual" | "assumed" | "missing"
    asof: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "value": None if self.value is None else round(float(self.value), 4),
            "source": self.source,
            "asof": self.asof.isoformat(timespec="seconds"),
            "note": self.note,
        }


@dataclass
class MarketSnapshot:
    index: str
    spot: Quote
    atm_iv: Quote
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "spot": self.spot.to_dict(),
            "atm_iv": self.atm_iv.to_dict(),
            "warnings": self.warnings,
        }


_cache: dict[tuple[str, str], tuple[float, Quote]] = {}


def _cached(key: tuple[str, str]) -> Quote | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    return None


def _store(key: tuple[str, str], quote: Quote) -> Quote:
    _cache[key] = (time.time(), quote)
    return quote


def clear_cache() -> None:
    _cache.clear()


def _yahoo_last(ticker: str) -> float | None:
    """Último precio de Yahoo, con `.fast_info` (ligero) y `.info` como reserva."""
    import yfinance as yf

    t = yf.Ticker(ticker)
    for getter in (
        lambda: t.fast_info["last_price"],
        lambda: t.fast_info["regular_market_price"],
        lambda: t.info.get("regularMarketPrice"),
        lambda: t.info.get("currentPrice"),
        lambda: t.info.get("previousClose"),
    ):
        try:
            v = getter()
            if v:
                return float(v)
        except Exception:
            continue
    return None


def get_spot(index: str, override: float | None = None) -> Quote:
    if override is not None:
        return Quote(float(override), "manual", note="precio introducido por el usuario")
    key = (index, "spot")
    if (hit := _cached(key)) is not None:
        return hit
    ticker = SPECS[index].yahoo_price
    try:
        v = _yahoo_last(ticker)
    except Exception as exc:                      # red caída, rate limit, etc.
        return _store(key, Quote(None, "missing", note=f"Yahoo falló: {exc.__class__.__name__}"))
    if v is None:
        return _store(key, Quote(None, "missing", note=f"sin precio en Yahoo para {ticker}"))
    return _store(key, Quote(v, f"yahoo:{ticker}"))


def get_atm_iv(index: str, override: float | None = None) -> Quote:
    """IV ATM en tanto por uno (0.20 = 20%)."""
    if override is not None and override > 0:
        return Quote(float(override), "manual", note="IV introducida por el usuario")
    key = (index, "iv")
    if (hit := _cached(key)) is not None:
        return hit
    tried: list[str] = []
    for ticker in SPECS[index].yahoo_iv_candidates:
        tried.append(ticker)
        try:
            v = _yahoo_last(ticker)
        except Exception:
            continue
        if v and 1.0 < v < 200.0:                 # un índice de vol va en puntos porcentuales
            return _store(key, Quote(v / 100.0, f"yahoo:{ticker}"))
    note = f"sin IV en Yahoo (probados: {', '.join(tried) or 'ninguno'})"
    return _store(key, Quote(None, "missing", note=note))


def build_snapshot(
    index: str,
    spot_override: float | None = None,
    iv_override: float | None = None,
) -> MarketSnapshot:
    spot = get_spot(index, spot_override)
    iv = get_atm_iv(index, iv_override)
    warnings: list[str] = []
    if not spot.ok:
        raise ValueError(f"{index}: sin precio de contado ({spot.note})")
    if not iv.ok:
        if ALLOW_IV_FALLBACK:
            iv = Quote(DEFAULT_IV, "assumed", note=f"IV ASUMIDA {DEFAULT_IV:.0%}; no es dato de mercado")
            warnings.append(
                f"{index}: IV no disponible → se usa {DEFAULT_IV:.0%} ASUMIDA. "
                "Los resultados NO reflejan el mercado real."
            )
        else:
            raise ValueError(
                f"{index}: IV ATM no disponible ({iv.note}). "
                "Introduce la IV de tu bróker o activa ALLOW_IV_FALLBACK=1."
            )
    return MarketSnapshot(index=index, spot=spot, atm_iv=iv, warnings=warnings)
