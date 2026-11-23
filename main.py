"""Motor de análisis de estructuras de crédito sobre ODAX y OESX.

Uso CLI:
    python main.py --expiry semanal --iv DAX=0.256 --no-llm
    python main.py --expiry 2m --json > analisis.json

El resultado es siempre el mismo diccionario que consume la API: mercados con
su fuente de dato, contratos elegidos, tabla de candidatos calculada y, si se
pide, la redacción del LLM.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

from eurex_calendar import SPECS, ExpiryKind, select_contract
from marketdata import build_snapshot
from pricing import VolSurface
from structures import CandidateConfig, build_candidates, rank

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


def _parse_pairs(values: list[str] | None) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for item in values or []:
        if "=" not in item:
            raise ValueError(f"formato esperado CLAVE=VALOR, recibido '{item}'")
        key, raw = item.split("=", 1)
        key = key.strip().upper()
        if key not in INDICES:
            raise ValueError(f"índice desconocido '{key}' (usa {'/'.join(INDICES)})")
        out[key] = float(raw.replace(",", "."))
    return out


def run_analysis(
    indices: tuple[str, ...] = INDICES,
    expiry_type: str = "semanal",
    spot_overrides: dict[str, float | None] | None = None,
    iv_overrides: dict[str, float | None] | None = None,
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
    now = now or datetime.now(tz=timezone.utc)

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

        payload["markets"][index] = snapshot.to_dict()
        payload["warnings"].extend(snapshot.warnings)

        spot = snapshot.spot.value
        atm_iv = snapshot.atm_iv.value
        if spot is None or atm_iv is None:      # build_snapshot ya lo impide, pero cerramos el tipo
            payload["errors"][index] = "dato de mercado incompleto"
            continue

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

        candidates = build_candidates(index, contract, spot, surface, r, cfg)
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
def render(payload: dict) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()
    console.print(
        Panel.fit(
            f"[bold cyan]Eurex — ODAX / OESX[/bold cyan]  vencimiento [bold]{payload['expiry_type']}[/bold]\n"
            f"[dim]{payload['generated_at']} · r={payload['risk_free_rate']:.2%} · skew={payload['vol_skew']}[/dim]",
            border_style="cyan",
        )
    )

    for warning in payload["warnings"]:
        console.print(f"[yellow]⚠ {warning}[/yellow]")
    for index, error in payload["errors"].items():
        console.print(f"[red]✗ {error}[/red]")

    for index, info in payload["contracts"].items():
        market = payload["markets"][index]
        iv = market["atm_iv"]
        console.print(
            f"\n[bold]{index}[/bold] — [cyan]{info['ticker']}[/cyan] vence {info['expiry']} "
            f"a las {info['expiry_time_cet']} CET → [yellow]{info['dte_days']:.2f} días[/yellow]\n"
            f"  spot {market['spot']['value']:,.1f} ([dim]{market['spot']['source']}[/dim]) · "
            f"IV ATM {(iv['value'] or 0) * 100:.1f}% ([dim]{iv['source']}[/dim]) · "
            f"EM 1σ ±{info['expected_move']:,.0f} pts · q={info['dividend_yield']:.1%}"
        )

        rows = payload["candidates"].get(index, [])
        if not rows:
            rejected = payload["rejected"].get(index, {})
            console.print(
                f"  [yellow]Ninguna estructura supera los filtros[/yellow] "
                f"({rejected.get('count', 0)} rechazadas)"
            )
            for item in rejected.get("top_reasons", []):
                console.print(f"    [dim]{item['count']}× {item['reason']}[/dim]")
            continue

        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("Estructura", justify="left", no_wrap=True)
        for col in ("Ancho", "Crédito", "Máx L", "POP", "EV", "ROC", "BE", "Score"):
            table.add_column(col, justify="right", no_wrap=True)

        short_kind = {"bull_put": "BP", "bear_call": "BC", "iron_condor": "IC"}
        for row in rows:
            legs = row["legs"]
            width = row.get("width_pts") or 0
            if row["kind"] == "iron_condor":
                strikes = f"{legs[0]['strike']:.0f}/{legs[2]['strike']:.0f}"
            else:
                strikes = f"{legs[0]['strike']:.0f}/{legs[1]['strike']:.0f}"
            table.add_row(
                f"{short_kind.get(row['kind'], '?')} {strikes}",
                f"{width:.0f}",
                f"{row['credit_pts']:.0f}p/{row['credit_eur']:.0f}€",
                f"[red]{row['max_loss_eur']:.0f}€[/red]",
                f"{row['pop']:.0%}",
                f"{row['ev_eur']:+.0f}€",
                f"{row['roc']:.1%}",
                "/".join(f"{b:.0f}" for b in row["breakevens"]),
                f"{row['score']:.2f}",
            )
        console.print(table)
        console.print(
            f"[dim]POP y EV con σ realizada {rows[0]['sigma_realized_pct']:.1f}% "
            f"(implícita {rows[0]['atm_iv_pct']:.1f}%). BP=bull put, BC=bear call, IC=iron condor.[/dim]"
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
    parser = argparse.ArgumentParser(description="Estructuras de crédito ODAX / OESX (Eurex)")
    parser.add_argument("--index", default="DAX,STOXX", help="DAX, STOXX o ambos separados por coma")
    parser.add_argument("--expiry", default="semanal", choices=sorted(EXPIRY_MAP), help="vencimiento objetivo")
    parser.add_argument("--spot", action="append", metavar="IDX=PRECIO", help="precio de contado manual")
    parser.add_argument("--iv", action="append", metavar="IDX=IV", help="IV ATM manual en tanto por uno (0.256)")
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
        cfg.width_ticks = tuple(w for w in cfg.width_ticks if w <= args.max_width) or (args.max_width,)

    try:
        payload = run_analysis(
            indices=indices,
            expiry_type=args.expiry,
            spot_overrides=_parse_pairs(args.spot),
            iv_overrides=_parse_pairs(args.iv),
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
