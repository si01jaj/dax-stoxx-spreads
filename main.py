import os
import sys
from datetime import date, timedelta

import numpy as np
import yfinance as yf
from dotenv import load_dotenv
from openai import OpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from scipy.stats import norm

load_dotenv()
sys.stdout.reconfigure(encoding='utf-8')
console = Console(force_terminal=True)
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")

RISK_FREE_RATE = 0.025
DAX_MULTIPLIER = 5
STOXX_MULTIPLIER = 10

INDICES = {
    "DAX 40": {
        "price_ticker": "^GDAXI",
        "alt_iv_tickers": ["^V1XI", "^VDAX", "VDAX=F", "VDAX-BILI"],
        "multiplier": DAX_MULTIPLIER,
        "strike_interval": 50,
        "strike_range": 0.06,
        "ticker_prefix": "ODAX",
    },
    "EURO STOXX 50": {
        "price_ticker": "^STOXX50E",
        "alt_iv_tickers": ["^V2TX", "^VSTOXX", "VSTOXX=F"],
        "multiplier": STOXX_MULTIPLIER,
        "strike_interval": 25,
        "strike_range": 0.06,
        "ticker_prefix": "OESX",
    },
}


def next_weekly_expiry() -> date:
    today = date.today()
    weekday = today.weekday()
    days_to_friday = (4 - weekday) % 7
    if days_to_friday == 0:
        days_to_friday = 7
    return today + timedelta(days=days_to_friday)


def friday_week_of_month(d: date) -> int:
    """Return which week-of-month (1-5) this Friday belongs to."""
    month_start = d.replace(day=1)
    friday_count = 1
    current = month_start
    while current <= d:
        if current.weekday() == 4:
            if current == d:
                return friday_count
            friday_count += 1
        current += timedelta(days=1)
    return friday_count


def contract_ticker(name: str, expiry: date) -> str:
    """Map index name + expiry → broker ticker (ODAX/OESX)."""
    prefix = "ODAX" if name == "DAX 40" else "OESX"
    wk = friday_week_of_month(expiry)
    if wk == 3:
        return prefix  # mensual, sin número
    return f"{prefix}{wk}"


def fetch_market_data(name: str, cfg: dict) -> dict:
    price_ticker = yf.Ticker(cfg["price_ticker"])
    info = price_ticker.info
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if not price:
        raise ValueError(f"No price for {name}")
    iv = None
    alt_tickers = cfg.get("alt_iv_tickers", [])
    for ticker in alt_tickers:
        try:
            t = yf.Ticker(ticker)
            ti = t.info
            iv = ti.get("regularMarketPrice") or ti.get("currentPrice") or ti.get("previousClose")
            if iv:
                iv = float(iv) / 100.0
                break
        except Exception:
            continue
    return {"name": name, "price": float(price), "iv_pct": iv}


