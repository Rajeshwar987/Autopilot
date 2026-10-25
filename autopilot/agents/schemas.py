"""Structured objects handed between the harness and the agents."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Diagnosis(BaseModel):
    fault_type: str = Field(description="short snake_case label, e.g. unit_change, positional_shift, "
                                        "timestamp_unit, null_flood, duplicate_keys, stale_source, unknown")
    affected_table: str
    affected_columns: list[str]
    root_cause: str = Field(description="one or two sentences, grounded in tool evidence")
    evidence: list[str] = Field(description="specific numbers/observations from tool outputs")
    confidence: float = Field(ge=0.0, le=1.0)
    fixable: bool = Field(description="true if a SQL change to a silver model can correct the data")
    suggested_fix_strategy: str = ""

    @field_validator("affected_columns", "evidence", mode="before")
    @classmethod
    def _coerce_list(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


class CheckOutcome(BaseModel):
    check_name: str
    status: str
    failed_rows: int | None = None
    total_rows: int | None = None


class FixProposal(BaseModel):
    proposal_id: str
    incident_id: str
    model_name: str
    old_sql: str
    new_sql: str
    backfill_sql: str = ""
    rollback_sql: str = ""
    rationale: str
    sandbox_checks: list[CheckOutcome] = []
    approved: bool | None = None
