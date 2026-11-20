from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml
from agent_loop import Action, AgentTrace, ToolRegistry, run_agent
from cache_ratelimit import CachedProvider, RateLimiter, RealClock, ThrottledProvider
from llm_client import CompletionRequest, CompletionResult, LlmClient, ReplayProvider, Span
from llm_client.providers.opencode_cli import OpenCodeCLI
from parser_io import parse
from schema_validate import SchemaRegistry
from secure_base import sanitize_for_prompt
from test_kit import EvalCase, EvalDataset, run

from .models import ChecklistReport

DEFAULT_MODEL = "opencode/big-pickle"
SCHEMA_ID = "checklist-report-v1"
PROMPT_ID = "checklist-report-generator"
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT = ROOT / "prompts" / "checklist-report-generator.md"
DEFAULT_DATASET = ROOT / "evals" / "checklist-report-generator.jsonl"
DEFAULT_CHECKLIST = ROOT / "checklist.md"
ITEM_RE = re.compile(r"^\s*-\s+\[( |x)\]\s+(.+)$")


# --------------------------------------------------------------------------
# checklist → ítems (parser-io para grupos; los ítems son líneas del cuerpo)
# --------------------------------------------------------------------------
def parse_checklist(path: Path) -> list[dict]:
    doc = parse(path)
    items: list[dict] = []
    for section in doc.sections:
        for line in section.body.splitlines():
            m = ITEM_RE.match(line)
            if not m:
                continue
            declaracion = m.group(2).strip()
            op, _, resto = declaracion.partition(":")
            items.append({"grupo": section.title, "decl": declaracion, "op": op.strip(), "args": resto.strip()})
    return items


def build_registry(repo: Path) -> ToolRegistry:
    """Solo lectura: el agente de esta semana no escribe nada (ADR-11 §2)."""
    registry = ToolRegistry()

    def _exists(args: dict) -> str:
        return "si" if (repo / args["path"]).exists() else "no"

    def _contains(args: dict) -> str:
        target = repo / args["path"]
        if not target.is_file():
            return "-1"
        return str(target.read_text(encoding="utf-8", errors="replace").count(args["text"]))

    registry.register("exists", "¿existe la ruta relativa al repo? args: {path}", _exists)
    registry.register("contains", "¿cuántas veces aparece el texto en el archivo? args: {path, text}", _contains)
    return registry


def brain_planificador(items: list[dict]):
    """Cerebro por guion: una acción de lectura por ítem y termina. D4 puro."""
    cola: list[Action] = []
    for item in items:
        if item["op"] == "existe":
            cola.append(Action(razon=item["decl"], tool="exists", args={"path": item["args"]}))
        elif item["op"] == "contiene":
            path, _, texto = item["args"].partition("::")
            cola.append(Action(razon=item["decl"], tool="contains", args={"path": path.strip(), "text": texto.strip()}))
    estado = {"i": 0}

    def brain(_vista: dict) -> Action:
        if estado["i"] < len(cola):
            accion = cola[estado["i"]]
            estado["i"] += 1
            return accion
        return Action(final="checklist caminado")

    return brain


def walk(repo: Path, checklist_path: Path, *, confirm: Callable | None = None, max_steps: int = 64) -> tuple[AgentTrace, list[dict]]:
    """Camina el checklist y devuelve (trace, resultados). Cada resultado sale de la
    salida MEDIDA de la herramienta, no de la opinión del agente."""
    items = parse_checklist(checklist_path)
    trace = run_agent(
        f"checklist de {repo.name}",
        registry=build_registry(repo),
        brain=brain_planificador(items),
        confirm=confirm,
        max_steps=max_steps,
    )
    ejecutados = [s for s in trace.steps if s.estado == "ok"]
    resultados: list[dict] = []
    for item, step in zip(items, ejecutados, strict=False):
        ok = (step.salida == "si") if item["op"] == "existe" else (step.salida not in {"0", "-1"})
        resultados.append({"item": item["decl"], "grupo": item["grupo"], "ok": ok, "detalle": step.salida})
    for item in items[len(resultados):]:  # pasos que no llegaron a correr (rechazo/presupuesto)
        resultados.append({"item": item["decl"], "grupo": item["grupo"], "ok": False, "detalle": "no ejecutado"})
    return trace, resultados


def results_text(resultados: list[dict]) -> str:
    if not resultados:
        return "(checklist vacío: sin ítems)"
    return "\n".join(
        f"- {r['item']}: {'ok' if r['ok'] else 'FALLÓ'} ({r['detalle']})" for r in resultados
    )


