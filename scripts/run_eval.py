"""Run Q1-Q5 x {Sales_Staff, Management} against the real model and database; write evidence.

    uv run python scripts/run_eval.py              # in-process app, 1 run
    uv run python scripts/run_eval.py --runs 5     # repeatability
    uv run python scripts/run_eval.py --url http://127.0.0.1:8000   # a running server

Outputs evidence/eval_report.md and evidence/eval_report.json. Exit code 1 if any case failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import platform
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

import httpx

from southcity_assistant.evaluation import QUESTIONS, Reference, run_suite
from southcity_assistant.security.roles import Role
from southcity_assistant.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--url", help="base URL of a running API (default: in-process app)")
    parser.add_argument("--out", default=str(ROOT / "evidence"))
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds between requests (keep a deployed instance under its rate limit)")
    args = parser.parse_args()

    settings = get_settings()
    if args.url:
        http = httpx.Client(base_url=args.url, timeout=120)
    else:
        from fastapi.testclient import TestClient

        from southcity_assistant.app import create_app

        http = TestClient(create_app(settings.model_copy(update={"rate_limit_per_minute": 10_000})))

    def post(question: str, role: Role) -> tuple[int, dict]:
        r = http.post("/api/v1/ask", json={"question": question}, headers={"X-User-Role": role.value})
        time.sleep(args.pause)
        return r.status_code, r.json()

    reference = Reference(settings)
    runs = []
    for i in range(args.runs):
        started = time.monotonic()
        results = run_suite(post, reference)
        runs.append({"run": i + 1, "seconds": round(time.monotonic() - started, 1),
                     "cases": [r.__dict__ for r in results]})
        passed = sum(r.passed for r in results)
        print(f"run {i + 1}: {passed}/{len(results)} passed")
        for r in results:
            if not r.passed:
                print(f"  FAIL {r.question} {r.role}: {'; '.join(r.problems)}")

    sql_version = reference.query("SELECT @@VERSION")[0][0].splitlines()[0]
    meta = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "model": settings.llm_model,
        "llm_base_url": settings.llm_base_url,
        "temperature": settings.llm_temperature,
        "sql_server": sql_version,
        "python": platform.python_version(),
        "packages": {p: version(p) for p in ("fastapi", "sqlglot", "pymssql", "openai", "pydantic")},
        "target": args.url or "in-process app",
        "runs": args.runs,
        "commit": git_commit(),
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "eval_report.json").write_text(json.dumps({"meta": meta, "runs": runs}, indent=2, ensure_ascii=False,
                                                     default=str), encoding="utf-8")
    (out / "eval_report.md").write_text(render_markdown(meta, runs), encoding="utf-8")
    total = sum(len(r["cases"]) for r in runs)
    ok = sum(c["passed"] for r in runs for c in r["cases"])
    print(f"overall: {ok}/{total} passed -> {out / 'eval_report.md'}")
    return 0 if ok == total else 1


def git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                             check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return sha + (" (uncommitted changes)" if dirty.strip() else "")


def render_markdown(meta: dict, runs: list[dict]) -> str:
    total = sum(len(r["cases"]) for r in runs)
    ok = sum(c["passed"] for r in runs for c in r["cases"])
    lines = [
        "# Live evaluation: management questions Q1-Q5 x roles",
        "",
        f"Generated {meta['generated_at']} by `scripts/run_eval.py` at commit `{meta['commit']}`, against the real",
        f"model and SQL Server. Target: {meta['target']}.",
        "",
        f"- Model: `{meta['model']}` via `{meta['llm_base_url']}` (temperature: {meta['temperature'] or 'provider default'})",
        f"- Database: {meta['sql_server']}",
        f"- Python {meta['python']}; " + ", ".join(f"{k} {v}" for k, v in meta["packages"].items()),
        f"- Runs: {meta['runs']} - **{ok}/{total} cases passed**",
        "",
        "Expected values come from independent reference SQL (Management login), cross-checked against",
        "hand-computed values from `schema.sql`. Q5 additionally checks that every table's checksum is unchanged.",
        "",
        "## Pass rate per case",
        "",
        "| Case | Role | Expectation | Passed |",
        "|---|---|---|---|",
    ]
    first = runs[0]["cases"]
    for i, case in enumerate(first):
        passed = sum(r["cases"][i]["passed"] for r in runs)
        lines.append(f"| {case['question']} | {case['role']} | {case['expectation']} | {passed}/{len(runs)} |")
    lines += ["", "## Responses (run 1)", ""]
    for case in first:
        resp = case["response"]
        lines += [f"### {case['question']} - {case['role']}", "",
                  f"> {QUESTIONS[case['question']]}", "",
                  f"HTTP {case['http_status']} - status `{resp.get('status')}`"
                  + (f", code `{resp.get('code')}`" if resp.get("code") else ""), "",
                  f"**Answer:** {resp.get('answer')}", ""]
        for s in resp.get("sections", []):
            lines += [f"**{s['title']}**", "", "```sql", s["sql"], "```", ""]
            if s["rows"]:
                lines.append("| " + " | ".join(s["columns"]) + " |")
                lines.append("|" + "---|" * len(s["columns"]))
                for row in s["rows"]:
                    lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
            else:
                lines.append("_(no rows)_")
            lines.append("")
        if case["problems"]:
            lines += ["Problems: " + "; ".join(case["problems"]), ""]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
