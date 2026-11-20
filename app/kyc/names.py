"""Name matching for government-ID verification (spec §5.9: normalised, fuzzy token
similarity ≥ 0.85).

Names are normalised (Unicode NFKC, case-folded, punctuation dropped, honorifics removed)
and compared as token sets, so word order, extra middle names and small spelling
differences don't fail a genuine match:

- "Sharma Asha" vs "Asha Sharma"           -> 1.0  (order)
- "Asha Sharma" vs "Asha Kumari Sharma"    -> 1.0  (the ID carries a middle name)
- "Ravi Kumar"  vs "Ravi Kumaar"           -> 0.95 (spelling)
- "Asha Rao"    vs "Asha Sharma"           -> 0.74 (different person)

It is lenient by design, and the DOB must match as well: a profile name that's a subset of
the ID name ("Asha" vs "Asha Sharma") scores 1.0, and close first names can pass ("Priya
Singh" vs "Priyanka Singh" -> 0.88). The threshold is config (`kyc.name_match_threshold`).

This is the token-set ratio (as in fuzzywuzzy/rapidfuzz), built on difflib.
"""

import re
import unicodedata
from difflib import SequenceMatcher

_HONORIFICS = frozenset(
    {"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "shri", "sri", "smt", "kumari", "km", "sushri"}
)
_WORD = re.compile(r"[^\W_]+")


def name_tokens(name: str) -> list[str]:
    text = unicodedata.normalize("NFKC", name).casefold()
    return [t for t in _WORD.findall(text) if t not in _HONORIFICS]


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def name_similarity(a: str | None, b: str | None) -> float:
    """0..1. Empty or missing names never match."""
    ta, tb = set(name_tokens(a or "")), set(name_tokens(b or ""))
    if not ta or not tb:
        return 0.0
    common = " ".join(sorted(ta & tb))
    only_a = " ".join(sorted(ta - tb))
    only_b = " ".join(sorted(tb - ta))
    with_a = f"{common} {only_a}".strip()
    with_b = f"{common} {only_b}".strip()
    if not common:
        return _ratio(with_a, with_b)
    return max(_ratio(common, with_a), _ratio(common, with_b), _ratio(with_a, with_b))
