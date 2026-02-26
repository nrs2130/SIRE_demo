"""
Azure AI Search client for SIRE Voice Agent.

Multi-strategy search with aggregated confidence scoring:
  1. Exact / standard search    (highest weight)
  2. Fuzzy search (~1 / ~2)     (handles typos & STT misspellings)
  3. Phonetic search            (Double Metaphone, Beider-Morse, Soundex)
  4. Number ↔ word normalization (1 → one, two → 2, etc.)

Each strategy returns results with Azure's @search.score.  We run them in
parallel, merge by document key, and compute a weighted aggregate score
normalised to 0–100 so the caller (and the voice model) can judge match
quality at a glance.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from config import SearchConfig

logger = logging.getLogger(__name__)

# ── Number ↔ word mapping ──────────────────────────────────────────────────
_NUM_TO_WORD: dict[str, str] = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten", "11": "eleven", "12": "twelve", "13": "thirteen",
    "14": "fourteen", "15": "fifteen", "16": "sixteen", "17": "seventeen",
    "18": "eighteen", "19": "nineteen", "20": "twenty",
}
_WORD_TO_NUM = {v: k for k, v in _NUM_TO_WORD.items()}


def _normalize_numbers(text: str) -> str:
    """Convert digits → words and words → digits, returning both variants ORed."""
    tokens = text.split()
    alt_tokens: list[str] = []
    changed = False
    for t in tokens:
        tl = t.lower()
        if tl in _NUM_TO_WORD:
            alt_tokens.append(_NUM_TO_WORD[tl])
            changed = True
        elif tl in _WORD_TO_NUM:
            alt_tokens.append(_WORD_TO_NUM[tl])
            changed = True
        else:
            alt_tokens.append(t)
    if changed:
        return " ".join(alt_tokens)
    return ""


# ── Scoring weights ────────────────────────────────────────────────────────
_W_TARGETED = 2.0    # field-targeted AND match (FirstName + LastName)
_W_EXACT    = 1.0    # standard / exact text match
_W_NORM     = 0.85   # number-normalised query match
_W_FUZZY    = 0.70   # fuzzy (~1/~2 edit distance)
_W_PHONETIC = 0.50   # phonetic match (Double Metaphone)
_W_PHON_SX  = 0.40   # phonetic match (Soundex)
_W_PHON_BM  = 0.35   # phonetic match (Beider-Morse — high false-positive rate)


# ── Confidence gap thresholds ──────────────────────────────────────────────
# If the gap between #1 and #2 scores exceeds these, mark as "confident"
# (the voice model can skip disambiguation and auto-confirm).
_CONFIDENCE_GAP_ABS = 25     # minimum absolute gap (#1 - #2) in points
_CONFIDENCE_MIN_SCORE = 70   # #1 must be at least this score


class SIRESearchClient:
    """Async multi-strategy Azure AI Search client with aggregated scoring."""

    def __init__(self, cfg: SearchConfig) -> None:
        self._cfg = cfg
        self._base = cfg.endpoint.rstrip("/")
        self._headers = {
            "api-key": cfg.api_key,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search_group(self, query: str, top: int = 5) -> list[dict[str, Any]]:
        """Multi-strategy search on group-slot-mapping-index."""
        select = "GroupID,GroupName,AlternateName1,AlternateName2,AlternateName3"
        key_field = "GroupID"

        # Standard fields use hospital_group_analyzer (handles word delimiters, shingles)
        std_fields = "GroupName,AlternateName1,AlternateName2,AlternateName3"
        # Normalized fields for number normalization
        norm_fields = "GroupName_normalized,AlternateName1_normalized,AlternateName2_normalized,AlternateName3_normalized,AllNormalizedNames"
        # Edge-ngram field for prefix matching
        edge_fields = "GroupName_edge"

        internal_top = max(top * 3, 10)  # gather more candidates per strategy
        strategies = self._build_group_strategies(query, std_fields, norm_fields, edge_fields, select, internal_top)
        return await self._multi_search(self._cfg.group_index, strategies, key_field, top)

    async def search_user(self, query: str, top: int = 5) -> list[dict[str, Any]]:
        """Multi-strategy search on user-slot-mapping-index."""
        select = "id,FirstName,LastName"
        key_field = "id"

        # Standard name fields
        std_fields = "FirstName,LastName,FullName"
        # Phonetic fields (Double Metaphone — best for English names)
        phonetic_fields = "FirstName_phonetic,LastName_phonetic,FullName_phonetic"
        # Soundex fields (complementary phonetic algorithm)
        soundex_fields = "FirstName_phonetic_soundex,LastName_phonetic_soundex"
        # Beider-Morse fields (good for non-English origin names)
        bm_fields = "FirstName_phonetic_bm,LastName_phonetic_bm"

        internal_top = max(top * 3, 10)  # gather more candidates per strategy
        strategies = self._build_user_strategies(
            query, std_fields, phonetic_fields, soundex_fields, bm_fields, select, internal_top
        )
        return await self._multi_search(self._cfg.user_index, strategies, key_field, top)

    # ------------------------------------------------------------------
    # Strategy builders
    # ------------------------------------------------------------------

    def _build_group_strategies(
        self, query: str, std_fields: str, norm_fields: str,
        edge_fields: str, select: str, top: int,
    ) -> list[tuple[dict[str, Any], float, str]]:
        """Return [(body, weight, label), ...] for group search."""
        strategies: list[tuple[dict[str, Any], float, str]] = []

        # 1. Exact / standard — searchMode:all so ALL query terms must match
        strategies.append((
            {"search": query, "searchFields": std_fields, "select": select,
             "top": top, "queryType": "simple", "searchMode": "all"},
            _W_EXACT, "exact-all",
        ))

        # 1b. Exact any-mode (catch partial matches at lower weight)
        strategies.append((
            {"search": query, "searchFields": std_fields, "select": select,
             "top": top, "queryType": "simple"},
            _W_EXACT * 0.5, "exact-any",
        ))

        # 2. Fuzzy (full Lucene ~1) — searchMode:all
        #    Escape "or"/"and"/"not" to prevent Lucene boolean interpretation
        fuzzy_terms = []
        for t in query.split():
            if len(t) <= 1:
                continue
            escaped = f'"{t}"' if t.lower() in ("or", "and", "not") else f"{t}~1"
            fuzzy_terms.append(escaped)
        fuzzy_q = " ".join(fuzzy_terms)
        if fuzzy_q:
            strategies.append((
                {"search": fuzzy_q, "searchFields": std_fields, "select": select,
                 "top": top, "queryType": "full", "searchMode": "all"},
                _W_FUZZY, "fuzzy",
            ))

        # 3. Number normalisation — searchMode:all + any fallback
        norm_q = _normalize_numbers(query)
        if norm_q:
            strategies.append((
                {"search": norm_q, "searchFields": std_fields, "select": select,
                 "top": top, "queryType": "simple", "searchMode": "all"},
                _W_NORM, "normalised",
            ))
            # Also any-mode at lower weight (for cases where "all" is too strict)
            strategies.append((
                {"search": norm_q, "searchFields": std_fields, "select": select,
                 "top": top, "queryType": "simple"},
                _W_NORM * 0.6, "normalised-any",
            ))
            # Also fuzzy on normalised — escape boolean keywords
            fuzzy_norm_terms = []
            for t in norm_q.split():
                if len(t) <= 1:
                    continue
                escaped = f'"{t}"' if t.lower() in ("or", "and", "not") else f"{t}~1"
                fuzzy_norm_terms.append(escaped)
            fuzzy_norm = " ".join(fuzzy_norm_terms)
            if fuzzy_norm:
                strategies.append((
                    {"search": fuzzy_norm, "searchFields": std_fields, "select": select,
                     "top": top, "queryType": "full", "searchMode": "all"},
                    _W_FUZZY * 0.9, "fuzzy-norm",
                ))

        # 4. Normalised fields (keyword_lowercase with char mappings) — searchMode:all
        strategies.append((
            {"search": query.lower(), "searchFields": norm_fields, "select": select,
             "top": top, "queryType": "simple", "searchMode": "all"},
            _W_EXACT * 0.9, "norm-fields",
        ))

        return strategies

    def _build_user_strategies(
        self, query: str, std_fields: str, phonetic_fields: str,
        soundex_fields: str, bm_fields: str, select: str, top: int,
    ) -> list[tuple[dict[str, Any], float, str]]:
        """Return [(body, weight, label), ...] for user search."""
        strategies: list[tuple[dict[str, Any], float, str]] = []

        # 1. Exact / standard
        strategies.append((
            {"search": query, "searchFields": std_fields, "select": select,
             "top": top, "queryType": "simple"},
            _W_EXACT, "exact",
        ))

        # 2. Fuzzy (~2 for names ≥ 4 chars — STT often produces 2-edit-distance errors)
        fuzzy_q = " ".join(
            f"{t}~2" if len(t) >= 4 else f"{t}~1"
            for t in query.split() if len(t) > 1
        )
        if fuzzy_q:
            strategies.append((
                {"search": fuzzy_q, "searchFields": std_fields, "select": select,
                 "top": top, "queryType": "full"},
                _W_FUZZY, "fuzzy",
            ))

        # 3. Field-targeted AND (for multi-word queries: "Ariah Hoebeke")
        #    Splits query into FirstName + LastName and requires BOTH to match.
        words = query.split()
        if len(words) >= 2:
            first_part = words[0]
            last_part = " ".join(words[1:])
            # Exact field-targeted
            targeted_q = f"FirstName:{first_part} AND LastName:{last_part}"
            strategies.append((
                {"search": targeted_q, "searchFields": std_fields,
                 "select": select, "top": top, "queryType": "full"},
                _W_TARGETED, "targeted",
            ))
            # Fuzzy field-targeted
            f_first = f"{first_part}~2" if len(first_part) >= 4 else f"{first_part}~1"
            f_last = f"{last_part}~1"
            targeted_fuzzy_q = f"FirstName:{f_first} AND LastName:{f_last}"
            strategies.append((
                {"search": targeted_fuzzy_q, "searchFields": std_fields,
                 "select": select, "top": top, "queryType": "full"},
                _W_TARGETED * 0.8, "targeted-fuzzy",
            ))
            # Phonetic field-targeted (DM)
            targeted_phon_q = f"FirstName_phonetic:{first_part} AND LastName_phonetic:{last_part}"
            strategies.append((
                {"search": targeted_phon_q,
                 "searchFields": "FirstName_phonetic,LastName_phonetic",
                 "select": select, "top": top, "queryType": "full"},
                _W_TARGETED * 0.6, "targeted-phonetic",
            ))

        # 4. Phonetic — Double Metaphone (best general-purpose)
        strategies.append((
            {"search": query, "searchFields": phonetic_fields, "select": select,
             "top": top, "queryType": "simple"},
            _W_PHONETIC, "phonetic-dm",
        ))

        # 5. Phonetic — Soundex
        strategies.append((
            {"search": query, "searchFields": soundex_fields, "select": select,
             "top": top, "queryType": "simple"},
            _W_PHON_SX, "phonetic-soundex",
        ))

        # 6. Phonetic — Beider-Morse (high false-positive rate, low weight)
        strategies.append((
            {"search": query, "searchFields": bm_fields, "select": select,
             "top": top, "queryType": "simple"},
            _W_PHON_BM, "phonetic-bm",
        ))

        return strategies

    # ------------------------------------------------------------------
    # Multi-strategy execution + score aggregation
    # ------------------------------------------------------------------

    async def _multi_search(
        self,
        index: str,
        strategies: list[tuple[dict[str, Any], float, str]],
        key_field: str,
        top: int,
    ) -> list[dict[str, Any]]:
        """
        Run all strategies in parallel, merge results by key, compute
        aggregated score (0–100), and return top-N sorted by score.
        """
        url = (
            f"{self._base}/indexes/{index}"
            f"/docs/search?api-version={self._cfg.api_version}"
        )

        # Fire all strategy requests in parallel
        async with httpx.AsyncClient(timeout=15) as client:
            responses: list[tuple[httpx.Response, float, str]] = []
            # Use asyncio.gather for parallel execution
            import asyncio
            tasks = [
                client.post(url, headers=self._headers, json=body)
                for body, _, _ in strategies
            ]
            resps = await asyncio.gather(*tasks, return_exceptions=True)

            for (body, weight, label), resp in zip(strategies, resps):
                if isinstance(resp, Exception):
                    logger.warning("Strategy %s failed: %s", label, resp)
                    continue
                if resp.status_code != 200:
                    logger.warning("Strategy %s returned %d", label, resp.status_code)
                    continue
                responses.append((resp, weight, label))

        # ── Reciprocal Rank Fusion (RRF) ──────────────────────────────────
        # Each strategy produces a ranked list.  For document at rank r in
        # strategy s with weight w:   rrf_contribution = w / (k + r)
        # The constant k (default 60) dampens the influence of low-ranked
        # results.  Summing across strategies naturally rewards documents
        # that appear in many lists and in high positions.
        RRF_K = 10  # low K = sharper rank sensitivity (rank #1 vs #2 matters)

        # Build per-strategy ranked lists: [(key, doc_data, raw_score), ...]
        strategy_ranks: list[tuple[float, str, list[tuple[str, dict, float]]]] = []

        for resp, weight, label in responses:
            data = resp.json()
            docs = data.get("value", [])
            if not docs:
                continue
            ranked: list[tuple[str, dict, float]] = []
            for doc in docs:
                key = doc.get(key_field, "")
                if not key:
                    continue
                ranked.append((key, doc, doc.get("@search.score", 0)))
            # Already sorted by Azure, but ensure descending by raw score
            ranked.sort(key=lambda x: x[2], reverse=True)
            strategy_ranks.append((weight, label, ranked))

        # Merge: accumulate RRF score per document key
        merged: dict[str, dict[str, Any]] = {}
        # {key: {"doc": {...}, "rrf": float, "strat_detail": {label: rank}}}

        for weight, label, ranked in strategy_ranks:
            for rank_0, (key, doc, raw_score) in enumerate(ranked):
                rank = rank_0 + 1  # 1-based
                rrf_contrib = weight / (RRF_K + rank)

                if key not in merged:
                    clean = {k: v for k, v in doc.items()
                             if not k.startswith("@search.")}
                    merged[key] = {"doc": clean, "rrf": 0.0, "strat_detail": {}}

                merged[key]["rrf"] += rrf_contrib
                merged[key]["strat_detail"][label] = rank

        # Normalise RRF scores to 0-100 (best result = 100)
        max_rrf = max((e["rrf"] for e in merged.values()), default=1.0) or 1.0

        results: list[dict[str, Any]] = []
        for key, entry in merged.items():
            agg_score = (entry["rrf"] / max_rrf) * 100.0

            doc = entry["doc"]
            doc["_match_score"] = round(agg_score, 1)
            # Show which strategies found this result and at what rank
            doc["_match_strategies"] = ", ".join(
                f"{label}(#{rank})"
                for label, rank in sorted(
                    entry["strat_detail"].items(), key=lambda x: x[1]
                )
            )
            doc["_strategy_count"] = len(entry["strat_detail"])
            results.append(doc)

        # Sort by aggregate score descending
        results.sort(key=lambda d: d.get("_match_score", 0), reverse=True)

        # ── Confidence detection ───────────────────────────────────────
        # If the top result is far enough ahead of #2, mark as confident
        if len(results) >= 2:
            gap = results[0].get("_match_score", 0) - results[1].get("_match_score", 0)
            top_score = results[0].get("_match_score", 0)
            confident = gap >= _CONFIDENCE_GAP_ABS and top_score >= _CONFIDENCE_MIN_SCORE
        elif len(results) == 1:
            confident = results[0].get("_match_score", 0) >= _CONFIDENCE_MIN_SCORE
        else:
            confident = False

        # Tag all results with confidence metadata
        for i, r in enumerate(results):
            r["_confident"] = confident and i == 0  # only #1 is confident
        if results:
            gap_val = round(results[0].get("_match_score", 0) - (results[1].get("_match_score", 0) if len(results) > 1 else 0), 1)
            results[0]["_gap_to_next"] = gap_val

        logger.info(
            "Multi-search on %s: %d unique results from %d strategies",
            index, len(results), len(strategies),
        )
        for r in results[:top]:
            logger.info(
                "  score=%.1f  key=%s  strategies=%s",
                r.get("_match_score", 0), r.get(key_field, "?"), r.get("_match_strategies", ""),
            )

        return results[:top]
