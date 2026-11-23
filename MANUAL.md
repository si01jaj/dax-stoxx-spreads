# Eurex — Estructuras de crédito ODAX / OESX

Recomendador de estructuras de crédito sobre **DAX 40 (ODAX)** y **EURO STOXX 50 (OESX)**.
Todo el cálculo es determinista y verificable; el LLM (opcional) solo redacta sobre
números ya calculados.

> v2.0 — reescritura del motor. Ver [Cambios frente a la v1](#cambios-frente-a-la-v1).

---

## 1. Qué calcula

Para cada índice y vencimiento:

1. **Calendario real de Eurex**: festivos oficiales, mensual = tercer viernes con
   *rollback* al día hábil anterior, semanales por semana del mes, hora exacta de
   vencimiento (ODAX 13:00 CET, OESX 12:00 CET) y DTE en fracción de día.
2. **Primas teóricas** con Black-Scholes sobre el forward, con **superficie de
   volatilidad** (nivel ATM + pendiente de skew) y **dividendo** (0% en el DAX, que es
   índice de rendimiento total; ~3% en el EURO STOXX 50, que es índice de precios).
3. **Estructuras construidas por delta objetivo**, no por rejilla: bull put, bear call
   e iron condor, con anchos configurables en ticks (50 pts en ODAX, 25 en OESX).
4. **Métricas deterministas**: crédito, máx. beneficio, máx. pérdida, breakevens,
   POP implícita y real, valor esperado y ROC sobre el riesgo del spread.
5. **Escenarios** de P&L a vencimiento en ±1σ y ±2σ.

### POP y EV: qué son exactamente

| Métrica | Definición |
|---|---|
| `pop_rn` | Probabilidad *que descuenta el mercado*: 1 − \|Δ\| de la pata corta |
| `pop` | P(beneficio a vencimiento) integrando el payoff con **σ realizada** = σ ATM × `realized_vol_factor` (0,90 por defecto) |
| `ev_pts` | ∫ payoff · densidad lognormal con deriva de forward (r−q). **No se asume dirección**, solo que la vol realizada difiere de la implícita |
| `roc` | crédito / máx pérdida del spread (no sobre margen PRMS de Eurex) |
| `score` | EV / máx pérdida; ordena los candidatos |

Con `realized_vol_factor = 1.0` el EV vuelve a ≈0: es la comprobación de que el
modelo no regala ventaja. La ventaja, si existe, viene del diferencial entre vol
implícita y realizada.

---

## 2. Uso en local (CLI)

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# La IV se copia de tu bróker, en % tal cual (25.6) o en fracción (0.256)
python main.py --expiry semanal --iv DAX=25.6 --iv STOXX=22.0 --no-llm

# Precio a mano también (entonces solo se usa el calendario, cero red)
python main.py --expiry 2m --spot DAX=25000 --spot STOXX=6000 \
               --iv DAX=24 --iv STOXX=21 --no-llm

# Salida completa en JSON (para guardar, comparar o procesar)
python main.py --expiry 3m --iv DAX=23.5 --json > analisis.json

# Filtros
python main.py --min-pop 0.75 --max-width 4
```

Opciones: `--index DAX,STOXX`, `--expiry semanal|mensual|2m|3m`,
`--spot DAX=25000`, `--iv DAX=25.6`, `--no-llm`, `--json`, `--min-pop`, `--max-width`.

Sin `DEEPSEEK_API_KEY` la herramienta funciona igual: muestra la tabla y avisa de que
no hay redacción.

### Datos manuales (modo por defecto, sin feed)

No hay ninguna fuente de opciones conectada, y no hace falta: se introducen dos datos
por índice.

| Dato | De dónde sale | Nota |
|---|---|---|
| Precio del subyacente | bróker o Yahoo (precarga) | `SPOT_PROVIDER=yahoo` para precargar, `manual` para no salir a internet |
| **IV ATM del vencimiento elegido** | pantalla de opciones del bróker, serie ATM | es la del vencimiento que vas a operar, no el VDAX a 30 días |

Con `IV_PROVIDER=none` (por defecto) la aplicación **no intenta** buscar volatilidad en
Yahoo: ni peticiones, ni esperas, ni el 20% silencioso de la v1. Si falta la IV, la API
responde 422 con el motivo y la CLI dice qué índice falta.

Calidad del resultado = calidad de esos dos datos. El resto (calendario, primas,
skew, POP, EV, ROC, escenarios) se calcula aquí y es reproducible.

---

## 3. Web

```bash
uvicorn api:app --host 127.0.0.1 --port 8000
```

- `GET  /api/health` → estado y versión
- `GET  /api/defaults?expiry_type=semanal` → precios para precargar el formulario
- `POST /api/analyze` → tabla completa + redacción opcional
- `POST /api/chat` → preguntas sobre el análisis de la sesión (`session_id`)
- `POST /api/chat/reset?session_id=…`

Ejemplo:

```bash
curl -s -X POST http://127.0.0.1:8000/api/analyze \
  -H 'Content-Type: application/json' \
  -d '{"expiry_type":"semanal","dax_price":25000,"dax_iv":0.256,
       "stoxx_price":6000,"stoxx_iv":0.22,"narrative":false}'
