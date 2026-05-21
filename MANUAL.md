# DAX & STOXX 50 — Spread Recomendador

App de análisis de spreads semanales para DAX 40 (ODAX) y EURO STOXX 50 (OESX) con inteligencia artificial.

---

## 1. Instalación

```bash
# Clonar
git clone https://github.com/si01jaj/dax-stoxx-spreads.git
cd dax-stoxx-spreads

# Crear .env con API key de DeepSeek
echo "DEEPSEEK_API_KEY=sk-tu-key" > .env

# Instalar dependencias
pip install -r requirements.txt
```

---

## 2. Uso en local (CLI)

```bash
python main.py
```

**Flujo interactivo:**

```
DAX & STOXX 50 - Spread Recomendador
Vencimiento semanal: 2026-05-22 (1 DTE)

DAX 40 — Yahoo detectó: Precio=24,852.0 IV=17.9%
  Ingresa tus datos reales (Enter para usar el de Yahoo):
  Precio subyacente [24,852.0]: 24611.8       ← dato real de tu broker
  IV ATM % [17.9]: 25.6                        ← dato real de tu broker

EURO STOXX 50 — Yahoo detectó: Precio=6,001.6 IV no disponible
  Ingresa tus datos reales (Enter para usar el de Yahoo):
  Precio subyacente [6,001.6]: 5985.0          ← dato real
  IV ATM % []: 22.0                             ← dato real

── Consultando a DeepSeek V4 ──

Tras analizar los datos de ODAX (DTE 1, IV 25.6%) y OESX (DTE 1, IV 22.0%)...
```

El script muestra tablas con Greeks y luego la IA devuelve los spreads recomendados.

---

## 3. Despliegue web (VPS)

```bash
# Clonar en el VPS
cd /docker
git clone https://github.com/si01jaj/dax-stoxx-spreads.git
cd dax-stoxx-spreads

# Crear .env
echo "DEEPSEEK_API_KEY=sk-tu-key" > .env

# Build y ejecutar
docker compose up -d --build
```

La web queda en `http://<vps-ip>:8001`.

### Cloudflare Tunnel

Si usas Cloudflare Tunnel, añade en tu configuración (`/root/.cloudflared/config.yml` o desde el dashboard):

```yaml
tunnel: tu-tunnel-id
ingress:
  - hostname: optionexpert.jj-apps.com
    service: http://localhost:8000
  - hostname: dax-stoxx.jj-apps.com
    service: http://localhost:8001
  - service: http_status:404
```

---

## 4. Web UI

La interfaz web en `https://dax-stoxx.jj-apps.com` muestra:

1. **Formulario con valores de Yahoo precargados** — Precio e IV para DAX y STOXX
2. **Campos editables** — Introduces los datos reales de tu broker
3. **Botón "Analizar spreads"** — Ejecuta el análisis
4. **Resultados formateados** — Tablas, strikes, ROC, POP, breakevens

---

## 5. Contratos disponibles

| Viernes del mes | Ticker DAX | Ticker STOXX | Notas |
|----------------|-----------|-------------|-------|
| Semana 1 | `ODAX1` | `OESX1` | Semanal |
| Semana 2 | `ODAX2` | `OESX2` | Semanal |
| Semana 3 | `ODAX` | `OESX` | **Mensual** (coincide con mensual) |
| Semana 4 | `ODAX4` | `OESX4` | Semanal |
| Semana 5 | `ODAX5` | `OESX5` | Semanal (si existe) |

---

## 6. Multiplicadores

| Índice | Multiplicador |
|--------|-------------|
| DAX 40 (ODAX) | **5€/punto** |
| STOXX 50 (OESX) | **10€/punto** |

---

## 7. Cómo interpretar la respuesta de la IA

Para cada spread recomendado, la IA muestra:

| Campo | Significado |
|-------|------------|
| **Strikes** | Short/Long strikes del spread |
| **Crédito (pts)** | Prima recibida en puntos del índice |
| **Max Profit** | Ganancia máxima en € |
| **Max Loss** | Pérdida máxima en € |
| **ROC %** | Return on Capital (rentabilidad sobre margen) |
| **POP %** | Probabilidad estimada de profit |
| **Breakeven** | Precio al que el spread ni gana ni pierde |

---

## 8. Estructura del proyecto

```
dax-stoxx-spreads/
├── main.py              # Lógica principal (CLI + run_analysis exportable)
├── api.py               # FastAPI (servicio web)
├── static/
│   ├── index.html       # Web UI (formulario + resultados)
│   └── style.css        # Tema oscuro
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── .env                 # DEEPSEEK_API_KEY (NO subir a git)
```

---

## 9. Disclaimer

⚠️ Esta herramienta es para fines educativos y de análisis. No constituye consejo financiero. El trading de opciones conlleva riesgo sustancial de pérdida. Siempre haz tu propia investigación.
