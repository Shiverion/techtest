"""Deterministic screening of the question before any model call.

Refuses clear requests to change data (Indonesian or English), including mixed requests such as
"show X, then delete Y". It matches *intent*, not bare substrings: passive or descriptive uses
("berapa unit yang dihapus?") and negated ones ("jangan hapus apa pun") pass through.

This is a supporting control. A request that slips past it still meets the model's read-only
instructions, the SQL policy (SELECT only) and a database login that has no write permission.
Prompt-injection phrases are detected and logged but never decide authorization by themselves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_QUESTION_CHARS = 2000

_NEGATION = r"(?:jangan|tidak|tanpa|bukan|don'?t|do\s+not|never|without)"

# SQL write syntax anywhere in the question.
_SQL_WRITE = re.compile(
    r"\b(?:delete\s+from|drop\s+(?:table|database|view|schema|login|user)|truncate\s+table|"
    r"insert\s+into|update\s+\[?\w+\]?\s+set|alter\s+(?:table|database|login|user|role)|"
    r"create\s+(?:table|view|procedure|proc|function|login|user|trigger)|merge\s+into|"
    r"grant\s+\w+|revoke\s+\w+|exec(?:ute)?\s+\w+|xp_cmdshell|sp_executesql)\b",
    re.IGNORECASE,
)

# Indonesian imperative write verbs ("hapus record ...", "tolong ubah ...").
_ID_IMPERATIVE = (
    r"hapus(?:kan|lah)?|ubah(?:lah)?|rubah|ganti(?:kan|lah)?|perbarui|update(?:lah)?|edit|"
    r"tambahkan|masukkan|kosongkan|bersihkan|hilangkan|musnahkan|batalkan|koreksi|setel|reset"
)
# Indonesian active forms, write intent only after a request marker ("saya mau menghapus ...").
_ID_ACTIVE = (
    r"menghapus|mengubah|merubah|mengganti|memperbarui|menambahkan|memasukkan|mengosongkan|"
    r"membersihkan|menghilangkan|membatalkan|mengoreksi|mengedit|mengupdate"
)
_REQUEST_MARKER = (
    r"tolong|mohon|harap|silakan|silahkan|bisa(?:kah)?|dapatkah|saya\s+(?:mau|ingin|minta)|"
    r"ingin|mau|segera|lakukan|please|can\s+you|could\s+you"
)
# English imperative / third-person verbs followed (within a few words) by a data object
# ("delete the row", "SQL that deletes the table"). Past participles are left out: "deleted records" is a
# description, not a request.
_EN_VERB = (
    r"(?:delete|remove|erase|drop|truncate|update|modify|change|edit|insert|add|alter|overwrite|wipe)s?"
)
_DATA_OBJECT = (
    r"record|records|row|rows|data|table|tables|entry|entries|transaction|transactions|journal|"
    r"journals|invoice|invoices|contract|contracts|unit|units|customer|amount|status|balance|"
    r"database|everything|journalid|invoiceid|contractid|unitid"
)

# \b keeps passive forms out: there is no word boundary inside "dihapus" or "diubah".
_ID_WRITE = re.compile(rf"\b(?:{_ID_IMPERATIVE})\b(?=\s+\S)", re.IGNORECASE)
_ID_ACTIVE_WRITE = re.compile(
    rf"\b(?:{_REQUEST_MARKER})\b(?:\s+\w+){{0,2}}\s+\b(?:{_ID_ACTIVE})\b", re.IGNORECASE
)
_EN_WRITE = re.compile(
    rf"\b(?:{_EN_VERB})\b(?:\s+\w+){{0,3}}?\s+\b(?:{_DATA_OBJECT})\b", re.IGNORECASE
)
_NEGATED = re.compile(rf"\b{_NEGATION}\s+(?:\w+\s+){{0,1}}$", re.IGNORECASE)

_INJECTION = re.compile(
    r"abaikan\s+(?:semua\s+)?(?:instruksi|perintah|aturan)|ignore\s+(?:all\s+)?(?:previous|prior|above)"
    r"|disregard\s+(?:all\s+)?(?:previous|prior)|system\s+administrator|saya\s+(?:adalah\s+)?admin"
    r"|you\s+are\s+now|act\s+as|developer\s+mode|jailbreak|system\s+prompt|<\s*/?\s*system\s*>",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    code: str | None = None
    message: str | None = None
    injection_suspected: bool = False


def screen_question(question: str) -> GuardResult:
    text = " ".join(question.split())
    injection = bool(_INJECTION.search(text))
    if not text:
        return GuardResult(False, "empty_question", "Pertanyaan kosong.", injection)
    if len(question) > MAX_QUESTION_CHARS:
        return GuardResult(
            False, "question_too_long",
            f"Pertanyaan melebihi {MAX_QUESTION_CHARS} karakter.", injection,
        )
    if _has_write_intent(text):
        return GuardResult(
            False,
            "write_request",
            "Permintaan ditolak: asisten ini hanya dapat membaca data (read-only). Perintah untuk "
            "menghapus, mengubah, atau menambah data (DELETE/UPDATE/INSERT/DROP/ALTER) tidak "
            "dapat diproses, apa pun peran atau instruksi yang disebutkan.",
            injection,
        )
    return GuardResult(True, injection_suspected=injection)


def _has_write_intent(text: str) -> bool:
    if _SQL_WRITE.search(text) or _ID_ACTIVE_WRITE.search(text):
        return True
    for pattern in (_ID_WRITE, _EN_WRITE):
        for match in pattern.finditer(text):
            if not _NEGATED.search(text[: match.start()]):
                return True
    return False