```

Validación: `dax_price > 0`, `iv ∈ (0, 3]`, `expiry_type` enumerado, índices no vacíos.
Si no hay IV y `ALLOW_IV_FALLBACK=0`, la API responde **422 con el motivo** en vez de
usar un 20% silencioso.

### Despliegue (estándar del host: loopback + puerto fijo)

```bash
cd /docker/dax-stoxx-spreads
docker compose up -d --build
```

Publica `127.0.0.1:8001` → contenedor `dax-stoxx-spreads:8000`, así que cloudflared
llega por `http://127.0.0.1:8001` sin abrir puertos en el cortafuegos.

---

## 4. Configuración (`.env`)

| Variable | Por defecto | Efecto |
|---|---|---|
| `DEEPSEEK_API_KEY` | — | sin ella no hay redacción (la tabla funciona igual) |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_TIMEOUT` | `deepseek-chat` / api.deepseek.com / 90 | proveedor del LLM |
| `IV_PROVIDER` | `none` | `none` = IV manual, sin red; `yahoo` = intentarla (hoy 404) |
| `SPOT_PROVIDER` | `yahoo` | `yahoo` precarga el contado; `manual` no sale a internet |
| `VOL_SKEW` | `0.0217` | pendiente de skew (vol absoluta por unidad de z) |
| `RISK_FREE_RATE` | `0.025` | tipo sin riesgo para el forward |
| `ALLOW_IV_FALLBACK` | `0` | `1` = usar `DEFAULT_IV` marcándolo como **ASUMIDA** |
| `DEFAULT_IV` | `0.20` | solo si el fallback está activo |
| `APP_TOKEN` | — | si se define, `/api/*` exige cabecera `X-App-Token` |
| `CORS_ORIGINS` | — | lista separada por comas; vacío = sin CORS |
| `MARKET_CACHE_TTL` | `60` | caché de datos de mercado (s) |

---

## 5. Contratos y factores

| Viernes del mes | DAX | STOXX | Notas |
|---|---|---|---|
| Semana 1 | `ODAX1` | `OESX1` | |
| Semana 2 | `ODAX2` | `OESX2` | |
| Semana 3 | `ODAX` | `OESX` | mensual |
| Semana 4 | `ODAX4` | `OESX4` | |
| Semana 5 | `ODAX5` | `OESX5` | solo si existe |

| Índice | Multiplicador | Tick de strike | Dividendo |
|---|---|---|---|
| DAX 40 (ODAX) | 5 €/punto | 50 | 0% (índice de rendimiento total) |
| EURO STOXX 50 (OESX) | 10 €/punto | 25 | ~3% (índice de precios) |

Festivos usados (regla, ampliable en `HOLIDAY_OVERRIDES`): 1 de enero, Viernes Santo,
Lunes de Pascua, 1 de mayo, 24, 25 y 31 de diciembre.

---

## 6. Tests

```bash
pip install pytest httpx
python -m pytest -q
```

47 tests: festivos y rollback del mensual, regresión del 25/12 y del Viernes Santo,
DTE con hora, paridad put-call, delta/vega, IV ida y vuelta, skew, aritmética de
spreads (max pérdida, breakeven, payoff en extremos), EV≈0 sin prima de vol,
diversidad del ranking, validación de la API, auth por token y acotado de sesiones.

---

## 7. Límites conocidos (honestos)

1. **No hay cadena de opciones real.** Las primas son teóricas: no hay bid/ask, ni
   open interest, ni volumen, ni IV por strike. La superficie es paramétrica (ATM +
   skew lineal). Para operar de verdad hace falta una fuente de datos de Eurex
   (IBKR, Eurex MD+S EOD, o export del bróker).
2. **El margen es una estimación** (máx pérdida del spread). Eurex liquida con PRMS
   por cartera: el capital real puede ser menor.
3. **Sin comisiones ni horquilla.** En alas semanales la horquilla puede ser una parte
   grande de la prima teórica.
4. **No hay calendarios** (diagonal/calendar spread): requieren estructura temporal de
   volatilidad, que hoy no se modela.
5. **La IV de mercado no está automatizada**: Yahoo dejó de servir VDAX/VSTOXX. La app
   lo dice explícitamente y pide la IV del bróker.
6. **No es asesoramiento financiero.**

---

## Cambios frente a la v1

| v1 | v2 |
|---|---|
| Rejilla de strikes ±6% con IV plana ATM | Estructuras por delta objetivo con superficie ATM + skew |
| Sin primas: el LLM inventaba crédito, POP, ROC y BE | Primas, POP, EV, ROC y BE calculados en Python |
| Solo se enviaban 7 strikes al LLM; las alas eran inventadas | El LLM recibe la tabla ya calculada y solo redacta (JSON validado) |
| IV de Yahoo con fallback silencioso a 20% | Sin IV → error explícito; fallback opcional y marcado como ASUMIDA |
| Sin festivos: proponía el 25/12 y el Viernes Santo | Calendario Eurex con rollback al día hábil anterior |
| DTE en días enteros; `T=max(dte,1)/365` | DTE con hora de vencimiento (13:00/12:00 CET) |
| Sin dividendo (q=0 en STOXX) | q=3% en OESX |
| `async def` con I/O bloqueante | Endpoints síncronos en threadpool (sin bloqueo del event loop) |
| CORS `*`, sin auth, sin validación | CORS opcional, `APP_TOKEN`, Pydantic con límites |
| Estado de chat global y creciente | Sesiones por `session_id` con TTL y tope de mensajes |
| Compose publicando en `0.0.0.0` | Loopback `127.0.0.1:8001`, imagen y deps fijadas, usuario no root, healthcheck |
| 0 tests | 47 tests |