# --------------------------------------------------------------------------
# reporte LLM validado
# --------------------------------------------------------------------------
def load_prompt(path: Path) -> tuple[str, str, str, Path]:
    text = path.read_text()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: falta frontmatter")
    _, frontmatter, body = text.split("---", 2)
    data = yaml.safe_load(frontmatter)
    eval_path = ROOT / data["eval"] if not Path(data["eval"]).is_absolute() else Path(data["eval"])
    return data["id"], data["version"], body.strip(), eval_path


def render_report(prompt_id: str, prompt_version: str, variables: dict) -> list[dict]:
    if prompt_id == PROMPT_ID:
        _, _, body, _ = load_prompt(DEFAULT_PROMPT)
        system_part = body.split("## Sistema\n", 1)[1].split("## Usuario\n", 1)[0].strip()
        user_part = body.split("## Usuario\n", 1)[1].strip().format(**variables)
        return [{"role": "system", "content": system_part}, {"role": "user", "content": user_part}]
    payload = {"prompt_id": prompt_id, "prompt_version": prompt_version, "variables": variables}
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_emitter(span_file: Path | None):
    def emit(span: Span, _result) -> None:
        if span_file is not None:
            with span_file.open("a") as handle:
                handle.write(span.as_jsonl() + "\n")
        else:
            sys.stderr.write(span.as_jsonl() + "\n")

    return emit


def build_client(inner_provider, *, clock=None, span_file: Path | None = None) -> dict:
    clock = RealClock() if clock is None else clock
    cached = CachedProvider(inner_provider, ttl_seconds=3600.0, clock=clock)
    limiter = RateLimiter(rpm=120, burst=20, clock=clock)
    provider = ThrottledProvider(cached, limiter)
    registry = SchemaRegistry()
    registry.register(SCHEMA_ID, ChecklistReport)
    client = LlmClient(
        provider,
        consumer_repo="checklist-walker",
        model_aliases={"fast": DEFAULT_MODEL},
        renderer=render_report,
        validator=registry.make_validator(SCHEMA_ID),
        emitter=build_emitter(span_file),
    )
    return {"client": client, "cached": cached, "limiter": limiter}


def report(client: LlmClient, repo_name: str, resultados: list[dict]):
    texto = sanitize_for_prompt(results_text(resultados)).text.replace("{", "(").replace("}", ")")
    result = client.complete(
        CompletionRequest(
            prompt_id=PROMPT_ID,
            prompt_version="0.1.0",
            variables={"repo": repo_name, "results": texto},
            model_alias="fast",
            response_schema=SCHEMA_ID,
            tags=["checklist-walker", "week-21"],
        )
    )
    if not result.validation.ok:
        raise RuntimeError("; ".join(result.validation.errors))
    return result.parsed


def judge_for(client: LlmClient) -> Callable[[EvalCase], CompletionResult]:
    def judge(case: EvalCase) -> CompletionResult:
        return client.complete(
            CompletionRequest(
                prompt_id=case.prompt_id,
                prompt_version=case.prompt_version,
                variables=case.input,
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["checklist-walker", "week-21"],
            )
        )

    return judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="checklist-walker", description="Un agente camina un checklist sobre un repo (semana 21).")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "record", "replay-check"])
    parser.add_argument("--repo", default="../triage-bot", help="repo a caminar")
    parser.add_argument("--checklist", default=str(DEFAULT_CHECKLIST), help="checklist markdown con ítems existe:/contiene:")
    parser.add_argument("--gate", action="store_true", help="puerta humana: pide confirmación por teclado en cada paso")
    args = parser.parse_args(argv)

    if args.command == "record":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
        out = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"grabadas {len(dataset.cases)} respuestas; pass={out.passed}/{out.total}")
        return 0 if out.threshold_ok else 1

    if args.command == "replay-check":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=False)
        out = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"replay: pass={out.passed}/{out.total} threshold_ok={out.threshold_ok}")
        return 0 if out.threshold_ok else 1

    def gate(action: Action) -> bool:
        respuesta = input(f"¿ejecutar {action.tool} {action.args}? [s/N] ").strip().lower()
        return respuesta == "s"

    trace, resultados = walk(
        Path(args.repo), Path(args.checklist),
        confirm=gate if args.gate else None,
    )
    salida = {"repo": Path(args.repo).name, "paro": trace.paro, "pasos": len(trace.steps), "resultados": resultados}
    if trace.paro == "done":
        card = report(build_client(OpenCodeCLI(DEFAULT_MODEL))["client"], Path(args.repo).name, resultados)
        salida["reporte"] = card.model_dump()
    print(json.dumps(salida, ensure_ascii=False, indent=2))
    return 0 if trace.paro == "done" and all(r["ok"] for r in resultados) else 1


if __name__ == "__main__":
    raise SystemExit(main())
