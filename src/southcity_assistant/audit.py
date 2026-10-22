"""Structured audit events. Records decisions and SQL fingerprints, not question text or literals."""

from __future__ import annotations

import hashlib
import json
import logging

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

logger = logging.getLogger("southcity.audit")


def sql_fingerprint(sql: str) -> str:
    """Shape of the query with every literal replaced, so names and amounts never reach the log."""
    try:
        tree = sqlglot.parse_one(sql, read="tsql")
        tree = tree.transform(
            lambda n: exp.Placeholder() if isinstance(n, (exp.Literal, exp.National)) else n
        )
        normalized = tree.sql(dialect="tsql")
    except SqlglotError:
        normalized = "<unparseable>"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def audit(event: str, **fields: object) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, ensure_ascii=False))
