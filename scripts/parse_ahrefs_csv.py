#!/usr/bin/env python3
"""Converts an Ahrefs Keywords Explorer "Matching terms" CSV export (Terms
match, All terms) into the exact JSON shape build_report.py's Ahrefs tab
expects. Use this when there's no Ahrefs API key available — export the CSV
by hand from the Ahrefs UI (Keywords Explorer -> your seed keyword ->
Matching terms -> Terms match: All -> Export), no admin permission needed
for that, only for API keys.

Column names vary slightly across Ahrefs export versions, so this looks for
several known variants per field, case-insensitively.

Usage:
  python parse_ahrefs_csv.py --csv ahrefs_export.csv --out ahrefs_matching.json
"""

import argparse
import csv
import json
import sys

FIELD_VARIANTS = {
    "keyword": ["keyword", "keywords"],
    "volume": ["volume", "search volume", "global volume"],
    "difficulty": ["difficulty", "kd", "keyword difficulty"],
    "cpc": ["cpc"],
    "traffic_potential": ["traffic potential", "traffic_potential"],
    "parent_topic": ["parent topic", "parent_topic", "parent keyword"],
}

INTENT_VARIANTS = {
    "informational": ["informational", "intent: informational"],
    "navigational": ["navigational", "intent: navigational"],
    "commercial": ["commercial", "intent: commercial"],
    "transactional": ["transactional", "intent: transactional"],
    "branded": ["branded", "intent: branded"],
    "local": ["local", "intent: local"],
}

# Newer Ahrefs exports put all intents in one comma-separated column
# (e.g. "Informational,Commercial,Branded,Non-local") instead of one
# boolean column per intent.
COMBINED_INTENTS_VARIANTS = ["intents", "intent"]


def find_column(fieldnames_lower, variants):
    for v in variants:
        if v in fieldnames_lower:
            return fieldnames_lower[v]
    return None


def parse_intents(raw_row, intent_cols, combined_col):
    intents = {}
    for key, source_col in intent_cols.items():
        if source_col:
            val = (raw_row.get(source_col) or "").strip().lower()
            intents[key] = val not in ("", "0", "false", "no")
    if combined_col and not any(intents.values()):
        tokens = {t.strip().lower() for t in (raw_row.get(combined_col) or "").split(",")}
        for key in INTENT_VARIANTS:
            intents[key] = key in tokens
    return intents


def to_number(value):
    if value is None:
        return None
    value = str(value).strip().replace(",", "").replace("%", "")
    if value in ("", "-", "n/a"):
        return None
    try:
        return float(value) if "." in value else int(value)
    except ValueError:
        return None


def main():
    parser = argparse.ArgumentParser(description="Convert an Ahrefs Matching Terms CSV export to build_report.py JSON")
    parser.add_argument("--csv", required=True, help="Ahrefs CSV export path")
    parser.add_argument("--out", required=True)
    parser.add_argument("--cpc-in-cents", action="store_true",
                         help="Pass this only if your export's CPC column is in cents, not full currency units "
                              "(Ahrefs API responses are in cents; most UI CSV exports are not — check one row).")
    args = parser.parse_args()

    with open(args.csv, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit("CSV has no header row — is this a real Ahrefs export?")
        fieldnames_lower = {name.strip().lower(): name for name in reader.fieldnames}

        col = {key: find_column(fieldnames_lower, variants) for key, variants in FIELD_VARIANTS.items()}
        if not col["keyword"]:
            sys.exit(f"Could not find a 'Keyword' column. Columns found: {reader.fieldnames}")

        intent_cols = {key: find_column(fieldnames_lower, variants) for key, variants in INTENT_VARIANTS.items()}
        combined_intents_col = find_column(fieldnames_lower, COMBINED_INTENTS_VARIANTS)

        rows = []
        skipped = 0
        for raw_row in reader:
            keyword = (raw_row.get(col["keyword"]) or "").strip()
            if not keyword:
                skipped += 1
                continue

            cpc = to_number(raw_row.get(col["cpc"])) if col["cpc"] else None
            if cpc is not None and args.cpc_in_cents:
                cpc = cpc / 100

            intents = parse_intents(raw_row, intent_cols, combined_intents_col)

            rows.append({
                "keyword": keyword,
                "volume": to_number(raw_row.get(col["volume"])) if col["volume"] else None,
                "difficulty": to_number(raw_row.get(col["difficulty"])) if col["difficulty"] else None,
                "cpc": cpc,
                "traffic_potential": to_number(raw_row.get(col["traffic_potential"])) if col["traffic_potential"] else None,
                "intents": intents,
                "parent_topic": (raw_row.get(col["parent_topic"]) or "").strip() if col["parent_topic"] else None,
            })

    # "Group by terms" répète la même ligne une fois par groupe de termes —
    # dédoublonne pour ne garder qu'une occurrence par mot-clé.
    seen = set()
    deduped = []
    for row in rows:
        key = row["keyword"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    dup_count = len(rows) - len(deduped)
    rows = deduped

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"Saved: {args.out} — {len(rows)} mots-clés convertis"
          + (f", {skipped} lignes vides ignorées" if skipped else "")
          + (f", {dup_count} doublons retirés (Group by terms)" if dup_count else ""))
    print("Colonnes détectées :", {k: v for k, v in col.items() if v})
    if not col["cpc"]:
        print("⚠️  Pas de colonne CPC trouvée — vérifie l'export si le CPC est important pour ce rapport.")


if __name__ == "__main__":
    main()
