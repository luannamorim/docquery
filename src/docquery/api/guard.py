"""Input guard against prompt injection attacks.

Lightweight, zero-latency validator based on regex patterns and structural
heuristics. Applied before query_pipeline in the /query route. Returns a
(blocked: bool, reason: str | None) tuple.

Design: defence-in-depth. This guard is the first layer; the system prompt
in rag.py (which instructs the LLM to use only context and never reveal
instructions) is the second layer. Neither layer alone is sufficient.

Coverage (OWASP LLM Top 10 categories):
  LLM01 - Prompt Injection: instruction-override and role-injection patterns
  LLM06 - Sensitive Information Disclosure: prompt/instruction leak attempts
  Structural: oversized inputs and Unicode control character attacks
"""

import re
import unicodedata

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

_INSTRUCTION_OVERRIDE = re.compile(
    r"(ignore|disregard|forget|override|bypass|skip)"
    r"(?:\s+\w+){0,3}\s+"
    r"(instruction|instructions|prompt|prompts|context|rule|rules|constraint|constraints|guideline|guidelines)",
    re.IGNORECASE,
)

_ROLE_INJECTION = re.compile(
    r"("
    r"\b(system|assistant|user)\s*:\s*"  # "system: do this"
    r"|<\|(?:im_start|im_end|endoftext)\|>"  # OpenAI token sentinels
    r"|###\s*(system|instruction|prompt)"  # markdown-style fake headers
    r"|<sys>|</sys>"  # XML-style injection
    r")",
    re.IGNORECASE,
)

_PROMPT_LEAK = re.compile(
    r"(reveal|print|output|show|repeat|display|tell me|what is|what are)"
    r"(?:\s+\w+){0,2}\s+"
    r"(system\s+prompt|system\s+message|instructions?|hidden\s+prompt|initial\s+prompt|"
    r"initial\s+instructions?|original\s+prompt|full\s+prompt|base\s+prompt)",
    re.IGNORECASE,
)

_JAILBREAK = re.compile(
    r"("
    r"pretend\s+(you\s+are|to\s+be|you're|your\s+are)\s+(a|an)?\s*(different|evil|jailbreak|unrestricted|unfiltered|DAN|GPT)"
    r"|you\s+are\s+now\s+(DAN|evil\s+AI|unfiltered|jailbreak)"
    r"|act\s+as\s+(if\s+)?(you\s+have\s+no\s+restrictions|DAN|an?\s+(unrestricted|unfiltered))"
    r")",
    re.IGNORECASE,
)

# Unicode control characters used in adversarial prompts:
# - RLO (U+202E): right-to-left override, hides injected text
# - Zero-width joiners/non-joiners used to split keywords past naive filters
_UNICODE_ATTACK_CATEGORIES = frozenset({"Cf"})  # Unicode "Format" category

_MAX_QUERY_LENGTH = 2000

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_input(query: str) -> tuple[bool, str | None]:
    """Return (blocked, reason). blocked=True means the query was rejected.

    Args:
        query: The raw user query string from the /query endpoint.

    Returns:
        (False, None) if the query is safe to process.
        (True, reason_string) if the query should be rejected with HTTP 400.
    """
    if len(query) > _MAX_QUERY_LENGTH:
        return True, f"Query exceeds maximum length of {_MAX_QUERY_LENGTH} characters"

    if _has_unicode_control_chars(query):
        return True, "Query contains disallowed Unicode control characters"

    if _INSTRUCTION_OVERRIDE.search(query):
        return True, "Query contains instruction-override pattern"

    if _ROLE_INJECTION.search(query):
        return True, "Query contains role-injection pattern"

    if _PROMPT_LEAK.search(query):
        return True, "Query attempts to extract system instructions"

    if _JAILBREAK.search(query):
        return True, "Query contains jailbreak pattern"

    return False, None


def _has_unicode_control_chars(text: str) -> bool:
    return any(unicodedata.category(ch) in _UNICODE_ATTACK_CATEGORIES for ch in text)
