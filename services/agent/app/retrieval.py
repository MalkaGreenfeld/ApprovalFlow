"""Retrieval over the policy document (N5).

Puts the relevant clauses in the prompt instead of the whole policy.

Indexing splits the policy into retrievable units: each rule table row becomes a
chunk carrying its rule_id, prose becomes chunks under its heading. The index is
rebuilt when the document changes, so an edit through the admin API takes effect
without a restart.

Ranking is BM25 (Okapi) over those chunks, against a query built from the
submission's own facts. Pure Python and deterministic, so CI and the eval harness
run the real retrieval path with no API key. When an embedding model is
configured, dense similarity is fused in by Reciprocal Rank Fusion; that is off
by default.

The global hard stops and the autonomy thresholds are always included whatever
they score. Retrieval is an optimisation and must never be the reason the model
did not see "a new vendor is always reviewed".

The retrieved rule_ids travel with the recommendation and are stored on the
decision row, so the audit trail shows what the analysis was grounded in (F9).
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from approvalflow.logging import get_logger

logger = get_logger(__name__)

TOP_K = int(os.getenv("RAG_TOP_K", "6"))
BM25_K1 = 1.5
BM25_B = 0.75
#: Rule-id prefixes that must never be omitted from the prompt.
MANDATORY_PREFIXES = ("GLOBAL-", "AUTONOMY-")
#: Section headings always included (the autonomy posture).
MANDATORY_SECTIONS = ("autonomy thresholds",)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-']*")
#: Matches a policy table row keyed by a rule id. The id part deliberately allows
#: letters *and* digits after the dash: the six ``GLOBAL-RECEIPT`` /
#: ``GLOBAL-VENDOR`` style hard stops are worded that way, and an id pattern that
#: only accepted ``MEAL-01`` style ids silently left every hard stop out of the
#: index, which are the clauses that matter most.
#: ``[^|]*`` after the id allows a qualifier in the same cell. The autonomy table
#: writes "`AUTONOMY-CEILING` Tier 1", and a pattern that demanded the cell end
#: right after the id left the ceiling clause itself out of the index.
_RULE_ROW_RE = re.compile(r"^\|\s*`?([A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+)`?[^|]*\|(.+)\|\s*$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")

#: Words in a section heading that mark it as belonging to one expense category.
#: These keep another category's caps out of the prompt (the metadata-filter half
#: of hybrid search).
SECTION_CATEGORY_HINTS: dict[str, tuple[str, ...]] = {
    "meals": ("meal", "entertainment"),
    "travel": ("travel",),
    "saas": ("software", "saas", "subscription"),
    "hardware": ("hardware",),
}

#: Very small stop list, enough to stop "the" dominating the scores without
#: needing an NLP dependency.
_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "with", "this", "these", "those", "must", "may", "any", "all", "each", "per", "not", "no"]
)


def tokenize(text: str) -> list[str]:
    """Lower-case alphanumeric tokens with stop words removed."""
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


@dataclass
class PolicyChunk:
    """One retrievable unit of the policy."""

    chunk_id: str
    section: str
    text: str
    rule_id: str | None = None
    tokens: list[str] = field(default_factory=list)
    embedding: list[float] | None = None

    @property
    def is_mandatory(self) -> bool:
        if self.rule_id and self.rule_id.startswith(MANDATORY_PREFIXES):
            return True
        return any(s in self.section.lower() for s in MANDATORY_SECTIONS)

    @property
    def category_hint(self) -> str | None:
        """The expense category this chunk belongs to, or ``None`` if global.

        Derived from the section heading, so it keeps working when a controller
        edits the policy prose (the headings are the stable part).
        """
        if self.is_mandatory:
            return None
        heading = self.section.lower()
        for category, needles in SECTION_CATEGORY_HINTS.items():
            if any(needle in heading for needle in needles):
                return category
        return None

    def citation(self) -> str:
        label = self.rule_id or self.section
        return f"[{label}] {self.text}"


def chunk_policy(markdown: str) -> list[PolicyChunk]:
    """Split the policy into retrievable chunks.

    Rule table rows become one chunk each so a single clause can be retrieved
    without dragging in its neighbours; everything else is chunked by paragraph
    under its heading.
    """
    chunks: list[PolicyChunk] = []
    section = "Preamble"
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(line.strip() for line in paragraph).strip()
        paragraph.clear()
        # Skip table headers, separators and blockquote noise.
        if len(text) < 20 or set(text) <= set("|-: "):
            return
        chunks.append(
            PolicyChunk(
                chunk_id=f"c{len(chunks)}",
                section=section,
                text=text,
                tokens=tokenize(f"{section} {text}"),
            )
        )

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        heading = _HEADING_RE.match(line)
        if heading:
            flush_paragraph()
            section = heading.group(1).strip()
            continue

        rule_row = _RULE_ROW_RE.match(line.strip())
        if rule_row:
            flush_paragraph()
            rule_id = rule_row.group(1)
            body = re.sub(r"[|`*]", " ", rule_row.group(2)).strip()
            body = re.sub(r"\s+", " ", body)
            chunks.append(
                PolicyChunk(
                    chunk_id=f"c{len(chunks)}",
                    section=section,
                    text=body,
                    rule_id=rule_id,
                    tokens=tokenize(f"{rule_id} {section} {body}"),
                )
            )
            continue

        if not line.strip():
            flush_paragraph()
        else:
            paragraph.append(line)

    flush_paragraph()
    logger.info(
        "Indexed policy into %s chunks (%s rule clauses)",
        len(chunks),
        sum(1 for c in chunks if c.rule_id),
    )
    return chunks


class PolicyIndex:
    """BM25 index over the policy chunks, with optional dense fusion."""

    def __init__(self, chunks: list[PolicyChunk], version: int, digest: str) -> None:
        self.chunks = chunks
        self.version = version
        self.digest = digest
        self._doc_freq: Counter[str] = Counter()
        for chunk in chunks:
            self._doc_freq.update(set(chunk.tokens))
        self._n = max(1, len(chunks))
        self._avg_len = (
            sum(len(c.tokens) for c in chunks) / self._n if chunks else 1.0
        ) or 1.0

    # -- sparse retrieval ------------------------------------------------

    def _idf(self, term: str) -> float:
        df = self._doc_freq.get(term, 0)
        # BM25 idf with the +0.5 smoothing, floored so common terms cannot go
        # negative and invert the ranking.
        return max(0.0, math.log(1 + (self._n - df + 0.5) / (df + 0.5)))

    def bm25_scores(self, query_tokens: list[str]) -> dict[str, float]:
        scores: dict[str, float] = {}
        query_counts = Counter(query_tokens)
        for chunk in self.chunks:
            counts = Counter(chunk.tokens)
            length = len(chunk.tokens) or 1
            score = 0.0
            for term, q_count in query_counts.items():
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                denominator = tf + BM25_K1 * (
                    1 - BM25_B + BM25_B * length / self._avg_len
                )
                score += self._idf(term) * (tf * (BM25_K1 + 1) / denominator) * q_count
            if score > 0:
                scores[chunk.chunk_id] = score
        return scores

    # -- retrieval -------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        category: str,
        top_k: int = TOP_K,
        dense_scores: dict[str, float] | None = None,
    ) -> list[tuple[PolicyChunk, float]]:
        """Return the most relevant chunks plus every mandatory clause.

        Args:
            query: Natural-language query built from the submission's facts.
            category: Item category, so its policy section is always in scope.
            top_k: How many *scored* chunks to keep before mandatory clauses.
            dense_scores: Optional embedding similarity by chunk id, fused with
                BM25 using Reciprocal Rank Fusion.

        Returns:
            ``(chunk, score)`` pairs, mandatory clauses first (score ``inf``).
        """
        sparse = self.bm25_scores(tokenize(query))
        ranked_ids = _fuse(sparse, dense_scores) if dense_scores else sparse

        by_id = {c.chunk_id: c for c in self.chunks}
        selected: dict[str, float] = {}

        # 1. Mandatory clauses, hard stops and the autonomy posture.
        for chunk in self.chunks:
            if chunk.is_mandatory:
                selected[chunk.chunk_id] = math.inf

        # 2. The item's own category section, so its caps are always present.
        category_key = category.strip().lower()
        for chunk in self.chunks:
            if category_key and chunk.category_hint == category_key:
                selected.setdefault(chunk.chunk_id, math.inf)

        # 3. Top-scoring remainder, up to top_k chunks beyond the mandatory set.
        #    Another category's clauses are filtered out even when they score
        #    well lexically: a SaaS invoice has no business carrying the meal
        #    per-attendee cap into the prompt, and every clause that survives
        #    here is one the model has to reason about.
        added = 0
        for chunk_id, score in sorted(
            ranked_ids.items(), key=lambda kv: kv[1], reverse=True
        ):
            if added >= top_k:
                break
            if chunk_id in selected:
                continue
            hint = by_id[chunk_id].category_hint if chunk_id in by_id else None
            if hint is not None and category_key and hint != category_key:
                continue
            selected[chunk_id] = score
            added += 1

        results = [(by_id[cid], score) for cid, score in selected.items() if cid in by_id]
        results.sort(key=lambda pair: (pair[1] == math.inf, pair[1]), reverse=True)
        return results


def _fuse(
    sparse: dict[str, float], dense: dict[str, float], k: int = 60
) -> dict[str, float]:
    """Reciprocal Rank Fusion of two score dictionaries.

    RRF combines rankings rather than raw scores, which matters here because BM25
    scores and cosine similarities are not on the same scale.
    """
    fused: dict[str, float] = {}
    for scores in (sparse, dense):
        for rank, (chunk_id, _score) in enumerate(
            sorted(scores.items(), key=lambda kv: kv[1], reverse=True), start=1
        ):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return fused


def build_query(submission: dict[str, Any]) -> str:
    """Turn a submission into a retrieval query.

    Includes the facts the policy is written in terms of, category, vendor
    status, currency, receipt, amount band, so the query matches clause wording
    rather than only the free-text notes.
    """
    amount = Decimal(str(submission.get("amount_usd", 0) or 0))
    category = str(submission.get("category", "") or "").strip().lower()
    # Attendee wording only belongs in the query for categories the policy
    # discusses attendees for. Unconditionally, it makes every attendee-less SaaS
    # invoice match the meal per-attendee clause.
    attendee_hint = ""
    if category == "meals":
        attendee_hint = (
            "attendee count" if submission.get("attendees") else "attendee count missing"
        )
    parts: list[str] = [
        category,
        str(submission.get("vendor", "")),
        "new unknown vendor" if not submission.get("vendor_known") else "known vendor",
        "missing receipt" if not submission.get("receipt_present") else "receipt attached",
        f"{submission.get('currency', 'USD')} currency conversion"
        if str(submission.get("currency", "USD")).upper() != "USD"
        else "",
        "math does not reconcile" if not submission.get("math_ok", True) else "",
        attendee_hint,
        f"amount {amount}",
        "high value capital expense" if amount > 1000 else "",
        "duplicate invoice" if submission.get("is_duplicate") else "",
        str(submission.get("notes") or ""),
        " ".join(
            str(item.get("description", ""))
            for item in submission.get("line_items", [])
            if isinstance(item, dict)
        ),
    ]
    return " ".join(p for p in parts if p).strip()


# ── Process-level index cache ───────────────────────────────────────────────

_index: PolicyIndex | None = None


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def get_index(policy_text: str, version: int) -> PolicyIndex:
    """Return the index, rebuilding it when the document changes.

    Keyed on a content digest rather than the version number alone, so an
    out-of-band edit to the mounted file is also picked up.
    """
    global _index
    digest = digest_of(policy_text)
    if _index is None or _index.digest != digest:
        logger.info("Building policy index (version=%s, digest=%s)", version, digest)
        _index = PolicyIndex(chunk_policy(policy_text), version, digest)
    return _index


def reset_index() -> None:
    """Drop the cached index (used by tests and the reindex endpoint)."""
    global _index
    _index = None


def format_clauses(results: list[tuple[PolicyChunk, float]]) -> str:
    """Render retrieved clauses for the prompt, newest-relevance first."""
    lines: list[str] = []
    for chunk, score in results:
        marker = "ALWAYS APPLIES" if score == math.inf else f"relevance {score:.2f}"
        label = chunk.rule_id or chunk.section
        lines.append(f"- [{label}] ({marker}) {chunk.text}")
    return "\n".join(lines)


def retrieved_rule_ids(results: list[tuple[PolicyChunk, float]]) -> list[str]:
    """The rule ids that were put in front of the model (stored for audit)."""
    seen: list[str] = []
    for chunk, _score in results:
        if chunk.rule_id and chunk.rule_id not in seen:
            seen.append(chunk.rule_id)
    return seen
