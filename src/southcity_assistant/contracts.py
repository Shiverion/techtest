"""Request, model-plan and response contracts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=4000)
    user_role: str | None = Field(
        default=None, description="Sales_Staff or Management (alternative to the X-User-Role header)"
    )


class PlannedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    sql: str = Field(min_length=1, max_length=4000)


class QueryPlan(BaseModel):
    """What the model must return. Anything else is rejected (fail closed)."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["query", "refuse", "clarify"]
    queries: list[PlannedQuery] = Field(default_factory=list, max_length=3)
    answer_template: str = Field(default="", max_length=800)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _consistent(self) -> QueryPlan:
        if self.status == "query" and not self.queries:
            raise ValueError("status=query requires at least one query")
        if self.status != "query" and self.queries:
            raise ValueError("queries are only allowed with status=query")
        return self


class Section(BaseModel):
    title: str
    sql: str
    columns: list[str]
    rows: list[list[str | None]]  # display values (Rupiah, ISO dates)
    raw_rows: list[list[str | int | float | bool | None]]  # exact values; money as decimal strings
    row_count: int
    truncated: bool


class AskResponse(BaseModel):
    request_id: str
    status: Literal["ok", "refused", "error"]
    role: str | None = None
    code: str | None = None  # stable machine-readable reason for refused/error
    answer: str
    sections: list[Section] = Field(default_factory=list)
