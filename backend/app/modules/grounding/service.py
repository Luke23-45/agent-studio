"""Output grounding: citations from retrieved docs + faithfulness check.

The faithfulness check is a lexical-overlap heuristic: an answer is
"grounded" when a meaningful fraction of its significant tokens appear in
the retrieved context. This is a cheap first-pass guard; a RAGAS-style
embedding comparison can replace it without changing the interface.
"""

import re
import structlog
from dataclasses import dataclass, field
from typing import Any

logger = structlog.get_logger(__name__)

_STOPWORDS = frozenset(
    """
    a an the and or but if then else when while of to in on at by for with
    from as is are was were be been being do does did have has had having
    it its this that these those i you he she we they me him her us them my
    your our their what which who whom how why not no yes so just very can
    could would should may might will shall about into over under again
    further once here there all any both each few more most other some such
    only own same than too s t don now
    """.split()
)


@dataclass
class FaithfulnessResult:
    """Outcome of the grounding check on a generated response."""

    score: float = 0.0
    grounded: bool = False
    source_count: int = 0
    matches: list[str] = field(default_factory=list)
    checked: bool = False


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


class FaithfulnessChecker:
    """Check that a response is grounded in the retrieved context."""

    def __init__(self, threshold: float = 0.12, max_matches: int = 8):
        self.threshold = threshold
        self.max_matches = max_matches

    def check(self, response: str, sources: list[dict[str, Any]] | list[str]) -> FaithfulnessResult:
        """Score lexical overlap between the response and each source chunk.

        ``sources`` entries may be dicts (``{"content": ...}``) or plain
        strings. Returns ``FaithfulnessResult`` with ``checked=False`` when
        there is nothing to ground against.
        """
        contents = [
            (s.get("content") if isinstance(s, dict) else s) for s in sources
        ]
        contents = [c for c in contents if c]

        if not response or not contents:
            return FaithfulnessResult(source_count=len(contents))

        response_tokens = _tokens(response)
        if not response_tokens:
            return FaithfulnessResult(source_count=len(contents))

        best = 0.0
        best_matches: set[str] = set()
        for content in contents:
            source_tokens = _tokens(content)
            if not source_tokens:
                continue
            overlap = response_tokens & source_tokens
            score = len(overlap) / len(response_tokens)
            if score > best:
                best = score
                best_matches = overlap

        matches = sorted(best_matches)[: self.max_matches]
        result = FaithfulnessResult(
            score=round(best, 4),
            grounded=best >= self.threshold,
            source_count=len(contents),
            matches=matches,
            checked=True,
        )
        if not result.grounded:
            logger.warning(
                "faithfulness_low",
                score=result.score,
                threshold=self.threshold,
                match_tokens=len(matches),
            )
        return result
