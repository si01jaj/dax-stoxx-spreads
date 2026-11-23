"""Calendario de Eurex y contratos de opciones sobre DAX (ODAX) y EURO STOXX 50 (OESX).

Fuentes oficiales usadas aquí:
  * Eurex Trading Calendar 2026 (PDF): Eurex cierra para todos los derivados el
    1 enero, 3 abril (Viernes Santo), 6 abril (Lunes de Pascua), 1 mayo y 25 diciembre;
    y no negocia el 24 y el 31 de diciembre.
  * El mismo calendario fija el fin de negociación del día de vencimiento:
    13:00 CET para ODAX y 12:00 CET para OESX.

Los festivos se generan por regla (Pascua + fechas fijas), de modo que 2027 y
posteriores salen solos; si Eurex publica una excepción, se añade a
HOLIDAY_OVERRIDES sin tocar la lógica.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Literal
from zoneinfo import ZoneInfo

CET = ZoneInfo("Europe/Berlin")
UTC = ZoneInfo("UTC")

IndexName = Literal["DAX", "STOXX"]
ExpiryKind = Literal["weekly", "monthly"]

# --------------------------------------------------------------------------- #
# Especificaciones de contrato
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class IndexSpec:
    name: str
    prefix: str                 # prefijo del ticker en el bróker (ODAX / OESX)
    expiry_time: time           # fin de negociación el día de vencimiento (CET)
    multiplier: float           # EUR por punto de índice
    strike_interval: float      # incremento de strike en la zona del dinero
    q: float                    # rendimiento por dividendo del subyacente
    yahoo_price: str            # ticker de Yahoo para el spot
    # tickers de Yahoo candidatos para la volatilidad ATM (pueden no existir ya)
    yahoo_iv_candidates: tuple[str, ...] = ()


SPECS: dict[str, IndexSpec] = {
    "DAX": IndexSpec(
        name="DAX",
        prefix="ODAX",
        expiry_time=time(13, 0),
        multiplier=5.0,
        strike_interval=50.0,
        q=0.0,                  # el DAX es un índice de rendimiento total
        yahoo_price="^GDAXI",
        yahoo_iv_candidates=("^V1XI", "^VDAX", "VDAX=F"),
    ),
    "STOXX": IndexSpec(
        name="STOXX",
        prefix="OESX",
        expiry_time=time(12, 0),
        multiplier=10.0,
        strike_interval=25.0,
        q=0.03,                 # EURO STOXX 50 es índice de precios (~3% de dividendo)
        yahoo_price="^STOXX50E",
        yahoo_iv_candidates=("^V2TX", "^VSTOXX", "VSTOXX=F"),
    ),
}

HOLIDAY_OVERRIDES: dict[int, set[date]] = {}


# --------------------------------------------------------------------------- #
# Festivos
# --------------------------------------------------------------------------- #
def easter_sunday(year: int) -> date:
    """Domingo de Pascua (algoritmo gregoriano de Meeus/Jones/Butcher)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def eurex_holidays(year: int) -> set[date]:
    """Días en que Eurex no negocia ningún derivado."""
    e = easter_sunday(year)
    days = {
        date(year, 1, 1),                  # Año Nuevo
        e - timedelta(days=2),             # Viernes Santo
        e + timedelta(days=1),             # Lunes de Pascua
        date(year, 5, 1),                  # Día del Trabajo
        date(year, 12, 24),                # Nochebuena (sin negociación)
        date(year, 12, 25),                # Navidad
        date(year, 12, 31),                # Nochevieja (sin negociación)
    }
    days |= HOLIDAY_OVERRIDES.get(year, set())
    return days


def is_holiday(d: date) -> bool:
    return d in eurex_holidays(d.year)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_holiday(d)


def previous_trading_day(d: date, n: int = 1) -> date:
    cur = d
    while n > 0:
        cur -= timedelta(days=1)
        if is_trading_day(cur):
            n -= 1
    return cur


def next_trading_day(d: date, n: int = 1) -> date:
    cur = d
    while n > 0:
        cur += timedelta(days=1)
        if is_trading_day(cur):
            n -= 1
    return cur


# --------------------------------------------------------------------------- #
# Vencimientos
# --------------------------------------------------------------------------- #
def third_friday(year: int, month: int) -> date:
    """Tercer viernes nominal del mes (puede caer en festivo)."""
    first = date(year, month, 1)
    days_to_fri = (4 - first.weekday()) % 7
    return first + timedelta(days=days_to_fri + 14)


