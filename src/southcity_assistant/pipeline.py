"""Request orchestration: guard -> scope check -> entities -> plan -> policy -> execute -> render.

Only a ValidatedQuery produced by the SQL policy ever reaches the database. One repair round is allowed
for SQL Server syntax/name errors; policy rejections, permission errors and timeouts are final.
"""

from __future__ import annotations

import time
from typing import Literal

from .audit import audit, sql_fingerprint
from .contracts import AskResponse, QueryPlan
from .db import Database, QueryError
from .entities import check_filters, resolve
from .llm import LLMError, Planner
from .rendering import build_section, model_text_ok, render_answer
from .security.input_guard import screen_question
from .security.roles import ROLE_TABLES, Role, restricted_topic
from .security.sql_policy import PolicyViolation, ValidatedQuery, validate
from .settings import Settings


class Stop(Exception):
    """Ends a request early with a refused/error/clarify response."""

    def __init__(self, status: Literal["ok", "refused", "error"], code: str, answer: str):
        super().__init__(answer)
        self.status = status
        self.code = code
        self.answer = answer


class Assistant:
    def __init__(self, settings: Settings, db: Database, planner: Planner):
        self.settings = settings
        self.db = db
        self.planner = planner

    def ask(self, question: str, role: Role, request_id: str) -> AskResponse:
        started = time.monotonic()
        try:
            response = self._answer(question, role, request_id)
        except Stop as stop:
            response = AskResponse(
                request_id=request_id, status=stop.status, role=role.value, code=stop.code, answer=stop.answer
            )
        audit(
            "ask",
            request_id=request_id,
            role=role.value,
            status=response.status,
            code=response.code,
            question_chars=len(question),
            sql_fingerprints=[sql_fingerprint(s.sql) for s in response.sections],
            latency_ms=round((time.monotonic() - started) * 1000),
        )
        return response

    def _answer(self, question: str, role: Role, request_id: str) -> AskResponse:
        deadline = time.monotonic() + self.settings.request_deadline_s  # per request, never shared
        guard = screen_question(question)
        if guard.injection_suspected:
            audit("injection_suspected", request_id=request_id, role=role.value)
        if not guard.allowed:
            raise Stop("refused", guard.code or "rejected", guard.message or "Permintaan ditolak.")

        area = restricted_topic(role, question)
        if area:
            raise Stop(
                "refused",
                "forbidden_data",
                f"Akses ditolak: peran {role.value} tidak diizinkan mengakses data {area}. "
                "Data ini hanya tersedia untuk peran Management.",
            )

        try:
            hints = resolve(self.db, role, question, budget=lambda: _remaining(deadline))
        except QueryError as exc:
            raise Stop("error", "database_unavailable", exc.safe_message) from None
        if hints.missing_units:
            raise Stop("ok", "not_found", f"Unit {hints.missing_units[0]} tidak ditemukan di data Units.")
        plan = self._plan(question, role, hints, deadline)

        repair_used = False
        while True:
            queries = self._validate(plan, role, question)
            try:
                problem = check_filters(self.db, role, [vq for _, vq in queries], budget=lambda: _remaining(deadline))
                if problem:
                    raise Stop("ok", problem.kind, problem.message)
                results = []
                for _, vq in queries:
                    _remaining(deadline)  # each query is bounded by DB_QUERY_TIMEOUT_S; check between them
                    results.append(self.db.run(role, vq, self.settings.max_rows, self.settings.max_result_bytes))
                break
            except QueryError as exc:
                if exc.kind == "permission":
                    raise Stop("refused", "database_denied", "Akses ditolak oleh database untuk peran ini.") from None
                if exc.kind == "timeout":
                    raise Stop("error", "query_timeout", "Query melebihi batas waktu eksekusi.") from None
                if exc.kind != "repairable" or repair_used:
                    raise Stop("error", "query_failed", "Query tidak dapat dijalankan.") from None
                repair_used = True
                audit("repair", request_id=request_id, role=role.value, sql_error=exc.number)
                plan = self._plan(question, role, hints, deadline, repair=_repair_message(exc))

        if time.monotonic() > deadline:  # never report success after the budget is spent
            raise Stop("error", "deadline_exceeded", "Permintaan melebihi batas waktu.")
        titled = [(title, result) for (title, _), result in zip(queries, results)]
        return AskResponse(
            request_id=request_id,
            status="ok",
            role=role.value,
            answer=render_answer(plan.answer_template, question, titled),
            sections=[build_section(title, vq.sql, r) for (title, vq), r in zip(queries, results)],
        )

    def _plan(self, question: str, role: Role, hints, deadline: float, repair: str | None = None) -> QueryPlan:
        timeout = min(self.settings.llm_timeout_s, _remaining(deadline))
        try:
            plan = self.planner.plan(question, role, hints, repair=repair, timeout=timeout)
        except LLMError as exc:
            raise Stop("error", exc.code, exc.message) from None
        if plan.status == "refuse":
            if "out_of_scope" in plan.reason:
                raise Stop(
                    "refused",
                    "forbidden_data",
                    f"Pertanyaan ini memerlukan data di luar hak akses peran {role.value}.",
                )
            raise Stop("refused", "model_refused", _model_text(plan.reason, question, "Permintaan tidak dapat diproses."))
        if plan.status == "clarify":
            raise Stop("ok", "clarify", _model_text(plan.reason, question, "Mohon perjelas pertanyaan Anda."))
        return plan

    def _validate(self, plan: QueryPlan, role: Role, question: str) -> list[tuple[str, ValidatedQuery]]:
        validated: list[tuple[str, ValidatedQuery]] = []
        for i, query in enumerate(plan.queries, start=1):
            try:
                vq = validate(query.sql, ROLE_TABLES[role], self.settings.max_rows)
            except PolicyViolation as exc:
                raise Stop("refused", exc.code, f"Query ditolak oleh kebijakan keamanan: {exc.message}") from None
            validated.append((_model_text(query.title, question, f"Hasil {i}"), vq))
        return validated


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining < 1:
        raise Stop("error", "deadline_exceeded", "Permintaan melebihi batas waktu.")
    return remaining


def _repair_message(exc: QueryError) -> str:
    return (
        f"Your previous plan failed on SQL Server: {exc.safe_message} "
        "Return the complete corrected JSON plan, following every rule above."
    )


def _model_text(text: str, question: str, fallback: str) -> str:
    """Model-written text shown to the user (titles, refusal/clarification reasons): single line, bounded,
    and replaced by a fixed fallback if it states figures of its own."""
    cleaned = " ".join(text.split())[:300]
    return cleaned if cleaned and model_text_ok(cleaned, question) else fallback
