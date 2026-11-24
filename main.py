"""Motor de análisis de estructuras sobre ODAX y OESX.

Uso CLI:
    python main.py --expiry semanal --iv DAX=25.6 --rv DAX=28 --no-llm
    python main.py --expiry 2m --json > analisis.json

El resultado es el mismo diccionario que consume la API: mercados con su fuente
de dato, contratos elegidos, tabla de candidatos calculada y, si se pide, la
redacción del LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime

from eurex_calendar import SPECS, ExpiryKind, select_contract
from marketdata import build_snapshot
from pricing import VolSurface
from structures import (
    CREDIT_KINDS,
    DEBIT_KINDS,
    CandidateConfig,
    build_candidates,
    rank,
)

INDICES = ("DAX", "STOXX")

# tipo de UI -> (tipo de vencimiento, vencimientos a saltar)
EXPIRY_MAP: dict[str, tuple[ExpiryKind, int]] = {
    "semanal": ("weekly", 0),
    "mensual": ("monthly", 0),
    "2m": ("monthly", 1),
    "3m": ("monthly", 2),
}

DISCLAIMER = (
    "Herramienta de análisis. No es asesoramiento financiero. "
    "Las cifras son teóricas: no incluyen comisiones, horquilla ni margen PRMS."
)


def _parse_pairs(values: list[str] | None, percent: bool = False) -> dict[str, float | None]:
    """Interpreta `IDX=VALOR` repetidos. Con percent=True admite 25.6 y 0.256."""
    out: dict[str, float | None] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"formato esperado CLAVE=VALOR, recibido '{item}'")
        key, raw = item.split("=", 1)
        key = key.strip().upper()
        if key not in INDICES:
            raise ValueError(f"índice desconocido '{key}' (usa {'/'.join(INDICES)})")
        value = float(raw.replace(",", ".").replace("%", "").strip())
        if percent and value > 3.0:      # 25.6 = 25,6% ; 0.256 = fracción
            value = value / 100.0
        out[key] = value
    return out


def _regime(atm_iv: float, sigma_rw: float, source: str) -> dict:
    """Dirección sugerida según IV vs vol realizada esperada."""
    ratio = sigma_rw / atm_iv if atm_iv > 0 else 1.0
    if ratio >= 1.05:
        bias = "comprar"
        why = "la vol que esperas realizar está por encima de la implícita: la prima está barata"
    elif ratio <= 0.95:
        bias = "vender"
        why = "la vol que esperas realizar está por debajo de la implícita: la prima está cara"
    else:
        bias = "neutral"
        why = "IV y vol realizada esperada están prácticamente igualadas: sin ventaja clara"
    return {
        "atm_iv_pct": round(atm_iv * 100, 2),
        "realized_vol_pct": round(sigma_rw * 100, 2),
        "ratio": round(ratio, 3),
        "source": source,          # "manual" | "assumed"
        "bias": bias,
        "why": why,
    }


def run_analysis(
    indices: tuple[str, ...] = INDICES,
    expiry_type: str = "semanal",
    spot_overrides: dict[str, float | None] | None = None,
    iv_overrides: dict[str, float | None] | None = None,
    rv_overrides: dict[str, float | None] | None = None,
    use_llm: bool = True,
    risk_free: float | None = None,
    skew: float | None = None,
    config: CandidateConfig | None = None,
    now: datetime | None = None,
) -> dict:
    """Analiza los índices pedidos y devuelve el payload completo (JSON-ready)."""
    if expiry_type not in EXPIRY_MAP:
        raise ValueError(f"expiry_type inválido '{expiry_type}' (usa {', '.join(EXPIRY_MAP)})")
    kind, skip = EXPIRY_MAP[expiry_type]
    r = float(os.getenv("RISK_FREE_RATE", "0.025")) if risk_free is None else risk_free
    sk = float(os.getenv("VOL_SKEW", "0.0217")) if skew is None else skew
    cfg = config or CandidateConfig()
    now = now or datetime.now(tz=UTC)

    payload: dict = {
        "generated_at": now.isoformat(timespec="seconds"),
        "expiry_type": expiry_type,
        "risk_free_rate": r,
        "vol_skew": sk,
        "markets": {},
        "contracts": {},
        "candidates": {},
        "rejected": {},
        "warnings": [],
        "errors": {},
        "disclaimer": DISCLAIMER,
    }

    for index in indices:
        spec = SPECS[index]
        try:
            snapshot = build_snapshot(index, (spot_overrides or {}).get(index), (iv_overrides or {}).get(index))
            contract = select_contract(index, kind, now=now, months_ahead=skip)
        except ValueError as exc:
            payload["errors"][index] = str(exc)
            continue

        spot = snapshot.spot.value
        atm_iv = snapshot.atm_iv.value
        if spot is None or atm_iv is None:
            payload["errors"][index] = "dato de mercado incompleto"
            continue

        rv_manual = (rv_overrides or {}).get(index)
        source = "manual" if rv_manual else "assumed"
        sigma_rw = float(rv_manual) if rv_manual else atm_iv * cfg.realized_vol_factor
        regime = _regime(atm_iv, sigma_rw, source)

        payload["markets"][index] = {
            **snapshot.to_dict(),
            "realized_vol": {"value": round(sigma_rw, 4), "source": source},
            "regime": regime,
        }
        payload["warnings"].extend(snapshot.warnings)

        dte_days = contract.days_to_expiry(now)
        surface = VolSurface(atm=atm_iv, skew=sk)
        expected_move = spot * surface.atm * (max(dte_days, 0.0) / 365.0) ** 0.5

        payload["contracts"][index] = {
            "ticker": contract.ticker,
            "expiry": contract.expiry.isoformat(),
            "kind": contract.kind,
            "week": contract.week,
            "expiry_time_cet": spec.expiry_time.strftime("%H:%M"),
            "dte_days": round(dte_days, 3),
            "expected_move": round(expected_move, 1),
            "multiplier": spec.multiplier,
            "strike_interval": spec.strike_interval,
            "dividend_yield": spec.q,
        }

        candidates = build_candidates(
            index, contract, spot, surface, r, cfg, sigma_realized=sigma_rw
        )
        accepted = rank(candidates, cfg)
        rejected = [c for c in candidates if not c.accepted]
        payload["candidates"][index] = [c.to_dict() for c in accepted]
        payload["rejected"][index] = {
            "count": len(rejected),
            "top_reasons": _top_reasons(rejected),
        }

    payload["narrative"] = None
    if use_llm and payload["candidates"]:
        import analyst

        payload["narrative"] = analyst.analyse(payload)
    return payload


def _top_reasons(rejected: list) -> list[dict]:
    counts: dict[str, int] = {}
    for spread in rejected:
        for reason in spread.rejections:
            counts[reason] = counts.get(reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return [{"reason": reason, "count": n} for reason, n in ordered]


# --------------------------------------------------------------------------- #
# Salida por terminal
# --------------------------------------------------------------------------- #
_ETIQUETA = {
    "bull_put": "BP",
    "bear_call": "BC",
    "iron_condor": "IC",
    "bull_call": "BCd",
    "bear_put": "BPd",
    "straddle": "STD",
    "strangle": "STR",
}


def _fmt_eur(value: float | None, signo: bool = False) -> str:
    if value is None:
        return "∞"
    pref = "+" if signo and value > 0 else ""
    return f"{pref}{value:,.0f}€".replace(",", ".")


def render(payload: dict) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    if console.width < 100:            # evita que rich trunque las columnas
        console = Console(width=100)
    console.print(
        Panel.fit(
            f"[bold cyan]Eurex — ODAX / OESX[/bold cyan]  vencimiento [bold]{payload['expiry_type']}[/bold]\n"
            f"[dim]{payload['generated_at']} · r={payload['risk_free_rate']:.2%} · skew={payload['vol_skew']}[/dim]",
            border_style="cyan",
        )
    )

    for warning in payload["warnings"]:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for error in payload["errors"].values():
        console.print(f"[red]✗ {error}[/red]")

    for index, info in payload["contracts"].items():
        market = payload["markets"][index]
        iv = market["atm_iv"]
        regime = market["regime"]
        console.print(
            f"\n[bold]{index}[/bold] — [cyan]{info['ticker']}[/cyan] · vence {info['expiry']} "
            f"{info['expiry_time_cet']} CET · [yellow]{info['dte_days']:.2f} días[/yellow] · "
            f"EM ±{info['expected_move']:,.0f} pts".replace(",", ".")
        )
        console.print(
            f"  spot {market['spot']['value']:,.1f} ({market['spot']['source']}) · "
            f"IV ATM {iv['value'] * 100:.1f}% ({iv['source']}) · "
            f"RV {regime['realized_vol_pct']:.1f}% ({regime['source']}) · q={info['dividend_yield']:.1%}".replace(",", ".")
        )
        color = {"comprar": "green", "vender": "yellow", "neutral": "white"}[regime["bias"]]
        console.print(f"  [dim]sesgo:[/dim] [{color}]{regime['bias']}[/{color}] [dim]{regime['why']}[/dim]")

        rows = payload["candidates"].get(index, [])
        if not rows:
            rejected = payload["rejected"].get(index, {})
            console.print(f"  [yellow]Ninguna estructura supera los filtros[/yellow] ({rejected.get('count', 0)} rechazadas)")
            for item in rejected.get("top_reasons", []):
                console.print(f"    [dim]{item['count']}× {item['reason']}[/dim]")
            continue

        for direction, _kinds, titulo, estilo in (
            ("credit", CREDIT_KINDS, "crédito — cobras prima", "yellow"),
            ("debit", DEBIT_KINDS, "débito — pagas prima", "green"),
        ):
            subset = [r for r in rows if r["direction"] == direction]
            if not subset:
                continue
            table = Table(
                title=f"{titulo}",
                title_justify="left",
                title_style=f"bold {estilo}",
                show_header=True,
                header_style="bold",
                box=None,
                pad_edge=False,
                padding=(0, 2),
            )
            table.add_column("Estructura", justify="left", no_wrap=True)
            table.add_column("Prima", justify="right", no_wrap=True)
            table.add_column("Máx. P", justify="right", no_wrap=True)
            table.add_column("Máx. L", justify="right", no_wrap=True)
            table.add_column("POP", justify="right", no_wrap=True)
            table.add_column("EV", justify="right", no_wrap=True)
            table.add_column("ROC", justify="right", no_wrap=True)
            table.add_column("Breakeven", justify="right", no_wrap=True)

            last_kind = None
            for row in subset:
                if last_kind is not None and row["kind"] != last_kind:
                    table.add_section()
                last_kind = row["kind"]
                legs = row["legs"]
                if row["kind"] in ("straddle", "strangle"):
                    strikes = "/".join(f"{leg['strike']:.0f}" for leg in legs)
                elif row["kind"] == "iron_condor":
                    strikes = "/".join(f"{leg['strike']:.0f}" for leg in legs if leg["side"] == "sell")
                else:
                    strikes = f"{legs[0]['strike']:.0f}/{legs[1]['strike']:.0f}"
                ancho = row["width_pts"]
                etiqueta = f"{_ETIQUETA.get(row['kind'], row['kind'])} {strikes}"
                if ancho:
                    etiqueta += f" ·{ancho:.0f}"
                table.add_row(
                    etiqueta,
                    _fmt_eur(row["premium_eur"], signo=True),
                    _fmt_eur(row["max_profit_eur"]),
                    _fmt_eur(row["max_loss_eur"]),
                    f"{row['pop']:.0%}",
                    _fmt_eur(row["ev_eur"], signo=True),
                    "—" if row["roc"] is None else f"{row['roc']:.0%}",
                    "/".join(f"{b:.0f}" for b in row["breakevens"]),
                )
            console.print(table)
        console.print(
            "[dim]BP bull put · BC bear call · IC iron condor · BCd/BPd bull call y bear put de débito · "
            "STD straddle · STR strangle. Ordenado por EV/riesgo; 2 mejores de cada clase.[/dim]"
        )
        for row in rows:
            for warning in row["warnings"]:
                console.print(f"  [dim]· {row['name']}: {warning}[/dim]")

    narrative = payload.get("narrative")
    if narrative:
        if narrative.get("error"):
            console.print(f"\n[yellow]Análisis LLM no disponible: {narrative['error']}[/yellow]")
        else:
            console.print(Panel(narrative.get("summary", ""), title="Lectura del analista", border_style="green"))
            for pick in narrative.get("picks", []):
                console.print(f"\n[bold green]{pick['id']}[/bold green]\n  {pick['why']}")
                for risk in pick["risks"]:
                    console.print(f"  [yellow]riesgo:[/yellow] {risk}")
                console.print(f"  [cyan]gestión:[/cyan] {pick['management']}")
            for item in narrative.get("avoid", []):
                console.print(f"[yellow]· {item}[/yellow]")

    console.print(f"\n[dim]{payload['disclaimer']}[/dim]")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Estructuras ODAX / OESX (Eurex)")
    parser.add_argument("--index", default="DAX,STOXX", help="DAX, STOXX o ambos separados por coma")
    parser.add_argument("--expiry", default="semanal", choices=sorted(EXPIRY_MAP), help="vencimiento objetivo")
    parser.add_argument("--spot", action="append", metavar="IDX=PRECIO", help="precio de contado manual")
    parser.add_argument("--iv", action="append", metavar="IDX=IV", help="IV ATM del bróker (25.6 o 0.256)")
    parser.add_argument(
        "--rv",
        action="append",
        metavar="IDX=RV",
        help="vol realizada esperada (25.6 o 0.256). Si no se da, se usa IV×0.90",
    )
    parser.add_argument("--no-llm", action="store_true", help="solo tabla determinista")
    parser.add_argument("--json", action="store_true", help="salida JSON completa")
    parser.add_argument("--min-pop", type=float, default=None)
    parser.add_argument("--max-width", type=int, default=None, help="ancho máximo en ticks")
    args = parser.parse_args(argv)

    indices = tuple(i.strip().upper() for i in args.index.split(",") if i.strip())
    unknown = [i for i in indices if i not in INDICES]
    if unknown:
        parser.error(f"índices desconocidos: {unknown}")

    cfg = CandidateConfig()
    if args.min_pop is not None:
        cfg.min_pop = args.min_pop
    if args.max_width is not None:
        cfg.max_width_ticks = args.max_width

    try:
        payload = run_analysis(
            indices=indices,
            expiry_type=args.expiry,
            spot_overrides=_parse_pairs(args.spot),
            iv_overrides=_parse_pairs(args.iv, percent=True),
            rv_overrides=_parse_pairs(args.rv, percent=True),
            use_llm=not args.no_llm,
            config=cfg,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        render(payload)

    if len(payload["errors"]) == len(indices):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
