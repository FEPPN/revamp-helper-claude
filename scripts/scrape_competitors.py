#!/usr/bin/env python3
"""Downloads each competitor page found by find_pages.py and extracts its
H1/H2/H3 structure, in the exact JSON shape build_report.py's Competitors
tab expects. Fully automatic, no LLM — the "summary" is the first
meaningful paragraph on the page (truncated), not an AI-written summary.
That's the one honest trade-off of removing the agent from this step.

Usage:
  python scrape_competitors.py --pages pages.json --out competitors.json
"""

import argparse
import json
import sys

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
}


def extract_structure(html):
    soup = BeautifulSoup(html, "html.parser")

    h1_tag = soup.find("h1")
    h1 = h1_tag.get_text(strip=True) if h1_tag else "(H1 introuvable)"

    # First substantial paragraph as a stand-in for a summary (no LLM here).
    summary = ""
    for p in soup.find_all("p"):
        text = p.get_text(strip=True)
        if len(text) > 60:
            summary = text[:300] + ("…" if len(text) > 300 else "")
            break
    if not summary:
        summary = "(résumé automatique indisponible — aucun paragraphe assez long trouvé)"

    # Walk H2/H3 in document order, grouping H3s under the preceding H2.
    structure = []
    current = None
    for tag in soup.find_all(["h2", "h3"]):
        text = tag.get_text(strip=True)
        if not text:
            continue
        if tag.name == "h2":
            current = {"h2": text, "h3": []}
            structure.append(current)
        elif tag.name == "h3" and current is not None:
            current["h3"].append(text)

    return h1, summary, structure


def main():
    parser = argparse.ArgumentParser(description="Scrape H1-H3 structure of competitor pages")
    parser.add_argument("--pages", required=True, help="Output of find_pages.py")
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.pages, encoding="utf-8") as f:
        pages = json.load(f)

    results = []
    for comp in pages.get("competitors", []):
        if comp.get("no_dedicated_avis_page") or not comp.get("url"):
            results.append({
                "site": comp["site"],
                "url": comp.get("url") or "",
                "h1": "(aucune page dédiée trouvée)",
                "structure": [],
                "summary": "Aucune page dédiée trouvée pour ce mot-clé sur ce site — "
                            "vérifier manuellement avant de conclure à une absence totale.",
                "no_dedicated_avis_page": True,
            })
            continue

        try:
            r = requests.get(comp["url"], headers=HEADERS, timeout=20)
            r.raise_for_status()
            h1, summary, structure = extract_structure(r.text)
            results.append({
                "site": comp["site"],
                "url": comp["url"],
                "h1": h1,
                "structure": structure,
                "summary": summary,
            })
            print(f"{comp['site']}: OK — H1 = \"{h1}\", {len(structure)} H2")
        except requests.RequestException as e:
            results.append({
                "site": comp["site"],
                "url": comp["url"],
                "h1": "(échec du téléchargement)",
                "structure": [],
                "summary": f"Erreur lors du téléchargement : {e}",
                "no_dedicated_avis_page": True,
            })
            print(f"{comp['site']}: ÉCHEC — {e}", file=sys.stderr)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Saved: {args.out}")


if __name__ == "__main__":
    main()
