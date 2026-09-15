#!/usr/bin/env python3
"""Fetches live Google SERP data (FR) via SerpAPI and writes it in the exact
JSON shape build_report.py's SERP tab expects. Fully automatic, no LLM
involved — the per-result "note" is SerpAPI's own snippet, not an
AI-written summary.

Usage:
  python fetch_serp.py --keyword "avis edf" --out serp.json
"""

import argparse
import json
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SERPAPI_URL = "https://serpapi.com/search.json"


def fetch_serp(keyword, api_key):
    params = {
        "engine": "google",
        "q": keyword,
        "gl": "fr",
        "hl": "fr",
        "api_key": api_key,
    }
    r = requests.get(SERPAPI_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def build_serp_json(raw, keyword):
    organic = []
    for item in raw.get("organic_results", [])[:10]:
        organic.append({
            "position": item.get("position"),
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "note": item.get("snippet", ""),
        })

    paa = [q.get("question", "") for q in raw.get("related_questions", []) if q.get("question")]

    related_searches = [q.get("query", "") for q in raw.get("related_searches", []) if q.get("query")]

    ai_overview = raw.get("ai_overview")
    ai_present = bool(ai_overview)
    ai_summary = ""
    if ai_overview:
        blocks = ai_overview.get("text_blocks", [])
        parts = []
        for b in blocks:
            if b.get("type") == "paragraph" and b.get("snippet"):
                parts.append(b["snippet"])
            elif b.get("type") == "list":
                for li in b.get("list", []):
                    if li.get("snippet"):
                        parts.append("- " + li["snippet"])
        ai_summary = "\n".join(parts) if parts else "(AI Overview présent mais texte non extrait par SerpAPI)"

    kg_present = bool(raw.get("knowledge_graph"))

    return {
        "keyword": keyword,
        "market": "FR (google.fr)",
        "organic": organic,
        "paa": paa,
        "related_searches": related_searches,
        "ai_overview_present": ai_present,
        "ai_overview_summary": ai_summary,
        "knowledge_graph_present": kg_present,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch live Google SERP (FR) via SerpAPI")
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--api-key", help="Overrides SERPAPI_API_KEY env var")
    args = parser.parse_args()

    api_key = args.api_key or os.environ.get("SERPAPI_API_KEY")
    if not api_key:
        sys.exit("Missing SerpAPI key: set SERPAPI_API_KEY in .env or pass --api-key")

    raw = fetch_serp(args.keyword, api_key)
    data = build_serp_json(raw, args.keyword)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"Saved: {args.out} ({len(data['organic'])} résultats organiques, "
          f"{len(data['paa'])} PAA, {len(data['related_searches'])} recherches associées)")


if __name__ == "__main__":
    main()
