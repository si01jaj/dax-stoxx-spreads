# Tests del motor anterior (v1) — no se ejecutan

`_test_engine_v1.py` y `_test_api_v1.py` se escribieron contra la primera
versión del proyecto, donde `main.py` era un motor monolítico con
`get_expiry()`, `calculate_greeks()`, `generate_strikes()` y `build_chain()`.

El código que corre en producción (importado a este repo para que deje de
vivir solo en `/docker`) es una reescritura con otra separación:

| v1 | v2 |
|---|---|
| `main.get_expiry()` | `eurex_calendar.select_contract()` |
| `main.calculate_greeks()` | `pricing.greeks()` |
| `main.generate_strikes()` | `pricing.strike_grid()` |
| `main.build_chain()` | `structures.build_candidates()` |
| `main.friday_week_of_month()` | `eurex_calendar.weekly_expiries()` |

Se conservan aquí con prefijo `_` para que pytest no los recoja: documentan
qué comportamiento se consideraba correcto, pero ya no aplican. La cobertura
del motor actual está en `tests/test_pricing.py`, `tests/test_calendar.py`,
`tests/test_structures.py` y `tests/test_api.py`.

Si algún día se recupera algo de aquí, muévelo de vuelta a `tests/` y
adáptalo a la API nueva.
