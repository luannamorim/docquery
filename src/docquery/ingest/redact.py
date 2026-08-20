"""Replace PII with typed placeholders before anything persists.

Detects CPF, CNPJ (numeric and the alphanumeric format the Receita is
rolling out), e-mail addresses and Brazilian phone numbers, and rewrites
each into a stable typed placeholder ([CPF], [CNPJ], [EMAIL], [TELEFONE]).
Replacement, never removal: a passage with a silent hole would still read
as the document's own words, and a citation has to stay legible.

The seam is `redact_chunks` inside `ingest_chunks` — the only door to
Qdrant — not the parsed document. The spec asked for "after parse, before
chunking", but the Docling path chunks from `dl_doc` and never reads
`Document.content`, so document-level redaction would silently miss every
Docling-parsed file. Chunk level covers both paths in one place, before
embedding, sparse indexing, point-ID hashing and payload assembly.

Every detector validates its match (check digits for CPF/CNPJ, shape rules
for phones) because the corpus is full of near-misses: a 9-12 digit
contract number is not a CPF, and "2020-2024" is a range of years, not a
phone. A missed phone is recoverable; a redacted contract number is a
silent retrieval loss — so false negatives beat false positives.

Redaction changes chunk text and therefore point IDs; the per-source
pre-delete in the pipeline makes re-ingest safe. Enabling the flag on an
existing corpus means re-ingesting everything. Log lines never carry chunk
text today; anything that starts to must pass through `redact_text` first.

TODO: proper names ("João da Silva") need NER, which regex cannot do —
deliberately out of scope until the cost/benefit is measured separately.
"""

import re
from collections.abc import Callable

from docquery.config import Settings
from docquery.ingest.chunker import Chunk

#: Content-derived metadata that can leak the same PII the text does.
#: source/folders/sector/entity are operator-controlled paths and stay.
_REDACTED_META_KEYS = ("section", "title", "tags", "emphasis", "emphasis_screen")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# The digit lookarounds keep an 11/14-digit slice of a longer run (a contract
# number, a barcode) from ever matching.
_CPF_RE = re.compile(r"(?<![\d.])(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})(?!\d)")
_CNPJ_RE = re.compile(r"(?<![\d.])(?:\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}|\d{14})(?!\d)")

# Alphanumeric CNPJ: only the fully punctuated uppercase form. A bare
# 12-char uppercase run would match half the SKU codes in a manual.
_CNPJ_ALNUM_RE = re.compile(
    r"(?<![A-Z0-9])[A-Z0-9]{2}\.[A-Z0-9]{3}\.[A-Z0-9]{3}/[A-Z0-9]{4}-\d{2}(?![A-Z0-9])"
)

# Landlines start 2-5 and mobiles 9 (ANATEL numbering). Bare unpunctuated
# 10-11 digit runs are deliberately NOT phones — they are contract numbers
# far more often. The "/" in both boundary classes keeps dates out.
_PHONE_RE = re.compile(
    r"""
    (?<![\w.\-/])
    (?:
        \+55[\s.]?\(?\d{2}\)?[\s.]?(?:9\d{4}|\d{4})[\s.\-]?\d{4}
      | \(\d{2}\)[\s.]?(?:9\d{4}|\d{4})[\s.\-]?\d{4}
      | (?:\d{2}\s)?(?:9\d{4}|[2-5]\d{3})-\d{4}
    )
    (?![\w\-/])
    """,
    re.VERBOSE,
)

_YEAR_RANGE_RE = re.compile(r"^(\d{4})-(\d{4})$")


def _digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def _valid_cpf(digits: str) -> bool:
    """Check-digit validation over exactly 11 digit chars."""
    if len(digits) != 11 or not digits.isdigit():
        return False
    if len(set(digits)) == 1:
        # 111.111.111-11 and friends pass the DV math but are invalid.
        return False
    for n in (9, 10):
        s = sum(int(c) * w for c, w in zip(digits[:n], range(n + 1, 1, -1)))
        if (s * 10) % 11 % 10 != int(digits[n]):
            return False
    return True


_CNPJ_W1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
_CNPJ_W2 = [6, *_CNPJ_W1]


def _cnpj_dvs(base: str) -> tuple[int, int]:
    """Check digits for a 12-char CNPJ base, numeric or alphanumeric.

    The Receita rule values every char as ord(c) - 48, so digits map to
    0-9 and A-Z to 17-42; the check digits themselves are always numeric.
    """

    def dv(vals: list[int], weights: list[int]) -> int:
        r = sum(v * w for v, w in zip(vals, weights)) % 11
        return 0 if r < 2 else 11 - r

    vals = [ord(c) - 48 for c in base]
    dv1 = dv(vals, _CNPJ_W1)
    dv2 = dv([*vals, dv1], _CNPJ_W2)
    return dv1, dv2


def _valid_cnpj(value: str) -> bool:
    """One routine for both formats: value is 14 chars, punctuation stripped."""
    if len(value) != 14 or not value[12:].isdigit():
        return False
    if value.isdigit() and len(set(value)) == 1:
        return False
    dv1, dv2 = _cnpj_dvs(value[:12])
    return value[12:] == f"{dv1}{dv2}"


def _plausible_phone(value: str) -> bool:
    """Reject the bare hyphenated form when both halves read as years."""
    match = _YEAR_RANGE_RE.match(value)
    if match and all(1900 <= int(g) <= 2099 for g in match.groups()):
        return False
    return True


def _strip_cnpj(value: str) -> str:
    return value.replace(".", "").replace("/", "").replace("-", "")


# Ordered: e-mail first so digit runs inside addresses vanish before the
# phone pass; CNPJ (14) before CPF (11) before phone so a longer id is
# never partially eaten.
_DETECTORS: list[tuple[str, re.Pattern[str], Callable[[str], bool]]] = [
    ("[EMAIL]", _EMAIL_RE, lambda value: True),
    ("[CNPJ]", _CNPJ_ALNUM_RE, lambda value: _valid_cnpj(_strip_cnpj(value))),
    ("[CNPJ]", _CNPJ_RE, lambda value: _valid_cnpj(_strip_cnpj(value))),
    ("[CPF]", _CPF_RE, lambda value: _valid_cpf(_digits(value))),
    ("[TELEFONE]", _PHONE_RE, _plausible_phone),
]


def redact_text(text: str) -> str:
    """Pure and unconditional — flag checks live at the seams, not here."""
    for placeholder, pattern, valid in _DETECTORS:

        def repl(
            match: re.Match[str], placeholder: str = placeholder, valid=valid
        ) -> str:
            value = match.group(0)
            return placeholder if valid(value) else value

        text = pattern.sub(repl, text)
    return text


def redact_chunks(chunks: list[Chunk], settings: Settings) -> list[Chunk]:
    """Redact chunk text and content-derived metadata; identity when off."""
    if not settings.pii_redaction_enabled:
        return chunks

    redacted: list[Chunk] = []
    for chunk in chunks:
        metadata = dict(chunk.metadata)
        for key in _REDACTED_META_KEYS:
            value = metadata.get(key)
            if isinstance(value, str):
                metadata[key] = redact_text(value)
            elif isinstance(value, list):
                metadata[key] = [
                    redact_text(item) if isinstance(item, str) else item
                    for item in value
                ]
        redacted.append(Chunk(text=redact_text(chunk.text), metadata=metadata))
    return redacted
