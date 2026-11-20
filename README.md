# checklist-walker

Un agente camina un checklist sobre un repo (semana 21): cada ítem (`existe:RUTA`,
`contiene:RUTA::TEXTO`) se ejecuta con herramientas de lectura, el resultado se
**mide** (no se opina), y un LLM validado redacta el reporte final. Nace con el módulo
nuevo del core: **`agent-loop` (C13, ADR-11)** — el loop mínimo con puerta humana.

El cerebro del agente es un **guion determinista** (una acción por ítem): el walk es
idéntico en cada corrida (D4) y el único LLM es el reporte. La autonomía llega cuando
el cerebro sea un cierre sobre `LlmClient` — el loop, la puerta y el trace ya están.

## Demo

```bash
uv sync
uv run checklist-walker run --repo ../triage-bot
# {
#   "repo": "triage-bot", "paro": "done", "pasos": 6,
#   "resultados": [
#     {"item": "existe:README.md", "ok": true, "detalle": "si"},
#     ...
#   ],
#   "reporte": {"passed": 5, "failed": 0, "summary": "…", "watch": []}
# }
```

Con puerta humana (`--gate`), cada paso pide confirmación por teclado y un "no"
termina con `paro: "rechazado"` — el agente no insiste (ADR-11 §2).

## Cómo funciona

```
checklist-walker run
  ├─ parser-io: checklist.md → grupos + ítems (existe:/contiene:)
  ├─ agent-loop: cerebro por guion → Action(exists/contains) por ítem
  │    ├─ confirm (opcional): puerta antes de cada herramienta
  │    └─ trace: cada paso con acción, estado y salida — la corrida completa
  ├─ resultados: [x]/[ ] salen de la salida MEDIDA de la herramienta
  └─ reporte LLM validado (checklist-report-v1) sobre los resultados saneados
```

- Las herramientas son **solo lectura** (`exists`, `contains`): esta semana el agente
  no escribe nada — deshacer no es de esta capa (ADR-11 §3).
- Un ítem rojo no corta el walk: todos corren y el rojo queda `FALLÓ` con su detalle.
- Si el loop termina por rechazo o presupuesto, los ítems no ejecutados quedan como
  `no ejecutado` — visibles, no descartados.

## Recicla de

| Módulo | Uso |
|---|---|
| `agent-loop` | seed (C13): el loop, la puerta humana, el presupuesto y el trace |
| `parser-io` | el checklist se lee en grupos e ítems con líneas reales |
| `llm-client` | la llamada del reporte (retry + reparación + span de 20 campos) |
| `schema-validate` | el reporte valida contra `checklist-report-v1` o falla |
| `secure-base` | los resultados que entran al prompt van saneados |
| `test-kit` | el prompt nace evaluado: dataset congelado + replay (D4) |
| `cache-ratelimit` | caché TTL + token bucket alrededor del proveedor |

## Limitaciones

- Dos operadores (`existe`, `contiene`); el checklist es un mini-DSL, no un lenguaje.
- El cerebro es un guion: no decide rutas alternativas cuando un ítem falla — cambia
  cuando el cerebro sea LLM (y entonces el replay del walk deja de ser determinista,
  por eso hoy no lo es).
- Sin marcado in place del archivo: los resultados van a stdout/JSON; reescribir el
  checklist queda para el segundo consumidor.

## Roadmap

- [x] Walk con agent-loop + puerta humana + reporte validado (sem. 21)
- [x] Seed de `agent-loop` (C13) con ADR-11
- [ ] Cerebro LLM: decidir el orden y reintentar ítems fallidos (con confirmación)
- [ ] Marcado in place del checklist y PR con el diff (cuando haya `gh-app`, sem. 27)
