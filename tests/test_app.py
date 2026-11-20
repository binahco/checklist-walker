from __future__ import annotations

import json
from pathlib import Path

from llm_client.provider import ProviderRequest, ProviderResponse
from test_kit import EvalDataset, run as run_evalset

from checklist_walker.main import (
    DEFAULT_CHECKLIST,
    DEFAULT_DATASET,
    build_client,
    judge_for,
    parse_checklist,
    report,
    walk,
)

STUB_TEXT = json.dumps(
    {"repo": "demo", "passed": 3, "failed": 0, "summary": "todo en verde", "watch": []},
    ensure_ascii=False,
)


class StubProvider:
    name = "stub"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or STUB_TEXT
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        return ProviderResponse(text=self.text, model=request.model)

    def stream(self, request: ProviderRequest) -> list[str]:
        self.calls += 1
        return [self.text]


def mini_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    (repo / "prompts").mkdir(parents=True)
    (repo / "README.md").write_text("# demo\n\n## Recicla de\n\nalgo\n")
    (repo / "core-consumer.yml").write_text("week: 1\nname: demo\nreused_modules: [llm-client]\nestimated_new_surface: 20\n")
    return repo


def test_el_checklist_se_parsea_con_grupos_e_items() -> None:
    items = parse_checklist(DEFAULT_CHECKLIST)
    assert [i["op"] for i in items] == ["existe", "existe", "existe", "contiene", "contiene"]
    assert items[3]["args"] == "README.md::Recicla de"
    assert {i["grupo"] for i in items} == {"Estructura", "Contrato"}


def test_el_walk_camina_y_mide_sobre_un_repo_real(tmp_path) -> None:
    trace, resultados = walk(mini_repo(tmp_path), DEFAULT_CHECKLIST)
    assert trace.paro == "done"
    assert all(r["ok"] for r in resultados), resultados
    assert len(trace.steps) == 6  # 5 herramientas + final


def test_un_item_rojo_queda_como_fallo_visible(tmp_path) -> None:
    repo = mini_repo(tmp_path)
    (repo / "core-consumer.yml").unlink()  # rompe un ítem de estructura
    trace, resultados = walk(repo, DEFAULT_CHECKLIST)
    roto = next(r for r in resultados if "core-consumer.yml" in r["item"])
    assert roto["ok"] is False and roto["detalle"] == "no"
    verdes = [r for r in resultados if r["ok"]]
    assert verdes, "los demás ítems sí corrieron"


def test_la_presa_de_confirmacion_termina_sin_inistir(tmp_path) -> None:
    trace, _ = walk(mini_repo(tmp_path), DEFAULT_CHECKLIST, confirm=lambda a: False)
    assert trace.paro == "rechazado"
    assert trace.steps[0].estado == "rechazado"


def test_el_reporte_valida_y_no_recibe_secretos(tmp_path) -> None:
    repo = mini_repo(tmp_path)
    (repo / "README.md").write_text("# demo\n\ntoken ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456\n")
    _, resultados = walk(repo, DEFAULT_CHECKLIST)
    stub = StubProvider()
    report(build_client(stub)["client"], repo.name, resultados)
    mensaje = json.dumps([m["content"] for m in stub.last_request.messages], ensure_ascii=False)
    assert "ghp_" not in mensaje, "un secreto del repo entró al prompt del reporte"


def test_el_judge_evoluciona_el_dataset_completo(tmp_path) -> None:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    client = build_client(StubProvider())["client"]
    out = run_evalset(dataset, judge_for(client), mode="full", threshold=1.0)
    assert out.total == 3
    assert out.passed == 3
