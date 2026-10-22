"""Roles, what each may read, and how the caller's role is established."""

from __future__ import annotations

import hmac
import re
from enum import StrEnum


class Role(StrEnum):
    SALES_STAFF = "Sales_Staff"
    MANAGEMENT = "Management"


ROLE_TABLES: dict[Role, frozenset[str]] = {
    Role.SALES_STAFF: frozenset({"Units", "Sales_Contracts", "Invoices_Billing"}),
    Role.MANAGEMENT: frozenset({"Units", "Sales_Contracts", "Invoices_Billing", "GL_Journals"}),
}


# Wording that clearly asks for data a role may not read. A supporting control that gives a clear,
# deterministic refusal before any model call; the real enforcement is the SQL policy's table
# allowlist and the database DENY.
RESTRICTED_TOPICS: dict[Role, tuple[tuple[re.Pattern[str], str], ...]] = {
    Role.SALES_STAFF: (
        (
            re.compile(
                r"\bgl[_ ]?journals?\b|\bgeneral\s+ledger\b|\bbuku\s+besar\b|\bjurnal\b|\bjournals?\b"
                r"|\bledger\b|\bkas\s*(?:&|dan|and)\s*bank\b|\bakun\s+kas\b|\bpendapatan\s+sales\s+unit\b"
                r"|\baccount\s*(?:code|name)\b|\bkode\s+akun\b",
                re.IGNORECASE,
            ),
            "GL_Journals (General Ledger)",
        ),
    ),
    Role.MANAGEMENT: (),
}


def restricted_topic(role: Role, question: str) -> str | None:
    """Name of the restricted data area the question asks for, if the role may not read it."""
    for pattern, area in RESTRICTED_TOPICS[role]:
        if pattern.search(question):
            return area
    return None


class RoleError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def parse_role(value: str) -> Role:
    try:
        return Role(value.strip())
    except ValueError:
        allowed = ", ".join(r.value for r in Role)
        raise RoleError(403, "unknown_role", f"Unknown user_role. Allowed: {allowed}.") from None


def resolve_role(
    *,
    header_role: str | None,
    body_role: str | None,
    api_key: str | None,
    auth_mode: str,
    api_keys: dict[str, str],
) -> Role:
    """Establish the effective role.

    demo    : the caller's user_role is trusted (what the assignment specifies). It may come from the
              `X-User-Role` header or the `user_role` body field; if both are sent they must agree.
    api_key : the role is bound to the API key. A requested user_role must match it, never widen it.
    """
    requested: Role | None = None
    if header_role is not None and body_role is not None and header_role.strip() != body_role.strip():
        raise RoleError(400, "conflicting_role", "X-User-Role header and user_role field disagree.")
    raw = header_role if header_role is not None else body_role
    if raw is not None:
        requested = parse_role(raw)

    if auth_mode == "api_key":
        bound = _role_for_key(api_key, api_keys)
        if requested is not None and requested != bound:
            raise RoleError(403, "role_not_permitted", "This API key is not permitted that role.")
        return bound

    if requested is None:
        raise RoleError(400, "missing_role", "user_role is required (X-User-Role header or body).")
    return requested


def _role_for_key(api_key: str | None, api_keys: dict[str, str]) -> Role:
    if not api_key:
        raise RoleError(401, "missing_api_key", "X-API-Key is required.")
    for key, role in api_keys.items():
        if hmac.compare_digest(api_key.encode(), key.encode()):
            return parse_role(role)
    raise RoleError(401, "invalid_api_key", "Invalid API key.")
