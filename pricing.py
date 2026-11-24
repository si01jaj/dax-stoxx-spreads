"""Precios, griegas y superficie de volatilidad (Black-Scholes sobre el forward).

Se trabaja siempre con el forward F = S·e^{(r-q)T} y su factor de descuento
DF = e^{-rT}: es la forma numéricamente estable y la que evita tener que
recordar si un delta es spot o forward.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.stats import norm

Kind = str  # "call" | "put"


# --------------------------------------------------------------------------- #
# Superficie de volatilidad
# --------------------------------------------------------------------------- #
@dataclass
class VolSurface:
    """Superficie paramétrica mínima: nivel ATM + pendiente de skew.

    IV(K, T) = atm + skew · z,  con z = ln(F/K) / (atm·√T)  (z>0 en puts OTM).

    `skew` va en unidades de volatilidad absoluta por unidad de z. Con
    skew=0.0217 se obtiene, en régimen normal, un put a 25Δ unos 2,5 puntos de
    vol por encima del ATM y un put a 10Δ unos 3,5-4 puntos: el orden de
    magnitud típico del skew de índices europeos.

    `atm_vol_override` permite pasar una IV ATM distinta para un vencimiento
    concreto (lo que el usuario teclea desde el bróker, o una vol a plazo).
    """

    atm: float
    skew: float = 0.0217
    skew_power: float = 1.0          # >1 acentúa el ala de puts
    min_iv: float = 0.02
    max_iv: float = 3.0
    _extra: dict = field(default_factory=dict, repr=False)

    def with_atm(self, atm: float) -> VolSurface:
        return VolSurface(atm, self.skew, self.skew_power, self.min_iv, self.max_iv)

    def iv(self, S: float, K: float, T: float, r: float, q: float) -> float:
        if T <= 0 or self.atm <= 0:
            return max(self.atm, self.min_iv)
        F = S * np.exp((r - q) * T)
        z = np.log(F / K) / (self.atm * np.sqrt(T))
        bump = self.skew * (z ** self.skew_power if z > 0 else -((-z) ** self.skew_power))
        return float(np.clip(self.atm + bump, self.min_iv, self.max_iv))


# --------------------------------------------------------------------------- #
# Black-Scholes sobre forward
# --------------------------------------------------------------------------- #
def _d1_d2(F: float, K: float, T: float, sigma: float) -> tuple[float, float]:
    if T <= 0 or sigma <= 0:
        raise ValueError("T y sigma deben ser positivos")
    vol_t = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * vol_t * vol_t) / vol_t
    return float(d1), float(d1 - vol_t)


def forward(spot: float, T: float, r: float, q: float) -> float:
    return spot * float(np.exp((r - q) * T))


def price(
    spot: float, strike: float, T: float, sigma: float, r: float, q: float, kind: Kind
) -> float:
    """Prima en puntos de índice (descontada)."""
    if T <= 0:
        intrinsic = max(spot - strike, 0.0) if kind == "call" else max(strike - spot, 0.0)
        return float(intrinsic)
    F = forward(spot, T, r, q)
    DF = float(np.exp(-r * T))
    d1, d2 = _d1_d2(F, strike, T, sigma)
    if kind == "call":
        return float(DF * (F * norm.cdf(d1) - strike * norm.cdf(d2)))
    return float(DF * (strike * norm.cdf(-d2) - F * norm.cdf(-d1)))


def greeks(
    spot: float, strike: float, T: float, sigma: float, r: float, q: float, kind: Kind
) -> dict:
    """Griegas en convenciones habituales.

    delta: por unidad de subyacente (spot). theta: por día natural.
    vega: por punto de volatilidad (1%). gamma: por punto de subyacente.
    """
    if T <= 0:
        return {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    F = forward(spot, T, r, q)
    DF = float(np.exp(-r * T))
    d1, d2 = _d1_d2(F, strike, T, sigma)
    pdf = float(norm.pdf(d1))
    if kind == "call":
        delta = float(np.exp(-q * T) * norm.cdf(d1))
        theta = (
            -spot * np.exp(-q * T) * pdf * sigma / (2 * np.sqrt(T))
            - r * strike * DF * norm.cdf(d2)
            + q * spot * np.exp(-q * T) * norm.cdf(d1)
        ) / 365.0
    else:
        delta = float(-np.exp(-q * T) * norm.cdf(-d1))
        theta = (
            -spot * np.exp(-q * T) * pdf * sigma / (2 * np.sqrt(T))
            + r * strike * DF * norm.cdf(-d2)
            - q * spot * np.exp(-q * T) * norm.cdf(-d1)
        ) / 365.0
    return {
        "delta": delta,
        "gamma": float(np.exp(-q * T) * pdf / (F * sigma * np.sqrt(T))),
        "theta": float(theta),
        "vega": float(spot * np.exp(-q * T) * pdf * np.sqrt(T) / 100.0),
    }


def implied_vol(
    target_price: float,
    spot: float,
    strike: float,
    T: float,
    r: float,
    q: float,
    kind: Kind,
    tol: float = 1e-6,
    max_iter: int = 100,
) -> float | None:
    """Volatilidad implícita por bisección (robusta, sin derivadas)."""
    if target_price <= 0 or T <= 0:
        return None
    lo, hi = 1e-4, 5.0
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        try:
            p = price(spot, strike, T, mid, r, q, kind)
        except ValueError:
            return None
        if abs(p - target_price) < tol:
            return float(mid)
        if p < target_price:
            lo = mid
        else:
            hi = mid
    return float(0.5 * (lo + hi))


# --------------------------------------------------------------------------- #
# Utilidades de strikes
# --------------------------------------------------------------------------- #
def round_to_tick(x: float, tick: float) -> float:
    return float(round(x / tick) * tick)


def strike_for_delta(
    spot: float,
    T: float,
    surface: VolSurface,
    kind: Kind,
    target_abs_delta: float,
    r: float,
    q: float,
    tick: float,
    lo_pct: float = 0.55,
    hi_pct: float = 1.30,
) -> float:
    """Strike (redondeado al tick) cuya |delta| es la pedida.

    Para puts busca por debajo del spot y para calls por encima; itera sobre la
    superficie (la IV depende de K, así que no vale una fórmula cerrada).
    """
    target = abs(target_abs_delta)
    if kind == "put":
        lo, hi = spot * lo_pct, spot * hi_pct     # delta creciente con K
    else:
        lo, hi = spot * lo_pct, spot * hi_pct     # delta decreciente con K
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        iv = surface.iv(spot, mid, T, r, q)
        d = abs(greeks(spot, mid, T, iv, r, q, kind)["delta"])
        # con K creciente: |put delta| sube, |call delta| baja
        if kind == "put":
            if d < target:
                lo = mid
            else:
                hi = mid
        else:
            if d > target:
                lo = mid
            else:
                hi = mid
    return round_to_tick(0.5 * (lo + hi), tick)


def strike_grid(spot: float, tick: float, sigmas: float, sigma_pct: float, max_strikes: int = 400) -> list[float]:
    """Rejilla de strikes que cubre ±sigmas desviaciones típicas del precio."""
    half_range = max(spot * sigma_pct * sigmas, 2 * tick)
    n = int(min(half_range / tick, max_strikes))
    center = round_to_tick(spot, tick)
    return [center + i * tick for i in range(-n, n + 1)]
