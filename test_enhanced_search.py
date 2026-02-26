"""Quick test of the enhanced multi-strategy search client."""

import asyncio
import sys
from dotenv import load_dotenv

load_dotenv()

from config import AppConfig
from search_client import SIRESearchClient

cfg = AppConfig.from_env()
client = SIRESearchClient(cfg.search)

TEST_CASES = [
    # (index_type, query, expected_match_description)
    ("group", "One and Two East Nurses", "Should find One and Two East Nurses at #1"),
    ("group", "PICU Team Three NP", "Should rank Three above Two"),
    ("user",  "Ariah hoebeke",      "Should find Ariah Hoebeke at #1 (field-targeted)"),
    ("user",  "Aryah",              "Should find Aria via phonetic match"),
    ("user",  "Barbara",            "Should find Barbara (baseline)"),
    ("group", "1 or 2 East nurses", "Should find One or Two East Nurses via normalization"),
    ("group", "ICU unit",           "Should find ICU (baseline)"),
]


async def main():
    print("=" * 80)
    print("ENHANCED SEARCH TEST — Multi-Strategy with Aggregated Scoring")
    print("=" * 80)

    for idx_type, query, description in TEST_CASES:
        print(f"\n{'─' * 70}")
        print(f"  TEST: {description}")
        print(f"  Query: '{query}'  |  Index: {idx_type}")
        print(f"{'─' * 70}")

        if idx_type == "user":
            results = await client.search_user(query, top=5)
        else:
            results = await client.search_group(query, top=5)

        if not results:
            print("  ⚠  NO RESULTS")
            continue

        top = results[0]
        confident = top.get("_confident", False)
        gap = top.get("_gap_to_next", 0)
        flag = "✅ AUTO-CONFIRM" if confident else "⚠️  DISAMBIGUATE"
        print(f"  {flag}  (gap to #2 = {gap} pts)")

        for i, r in enumerate(results, 1):
            score = r.get("_match_score", "?")
            strategies = r.get("_match_strategies", "")
            if idx_type == "user":
                print(f"  {i}. [{r.get('id', '?')}] {r.get('FirstName', '')} {r.get('LastName', '')}  "
                      f"score={score}  [{strategies}]")
            else:
                print(f"  {i}. [{r.get('GroupID', '?')}] {r.get('GroupName', '?')}  "
                      f"score={score}  [{strategies}]")

    print(f"\n{'=' * 80}")
    print("DONE")


if __name__ == "__main__":
    asyncio.run(main())
