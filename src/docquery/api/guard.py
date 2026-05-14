"""Input guard against prompt injection attacks.

Lightweight, zero-latency validator based on regex patterns and structural
heuristics. Applied before query_pipeline in the /query route. Returns a
(blocked: bool, reason: str | None) tuple.

Design: defence-in-depth. This guard is the first layer; the system prompt
in rag.py (which instructs the LLM to use only context and never reveal
instructions) is the second layer. Neither layer alone is sufficient.

Coverage (OWASP LLM Top 10 categories):
  LLM01 - Prompt Injection: instruction-override and role-injection patterns
          (English + PT-BR/ES) and indirect injection via retrieved chunks
          (see check_context below)
  LLM06 - Sensitive Information Disclosure: prompt/instruction leak attempts
  Structural: oversized inputs, Unicode control character attacks, NFKC
              homoglyph normalization
"""

from __future__ import annotations

import re
import unicodedata

from docquery.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Verbs that attempt to override the system instructions, EN + PT-BR + ES.
_OVERRIDE_VERBS = (
    r"ignore|disregard|forget|override|bypass|skip"
    r"|ignor[ea]r?|esque[çc][ae]r?|desconsider[ea]r?|descart[ea]r?|sobrescre[vt]e?r?"
    r"|olvid[ae]r?|olvida"
)
# Targets of override, EN + PT-BR + ES.
_OVERRIDE_TARGETS = (
    r"instruction|instructions|prompt|prompts|context|rule|rules|constraint|constraints|guideline|guidelines"
    r"|instru[çc][ãa]o|instru[çc][õo]es|regras?|restri[çc][õo]es?|diretrizes?|contexto"
    r"|instrucci[oó]n|instrucciones|reglas?|restricciones?"
)

_INSTRUCTION_OVERRIDE = re.compile(
    rf"({_OVERRIDE_VERBS})(?:\s+\w+){{0,3}}\s+({_OVERRIDE_TARGETS})",
    re.IGNORECASE | re.UNICODE,
)

_ROLE_INJECTION = re.compile(
    r"("
    r"\b(system|assistant|user|sistema|usu[áa]rio)\s*:\s*"
    r"|<\|(?:im_start|im_end|endoftext)\|>"
    r"|###\s*(system|instruction|prompt|sistema|instru[çc][ãa]o)"
    r"|<sys>|</sys>"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Tightened: only match leak attempts that name a *qualified* prompt-like
# target (system/hidden/initial/original/full/base). The bare word
# "instructions" was previously matched, causing false positives on legitimate
# questions like "What are the instructions to configure X?".
_PROMPT_LEAK = re.compile(
    r"(reveal|print|output|show|repeat|display|expose|leak"
    r"|revel[ae]r?|exiba|mostre|imprim[ae]|repit[ae]"
    r"|tell\s+me|what\s+is|what\s+are"
    r"|me\s+diga|qual\s+(é|e|s[ãa]o)|quais\s+s[ãa]o)"
    r"(?:\s+\w+){0,3}\s+"
    r"(system|hidden|initial|original|full|base"
    r"|sistema|oculto|inicial|original|completo|base)"
    r"\s+"
    r"(prompt|prompts|instruction|instructions|message"
    r"|instru[çc][ãa]o|instru[çc][õo]es|mensagem)",
    re.IGNORECASE | re.UNICODE,
)

_JAILBREAK = re.compile(
    r"("
    r"pretend\s+(you\s+are|to\s+be|you're|your\s+are)\s+(a|an)?\s*(different|evil|jailbreak|unrestricted|unfiltered|DAN|GPT)"
    r"|you\s+are\s+now\s+(DAN|evil\s+AI|unfiltered|jailbreak)"
    r"|act\s+as\s+(if\s+)?(you\s+have\s+no\s+restrictions|DAN|an?\s+(unrestricted|unfiltered))"
    r"|finja\s+que\s+(é|e)\s+(um|uma)"
    r"|aja\s+como\s+(se|um|uma)"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Unicode "Format" category (Cf) covers adversarial chars like RLO (U+202E)
# but also a few legitimate joiners commonly seen in PT/EN text. Block Cf
# except for the allowlist below.
_UNICODE_CF_ALLOW = frozenset(
    {
        "‌",  # ZWNJ
        "‍",  # ZWJ — used in emoji ZWJ sequences and some scripts
        "‎",  # LRM
        "‏",  # RLM
        "﻿",  # BOM
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_input(query: str, settings: Settings | None = None) -> tuple[bool, str | None]:
    """Return (blocked, reason). blocked=True means the query was rejected.

    The query is NFKC-normalized before regex matching so homoglyph and
    fullwidth-Latin attacks are flattened to the canonical form.
    """
    settings = settings or get_settings()
    max_len = settings.guard_max_query_length

    normalized = unicodedata.normalize("NFKC", query)

    if len(normalized) > max_len:
        return True, f"Query exceeds maximum length of {max_len} characters"

    if _has_disallowed_control_chars(normalized):
        return True, "Query contains disallowed Unicode control characters"

    if _INSTRUCTION_OVERRIDE.search(normalized):
        return True, "Query contains instruction-override pattern"

    if _ROLE_INJECTION.search(normalized):
        return True, "Query contains role-injection pattern"

    if _PROMPT_LEAK.search(normalized):
        return True, "Query attempts to extract system instructions"

    if _JAILBREAK.search(normalized):
        return True, "Query contains jailbreak pattern"

    return False, None


def _has_disallowed_control_chars(text: str) -> bool:
    for ch in text:
        if unicodedata.category(ch) == "Cf" and ch not in _UNICODE_CF_ALLOW:
            return True
    return False


# Subset of patterns applied to retrieved chunks. Role-injection sentinels and
# explicit instruction overrides in *indexed content* are the strongest
# signal of indirect prompt injection (LLM01 — see OWASP LLM Top 10). We do
# not include _JAILBREAK or the broad PROMPT_LEAK patterns because docs may
# legitimately contain examples about prompt injection.
_CONTEXT_PATTERNS = (
    ("instruction_override", _INSTRUCTION_OVERRIDE),
    ("role_injection", _ROLE_INJECTION),
)


def check_context(contexts: list[dict]) -> list[tuple[str, str]]:
    """Scan retrieved chunks for indirect-injection signals.

    Returns a list of (source, reason) tuples for chunks whose text matches an
    injection pattern. Callers should LOG these (defence in depth) rather
    than block — indexed docs may contain legitimate examples about prompt
    attacks. The hardened system prompt is the final line of defence.
    """
    findings: list[tuple[str, str]] = []
    for ctx in contexts:
        text = unicodedata.normalize("NFKC", str(ctx.get("text", "")))
        for reason, pattern in _CONTEXT_PATTERNS:
            if pattern.search(text):
                findings.append((str(ctx.get("source", "")), reason))
                break
    return findings
