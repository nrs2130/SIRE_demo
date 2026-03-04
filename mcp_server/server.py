#!/usr/bin/env python3
"""
SIRE MCP Server — Multi-strategy Azure AI Search exposed as MCP tools.

This server mirrors the search logic from the main SIRE pipeline
(search_client.py) but exposes it through the Model Context Protocol (MCP)
so any MCP-compatible client can invoke the same searches.

Run standalone:
    python -m mcp_server.server          # stdio transport (default)
    python -m mcp_server.server --sse    # SSE transport on port 8080

Environment variables (via .env or shell):
    AZURE_SEARCH_ENDPOINT       – AI Search service URL
    AZURE_SEARCH_API_KEY        – admin or query key
    AZURE_SEARCH_GROUP_INDEX    – index name (default: group-slot-mapping-index)
    AZURE_SEARCH_USER_INDEX     – index name (default: user-slot-mapping-index)
    AZURE_SEARCH_API_VERSION    – API version (default: 2024-07-01)
    MCP_SERVER_PORT             – SSE port (default: 8080)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from dotenv import load_dotenv
import httpx
from mcp.server.fastmcp import FastMCP

# ── Resolve .env from project root (one level up from mcp_server/) ─────────
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_project_root, ".env"), override=True)

# ── Import the search engine from the parent project ───────────────────────
# We add the project root to sys.path so we can import search_client & config
# without modifying the original files.
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from config import SearchConfig          # noqa: E402
from search_client import SIRESearchClient  # noqa: E402

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sire-mcp")

# ── Create MCP server ─────────────────────────────────────────────────────
mcp = FastMCP(
    "SIRE Search",
    instructions=(
        "Multi-strategy Azure AI Search for the SIRE Voice Agent. "
        "Searches users and groups with exact, fuzzy, phonetic, and "
        "number-normalised strategies, returning RRF-aggregated scores."
    ),
)

# ── Lazy-init search client (created on first tool call) ──────────────────
_search_client: SIRESearchClient | None = None


def _get_client() -> SIRESearchClient:
    """Lazily initialise the search client from env vars."""
    global _search_client
    if _search_client is None:
        cfg = SearchConfig.from_env()
        _search_client = SIRESearchClient(cfg)
        logger.info(
            "Search client initialised: endpoint=%s  group_index=%s  user_index=%s",
            cfg.endpoint, cfg.group_index, cfg.user_index,
        )
    return _search_client


# ═══════════════════════════════════════════════════════════════════════════
# MCP TOOLS
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool()
async def search_group(query: str, top: int = 5) -> str:
    """Search the group/department directory by name.

    Uses multiple strategies in parallel (exact, fuzzy, number-normalisation,
    normalised-field matching) and returns results ranked by RRF-aggregated
    confidence score (0-100).

    Args:
        query: The group name or partial name to search for
               (e.g. "One and Two East Nurses", "PICU Team Three NP", "ICU unit").
        top:   Maximum number of results to return (default 5).

    Returns:
        JSON array of matching groups, each with:
        - GroupID, GroupName, AlternateName1-3
        - _match_score (0-100)
        - _confident (bool) — true if the top match is decisively ahead
        - _match_strategies — which search strategies found this result
        - _gap_to_next — score gap between #1 and #2 (on top result only)
    """
    client = _get_client()
    results = await client.search_group(query, top=top)
    logger.info("search_group query=%r → %d results", query, len(results))
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
async def search_user(query: str, top: int = 5) -> str:
    """Search the user/person directory by name.

    Uses multiple strategies in parallel (exact, fuzzy with edit-distance 2,
    field-targeted FirstName+LastName, Double Metaphone phonetic, Soundex,
    Beider-Morse) and returns results ranked by RRF-aggregated confidence
    score (0-100).

    Args:
        query: The person's name — first, last, or full name
               (e.g. "Ariah Hoebeke", "Barbara", "Aryah").
        top:   Maximum number of results to return (default 5).

    Returns:
        JSON array of matching users, each with:
        - id, FirstName, LastName
        - _match_score (0-100)
        - _confident (bool) — true if the top match is decisively ahead
        - _match_strategies — which search strategies found this result
        - _gap_to_next — score gap between #1 and #2 (on top result only)
    """
    client = _get_client()
    results = await client.search_user(query, top=top)
    logger.info("search_user query=%r → %d results", query, len(results))
    return json.dumps(results, indent=2, default=str)


@mcp.tool()
async def search_any(query: str, top: int = 5) -> str:
    """Search BOTH user and group directories simultaneously.

    Runs search_user and search_group in parallel and returns combined results,
    labelled by index. Useful when the caller doesn't know whether the entity
    is a person or a group.

    Args:
        query: Name to search for across both indexes.
        top:   Maximum results per index (default 5).

    Returns:
        JSON object with "users" and "groups" arrays.
    """
    client = _get_client()
    user_results, group_results = await asyncio.gather(
        client.search_user(query, top=top),
        client.search_group(query, top=top),
    )
    combined = {
        "query": query,
        "users": user_results,
        "groups": group_results,
        "user_count": len(user_results),
        "group_count": len(group_results),
    }
    logger.info(
        "search_any query=%r → %d users, %d groups",
        query, len(user_results), len(group_results),
    )
    return json.dumps(combined, indent=2, default=str)


@mcp.tool()
async def search_diagnostics(query: str, index_type: str = "user") -> str:
    """Run a search with full diagnostic output showing per-strategy results.

    This tool is for debugging/comparison — it shows exactly which strategies
    contributed to each result and their individual ranks, so you can compare
    the MCP server's aggregation against other approaches.

    Args:
        query:      Name to search for.
        index_type: "user" or "group" (default "user").

    Returns:
        JSON object with:
        - results: scored results (same as search_user/search_group)
        - strategies_used: list of strategy labels and weights
        - scoring_config: the weight and confidence parameters in effect
    """
    client = _get_client()

    if index_type.lower() == "group":
        results = await client.search_group(query, top=10)
        strategies_info = _describe_group_strategies()
    else:
        results = await client.search_user(query, top=10)
        strategies_info = _describe_user_strategies()

    # Import scoring constants for diagnostics
    from search_client import (
        _W_TARGETED, _W_EXACT, _W_NORM, _W_FUZZY,
        _W_PHONETIC, _W_PHON_SX, _W_PHON_BM,
        _CONFIDENCE_GAP_ABS, _CONFIDENCE_MIN_SCORE,
    )

    diagnostic = {
        "query": query,
        "index_type": index_type,
        "results": results,
        "result_count": len(results),
        "strategies_used": strategies_info,
        "scoring_config": {
            "weights": {
                "targeted_AND": _W_TARGETED,
                "exact": _W_EXACT,
                "number_normalised": _W_NORM,
                "fuzzy": _W_FUZZY,
                "phonetic_double_metaphone": _W_PHONETIC,
                "phonetic_soundex": _W_PHON_SX,
                "phonetic_beider_morse": _W_PHON_BM,
            },
            "confidence": {
                "min_score": _CONFIDENCE_MIN_SCORE,
                "gap_abs": _CONFIDENCE_GAP_ABS,
            },
            "rrf_k": 10,
        },
    }
    logger.info("search_diagnostics query=%r index=%s → %d results", query, index_type, len(results))
    return json.dumps(diagnostic, indent=2, default=str)


@mcp.tool()
async def get_index_info() -> str:
    """Return metadata about the configured Azure AI Search indexes.

    Shows the index names, endpoints, and searchable field layout so the
    caller understands what fields and analyzers are available.

    Returns:
        JSON object with index configuration details.
    """
    cfg = SearchConfig.from_env()
    info = {
        "endpoint": cfg.endpoint,
        "api_version": cfg.api_version,
        "indexes": {
            "group": {
                "name": cfg.group_index,
                "standard_fields": ["GroupName", "AlternateName1", "AlternateName2", "AlternateName3"],
                "normalised_fields": [
                    "GroupName_normalized", "AlternateName1_normalized",
                    "AlternateName2_normalized", "AlternateName3_normalized",
                    "AllNormalizedNames",
                ],
                "edge_ngram_fields": ["GroupName_edge"],
                "key_field": "GroupID",
                "select_fields": ["GroupID", "GroupName", "AlternateName1", "AlternateName2", "AlternateName3"],
            },
            "user": {
                "name": cfg.user_index,
                "standard_fields": ["FirstName", "LastName", "FullName"],
                "phonetic_dm_fields": ["FirstName_phonetic", "LastName_phonetic", "FullName_phonetic"],
                "phonetic_soundex_fields": ["FirstName_phonetic_soundex", "LastName_phonetic_soundex"],
                "phonetic_bm_fields": ["FirstName_phonetic_bm", "LastName_phonetic_bm"],
                "key_field": "id",
                "select_fields": ["id", "FirstName", "LastName"],
            },
        },
        "search_strategies": {
            "group": _describe_group_strategies(),
            "user": _describe_user_strategies(),
        },
    }
    return json.dumps(info, indent=2)


@mcp.tool()
async def compare_search(query: str, index_type: str = "user", top: int = 5) -> str:
    """Run each search strategy INDEPENDENTLY and show per-strategy results.

    Unlike the main search tools which merge all strategies via RRF, this tool
    runs each strategy separately and shows its raw ranked results. Useful for
    A/B-testing individual strategies and understanding which ones contribute
    most for a given query.

    Args:
        query:      Name to search for.
        index_type: "user" or "group" (default "user").
        top:        Max results per strategy (default 5).

    Returns:
        JSON object with per-strategy result lists and timing.
    """
    import time
    cfg = SearchConfig.from_env()
    client = _get_client()

    base_url = cfg.endpoint.rstrip("/")
    headers = {"api-key": cfg.api_key, "Content-Type": "application/json"}

    if index_type.lower() == "group":
        index = cfg.group_index
        key_field = "GroupID"
        select = "GroupID,GroupName,AlternateName1,AlternateName2,AlternateName3"
        std_fields = "GroupName,AlternateName1,AlternateName2,AlternateName3"
        norm_fields = "GroupName_normalized,AlternateName1_normalized,AlternateName2_normalized,AlternateName3_normalized,AllNormalizedNames"
        edge_fields = "GroupName_edge"
        strategies = client._build_group_strategies(query, std_fields, norm_fields, edge_fields, select, top)
    else:
        index = cfg.user_index
        key_field = "id"
        select = "id,FirstName,LastName"
        std_fields = "FirstName,LastName,FullName"
        phonetic_fields = "FirstName_phonetic,LastName_phonetic,FullName_phonetic"
        soundex_fields = "FirstName_phonetic_soundex,LastName_phonetic_soundex"
        bm_fields = "FirstName_phonetic_bm,LastName_phonetic_bm"
        strategies = client._build_user_strategies(
            query, std_fields, phonetic_fields, soundex_fields, bm_fields, select, top
        )

    url = f"{base_url}/indexes/{index}/docs/search?api-version={cfg.api_version}"

    per_strategy_results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=15) as http:
        for body, weight, label in strategies:
            t0 = time.perf_counter()
            try:
                resp = await http.post(url, headers=headers, json=body)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                if resp.status_code != 200:
                    per_strategy_results.append({
                        "strategy": label,
                        "weight": weight,
                        "error": f"HTTP {resp.status_code}",
                        "elapsed_ms": round(elapsed_ms, 1),
                    })
                    continue
                data = resp.json()
                docs = data.get("value", [])
                cleaned = []
                for doc in docs[:top]:
                    clean = {k: v for k, v in doc.items() if not k.startswith("@search.")}
                    clean["_raw_score"] = doc.get("@search.score", 0)
                    cleaned.append(clean)
                per_strategy_results.append({
                    "strategy": label,
                    "weight": weight,
                    "result_count": len(cleaned),
                    "results": cleaned,
                    "elapsed_ms": round(elapsed_ms, 1),
                })
            except Exception as e:
                per_strategy_results.append({
                    "strategy": label,
                    "weight": weight,
                    "error": str(e),
                    "elapsed_ms": 0,
                })

    output = {
        "query": query,
        "index_type": index_type,
        "strategy_count": len(strategies),
        "per_strategy": per_strategy_results,
    }
    logger.info("compare_search query=%r → %d strategies evaluated", query, len(strategies))
    return json.dumps(output, indent=2, default=str)


# ═══════════════════════════════════════════════════════════════════════════
# MCP RESOURCES
# ═══════════════════════════════════════════════════════════════════════════

@mcp.resource("sire://config/search")
def search_config_resource() -> str:
    """Current search configuration (endpoint, indexes, API version)."""
    cfg = SearchConfig.from_env()
    return json.dumps({
        "endpoint": cfg.endpoint,
        "group_index": cfg.group_index,
        "user_index": cfg.user_index,
        "api_version": cfg.api_version,
    }, indent=2)


@mcp.resource("sire://config/scoring")
def scoring_config_resource() -> str:
    """Scoring weights and confidence thresholds used by the search engine."""
    from search_client import (
        _W_TARGETED, _W_EXACT, _W_NORM, _W_FUZZY,
        _W_PHONETIC, _W_PHON_SX, _W_PHON_BM,
        _CONFIDENCE_GAP_ABS, _CONFIDENCE_MIN_SCORE,
    )
    return json.dumps({
        "weights": {
            "targeted_AND_match": _W_TARGETED,
            "exact_text": _W_EXACT,
            "number_normalised": _W_NORM,
            "fuzzy_edit_distance": _W_FUZZY,
            "phonetic_double_metaphone": _W_PHONETIC,
            "phonetic_soundex": _W_PHON_SX,
            "phonetic_beider_morse": _W_PHON_BM,
        },
        "confidence_thresholds": {
            "minimum_top_score": _CONFIDENCE_MIN_SCORE,
            "minimum_gap_to_second": _CONFIDENCE_GAP_ABS,
        },
        "rrf_k": 10,
        "description": (
            "Each search strategy returns ranked results. The RRF formula "
            "rrf_contrib = weight / (k + rank) is summed across strategies "
            "per document, then normalised to 0-100."
        ),
    }, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _describe_group_strategies() -> list[dict[str, Any]]:
    from search_client import _W_EXACT, _W_FUZZY, _W_NORM
    return [
        {"label": "exact-all",      "weight": _W_EXACT,       "description": "Standard search, searchMode=all (all terms must match)"},
        {"label": "exact-any",      "weight": _W_EXACT * 0.5, "description": "Standard search, searchMode=any (partial match)"},
        {"label": "fuzzy",          "weight": _W_FUZZY,       "description": "Lucene ~1 edit distance, searchMode=all"},
        {"label": "normalised",     "weight": _W_NORM,        "description": "Number→word / word→number conversion, searchMode=all"},
        {"label": "normalised-any", "weight": _W_NORM * 0.6,  "description": "Number normalisation, searchMode=any"},
        {"label": "fuzzy-norm",     "weight": _W_FUZZY * 0.9, "description": "Fuzzy on number-normalised query"},
        {"label": "norm-fields",    "weight": _W_EXACT * 0.9, "description": "Normalised fields (keyword_lowercase + char mappings), searchMode=all"},
    ]


def _describe_user_strategies() -> list[dict[str, Any]]:
    from search_client import (
        _W_TARGETED, _W_EXACT, _W_FUZZY,
        _W_PHONETIC, _W_PHON_SX, _W_PHON_BM,
    )
    return [
        {"label": "exact",            "weight": _W_EXACT,           "description": "Standard text search on FirstName, LastName, FullName"},
        {"label": "fuzzy",            "weight": _W_FUZZY,           "description": "Lucene ~2 (≥4 chars) / ~1 edit distance"},
        {"label": "targeted",         "weight": _W_TARGETED,        "description": "Field-targeted AND: FirstName:X AND LastName:Y (multi-word queries)"},
        {"label": "targeted-fuzzy",   "weight": _W_TARGETED * 0.8,  "description": "Fuzzy field-targeted AND match"},
        {"label": "targeted-phonetic","weight": _W_TARGETED * 0.6,  "description": "Phonetic field-targeted AND match (Double Metaphone)"},
        {"label": "phonetic-dm",      "weight": _W_PHONETIC,        "description": "Double Metaphone phonetic search (best general-purpose)"},
        {"label": "phonetic-soundex", "weight": _W_PHON_SX,         "description": "Soundex phonetic search"},
        {"label": "phonetic-bm",      "weight": _W_PHON_BM,         "description": "Beider-Morse phonetic search (high false-positive rate)"},
    ]


# ═══════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ═══════════════════════════════════════════════════════════════════════════

def main():
    """Run the MCP server (stdio by default, SSE with --sse flag)."""
    import argparse
    parser = argparse.ArgumentParser(description="SIRE MCP Server")
    parser.add_argument("--sse", action="store_true", help="Run with SSE transport instead of stdio")
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_SERVER_PORT", "8080")),
                        help="Port for SSE transport (default: 8080)")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.sse:
        logger.info("Starting SIRE MCP server (SSE) on port %d", args.port)
        mcp.run(transport="sse")
    else:
        logger.info("Starting SIRE MCP server (stdio)")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