def monthly_expiry(year: int, month: int) -> date:
    """Vencimiento mensual: tercer viernes, o el día hábil anterior si es festivo."""
    nominal = third_friday(year, month)
    return nominal if is_trading_day(nominal) else previous_trading_day(nominal)


def fridays_in_month(year: int, month: int) -> list[date]:
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    out = []
    while d.month == month:
        out.append(d)
        d += timedelta(days=7)
    return out


def weekly_expiries(year: int, month: int) -> list[tuple[date, int]]:
    """Vencimientos semanales del mes como [(fecha, número_de_semana)].

    En Eurex la semana del tercer viernes es el vencimiento mensual, así que no
    hay contrato semanal en esa semana. Si el viernes es festivo, no hay
    vencimiento esa semana.
    """
    nominal_monthly = third_friday(year, month)
    out: list[tuple[date, int]] = []
    for idx, f in enumerate(fridays_in_month(year, month), start=1):
        if f == nominal_monthly:
            continue
        if not is_trading_day(f):
            continue
        out.append((f, idx))
    return out


@dataclass(frozen=True)
class Contract:
    index: str
    expiry: date
    kind: ExpiryKind
    week: int | None = None

    @property
    def spec(self) -> IndexSpec:
        return SPECS[self.index]

    @property
    def ticker(self) -> str:
        """Ticker tipo bróker: mensual = ODAX/OESX; semanal = ODAX1..ODAX5."""
        if self.kind == "monthly":
            return self.spec.prefix
        return f"{self.spec.prefix}{self.week}"

    @property
    def expiry_dt(self) -> datetime:
        """Momento exacto del vencimiento (CET)."""
        return datetime.combine(self.expiry, self.spec.expiry_time, tzinfo=CET)

    def years_to_expiry(self, now: datetime | None = None) -> float:
        now = (now or datetime.now(tz=UTC)).astimezone(UTC)
        seconds = (self.expiry_dt.astimezone(UTC) - now).total_seconds()
        return max(seconds, 0.0) / (365.0 * 86400.0)

    def days_to_expiry(self, now: datetime | None = None) -> float:
        return self.years_to_expiry(now) * 365.0

    @property
    def label(self) -> str:
        return f"{self.ticker} {self.expiry.isoformat()}"


def month_iter(start: date, months_ahead: int = 0) -> tuple[int, int]:
    m = start.month + months_ahead
    y = start.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    return y, m


def upcoming_contracts(
    index: str,
    now: datetime | None = None,
    horizon_days: int = 200,
    min_minutes: float = 30.0,
) -> list[Contract]:
    """Todos los vencimientos futuros (semanal y mensual) ordenados por fecha."""
    now = now or datetime.now(tz=UTC)
    today = now.astimezone(CET).date()
    out: list[Contract] = []
    y, m = today.year, today.month
    for _ in range(14):  # 14 meses cubren cualquier horizonte razonable
        mexp = monthly_expiry(y, m)
        out.append(Contract(index, mexp, "monthly"))
        for f, week in weekly_expiries(y, m):
            out.append(Contract(index, f, "weekly", week))
        y, m = month_iter(date(y, m, 1), 1)
    # filtrar por horizonte y por tiempo mínimo restante (hoy a primera hora no vale)
    out = [c for c in out if (c.expiry - today).days <= horizon_days]
    out = [c for c in out if c.days_to_expiry(now) * 24 * 60 >= min_minutes]
    return sorted(out, key=lambda c: (c.expiry, c.kind != "monthly"))


def select_contract(
    index: str,
    kind: ExpiryKind,
    now: datetime | None = None,
    months_ahead: int = 0,
    min_minutes: float = 30.0,
) -> Contract:
    """Contrato del tipo pedido, el más cercano; months_ahead>0 salta N vencimientos."""
    now = now or datetime.now(tz=UTC)
    candidates = [c for c in upcoming_contracts(index, now, min_minutes=min_minutes) if c.kind == kind]
    if not candidates:
        raise ValueError(f"Sin vencimientos {kind} disponibles para {index}")
    if months_ahead >= len(candidates):
        raise ValueError(f"No hay {months_ahead + 1} vencimientos {kind} en el horizonte")
    return candidates[months_ahead]
