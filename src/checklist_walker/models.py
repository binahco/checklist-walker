from __future__ import annotations

from pydantic import BaseModel


class ChecklistReport(BaseModel):
    """Reporte del walk (`checklist-report-v1`)."""

    repo: str
    passed: int
    failed: int
    summary: str
    watch: list[str] = []