def calculate_greeks(
    S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE, q: float = 0.0
) -> dict:
    if T <= 0 or sigma <= 0:
        return None
    d1 = (np.log(S / K) + (r - q + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    call_delta = np.exp(-q * T) * norm.cdf(d1)
    put_delta = -np.exp(-q * T) * norm.cdf(-d1)
    gamma = np.exp(-q * T) * norm.pdf(d1) / (S * sigma * np.sqrt(T))
    theta_call = (
        -(S * sigma * np.exp(-q * T) * norm.pdf(d1)) / (2 * np.sqrt(T))
        - r * K * np.exp(-r * T) * norm.cdf(d2)
        + q * S * np.exp(-q * T) * norm.cdf(d1)
    ) / 365.0
    theta_put = (
        -(S * sigma * np.exp(-q * T) * norm.pdf(d1)) / (2 * np.sqrt(T))
        + r * K * np.exp(-r * T) * norm.cdf(-d2)
        - q * S * np.exp(-q * T) * norm.cdf(-d1)
    ) / 365.0
    vega = S * np.exp(-q * T) * norm.pdf(d1) * np.sqrt(T) / 100.0

    return {
        "strike": round(float(K), 1),
        "call_delta": round(float(call_delta), 4),
        "put_delta": round(float(put_delta), 4),
        "gamma": round(float(gamma), 6),
        "theta_call": round(float(theta_call), 4),
        "theta_put": round(float(theta_put), 4),
        "vega": round(float(vega), 4),
    }


def generate_strikes(price: float, interval: float, range_pct: float) -> list[float]:
    low = price * (1 - range_pct)
    high = price * (1 + range_pct)
    nearest = round(price / interval) * interval
    strikes = []
    k = nearest
    while k >= low:
        strikes.append(k)
        k -= interval
    k = nearest + interval
    while k <= high:
        strikes.append(k)
        k += interval
    return sorted(strikes)


def build_chain(name: str, price: float, iv_pct: float, dte: int, cfg: dict, expiry: date) -> dict:
    T = max(dte, 1) / 365.0
    sigma = max(iv_pct, 0.05)
    strikes = generate_strikes(price, cfg["strike_interval"], cfg["strike_range"])
    options = []
    for k in strikes:
        g = calculate_greeks(price, k, T, sigma)
        if g:
            options.append(g)
    expected_move_1sd = price * sigma * np.sqrt(T)
    return {
        "name": name,
        "contract": contract_ticker(name, expiry),
        "price": round(price, 1),
        "iv_pct": round(sigma * 100, 1),
        "dte": dte,
        "expiry": expiry.isoformat(),
        "multiplier": cfg["multiplier"],
        "strike_interval": cfg["strike_interval"],
        "expected_move_1sd": round(expected_move_1sd, 1),
        "rally_scenario": round(price + expected_move_1sd, 1),
        "drop_scenario": round(price - expected_move_1sd, 1),
        "options": options,
    }


def print_summary(chain: dict):
    t = Table(title=f"{chain['name']} — {chain['contract']} ({chain['dte']} DTE)")
    t.add_column("Parámetro", style="cyan")
    t.add_column("Valor", style="green")
    t.add_row("Contrato", chain["contract"])
    t.add_row("Vencimiento", chain["expiry"])
    t.add_row("Precio subyacente", f"{chain['price']:,.1f}")
    t.add_row("IV ATM", f"{chain['iv_pct']}%")
    t.add_row("Multiplicador", f"{chain['multiplier']}€/punto")
    t.add_row("Expected Move (±1σ)", f"±{chain['expected_move_1sd']:,.1f}")
    t.add_row("Rango esperado", f"{chain['drop_scenario']:,.1f} — {chain['rally_scenario']:,.1f}")
    console.print(t)

    if chain["options"]:
        g_table = Table(title=f"Greeks por strike (rejilla sintética)")
        g_table.add_column("Strike", style="cyan")
        g_table.add_column("Call Δ", style="green")
        g_table.add_column("Put Δ", style="red")
        g_table.add_column("Gamma", style="yellow")
        g_table.add_column("Θ Call", style="magenta")
        g_table.add_column("Θ Put", style="magenta")
        g_table.add_column("Vega", style="blue")
        atm_idx = min(range(len(chain["options"])), key=lambda i: abs(chain["options"][i]["strike"] - chain["price"]))
        start = max(0, atm_idx - 5)
        end = min(len(chain["options"]), atm_idx + 6)
        for g in chain["options"][start:end]:
            g_table.add_row(
                str(g["strike"]),
                f"{g['call_delta']:.3f}",
                f"{g['put_delta']:.3f}",
                f"{g['gamma']:.5f}",
                f"{g['theta_call']:.3f}",
                f"{g['theta_put']:.3f}",
                f"{g['vega']:.3f}",
            )
        console.print(g_table)


def build_prompt(indices_data: list) -> str:
    prompt = """Actúa como un trader profesional de opciones sobre índices europeos (EUREX).
Tu especialidad son los spreads de crédito sobre DAX 40 (ODAX) y EURO STOXX 50 (OESX)
para el vencimiento semanal más cercano.

DATOS DE MERCADO ACTUALES:
"""
    for idx in indices_data:
        prompt += f"""
--- {idx['name']} ({idx['contract']}) ---
Precio: {idx['price']:,.0f} | IV: {idx['iv_pct']}% | DTE: {idx['dte']} | Multi: {idx['multiplier']}e | EM 1sd: +/-{idx['expected_move_1sd']:,.0f}

ATM Greeks:
Strike  C_Delta  P_Delta  Gamma  Theta  Vega
"""
        atm_ix = min(range(len(idx["options"])), key=lambda i: abs(idx["options"][i]["strike"] - idx["price"]))
        opts_subset = idx["options"][max(0, atm_ix - 3):min(len(idx["options"]), atm_ix + 4)]
        for g in opts_subset:
            prompt += f"{g['strike']:>6}  {g['call_delta']:>+5.2f}  {g['put_delta']:>+5.2f}  {g['gamma']:.4f}  {g['theta_call']:>+5.0f}  {g['vega']:.2f}\n"

    prompt += """
INSTRUCCIONES:
Recomienda los 2 MEJORES spreads (crédito) para cada contrato.
Muestra: tipo, strikes, delta, crédito en puntos, max profit/loss en EUR, ROC%, POP%, breakevens.
Usa strikes cada 50 en ODAX, cada 25 en OESX.
Prioriza ROC > 10% y POP > 70%. IV >= 18% favorable para vender.
Si no hay buenas condiciones, di que esperes.
Include disclaimer.
"""
    return prompt


def query_llm(prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        return "⚠️  DEEPSEEK_API_KEY no configurada. Crea un archivo .env con tu API key."
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com", timeout=120.0)
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {
                "role": "system",
                "content": "Eres un trader de opciones europeas (EUREX). Das respuestas concisas, con tablas y datos numéricos precisos. Siempre incluyes max profit, max loss, breakevens, ROC y POP. Usas emojis. Al final incluyes disclaimer.",
            },
            {"role": "user", "content": prompt},
        ],
    )
    return response.choices[0].message.content


def main():
    if not DEEPSEEK_API_KEY:
        console.print("[red]ERROR: DEEPSEEK_API_KEY no configurada.[/red]")
        console.print("Crea un archivo [bold].env[/bold] con: [cyan]DEEPSEEK_API_KEY=sk-tu-key[/cyan]")
        sys.exit(1)

    console.print(Panel.fit(
        "[bold cyan]DAX & STOXX 50 - Spread Recomendador[/bold cyan]\n"
        "[dim]Análisis del próximo vencimiento semanal con IA[/dim]",
        border_style="cyan",
    ))

    expiry = next_weekly_expiry()
    dte = (expiry - date.today()).days
    console.print(f"\n[cyan]Vencimiento semanal:[/cyan] {expiry.isoformat()} ([yellow]{dte} DTE[/yellow])\n")

    chains = []
    for name, cfg in INDICES.items():
        with console.status(f"[cyan]Obteniendo datos de {name}...[/cyan]"):
            try:
                market = fetch_market_data(name, cfg)
            except Exception as e:
                console.print(f"[red]Error al obtener {name}: {e}[/red]")
                continue

        yahoo_price = market["price"]
        yahoo_iv = market["iv_pct"]

        console.print(f"\n[bold]{name}[/bold] — Yahoo detectó: [cyan]Precio={yahoo_price:,.1f}[/cyan] [cyan]IV={yahoo_iv*100:.1f}%[/cyan]" if yahoo_iv else f"\n[bold]{name}[/bold] — Yahoo detectó: [cyan]Precio={yahoo_price:,.1f}[/cyan] [yellow]IV no disponible[/yellow]")
        console.print(f"  [dim]Ingresa tus datos reales (Enter para usar el de Yahoo):[/dim]")

        try:
            price_input = input(f"  Precio subyacente [{yahoo_price:,.1f}]: ").strip().replace(",", "")
            price = float(price_input) if price_input else yahoo_price
        except (ValueError, EOFError):
            price = yahoo_price

        iv_str = f"{yahoo_iv*100:.1f}" if yahoo_iv else ""
        try:
            iv_input = input(f"  IV ATM % [{iv_str}]: ").strip().replace("%", "")
            iv = float(iv_input) / 100.0 if iv_input else (yahoo_iv or 0.20)
        except (ValueError, EOFError):
            iv = yahoo_iv or 0.20

        chain = build_chain(market["name"], price, iv, dte, cfg, expiry)
        chains.append(chain)
        print_summary(chain)
        console.print()

    if not chains:
        console.print("[red]No hay datos de mercado para analizar.[/red]")
        sys.exit(1)

    console.print("\n[bold magenta]── Consultando a DeepSeek V4... ──[/bold magenta]\n")
    with console.status("[cyan]Analizando spreads con IA...[/cyan]", spinner="dots"):
        prompt = build_prompt(chains)
        response = query_llm(prompt)

    console.print(Panel.fit(response, border_style="green", title="📈 Recomendaciones AI"))
    console.print()


if __name__ == "__main__":
    main()
