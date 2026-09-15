---
name: revamp-report-fr
description: "Automates checklist steps 2 (search intent), 3 (secondary keywords), 4/competitor analysis, and a partial step 5 (generative AI signal) of the France Squad Consolidation/Revamp Checklist. Given one French keyword + its papernest.com target page, produces a 5-tab Excel report: SERP (top organic + PAA + related searches + AI Overview), Ahrefs (real secondary keywords with detailed search-intent descriptions), GSC (real query performance on the target page), Competitors (H1-H3 structure of selectra.info / kelwatt.fr / fournisseurs-electricite.com / hellowatt.fr), and an optional AI Generative tab (simulated AI answer + real cited sources + genuine fan-out sub-queries). FR market only — papernest.com."
---

# Revamp Report (FR)

Turns roughly 15 back-and-forth tool calls into one repeatable workflow. Built after doing this by hand for three keywords (octopus energy avis, engie avis, edf avis) on the France revamp checklist and noticing the slow, error-prone parts: hand-retyping Ahrefs JSON (once silently dropped 22 rows), mixing `related-terms` noise into the secondary-keyword tab, and generating "fan-out queries" that were just the seed keyword with words bolted on.

## Intent
- Goal: given a French keyword + its papernest.com page, produce the SERP/Ahrefs/GSC/Competitor(/AI Generative) analysis the revamp checklist asks for, as one reviewable Excel file, in French.
- Optimizes for: zero hand-retyping of API data (source of the row-loss bug below), a real distinction between "secondary keyword" and "SERP co-occurrence noise", genuine fan-out sub-questions instead of keyword restatements.
- Sacrifices: the checklist's own ChatGPT DevTools fan-out extraction (needs a logged-in browser session, not available to an agent) — this skill's AI Generative tab is a reasoned proxy, not that.
- Success: a report where every tab can be handed to a copywriter as-is, with no "trust me" cells.
- Failure: a report where the Ahrefs tab is polluted with generic terms (trustpilot, prix, gaz...) that aren't secondary keywords, or an AI Generative tab whose "fan-out queries" are just `<brand> avis <word>`.

## Gotchas
<!-- Populate from real usage only. -->
- **Ahrefs CPC is returned in USD cents.** Divide by 100 before writing it into any JSON file this script reads. Forgetting this makes every CPC column 100x too high.
- **Use `keywords-explorer-matching-terms` for the Ahrefs tab, never `keywords-explorer-related-terms`.** `related-terms` (mode `all` = "also_rank_for" + "also_talk_about") returns what the *ranking pages* also cover — generic terms like "trustpilot", "prix", "gaz", "service client" that are NOT variants of the seed keyword. Mixing them in (sorted by volume) buries the real secondary keywords under generic noise. Caught on the first real run (octopus recensioni, IT) and again on the France pipeline — this is the single most common mistake.
- **Never hand-retype an Ahrefs JSON response into a file.** Write the raw tool output to a file verbatim (or via a tiny conversion script for the CPC divide), then let `build_report.py` read it. Hand-retyping once dropped 22 of 45 rows silently — the file "looked done" but wasn't.
- **`--brand` is required and must not be hardcoded anywhere in the script.** An earlier version hardcoded "Octopus" in the default intent-cluster text and in the brand-detection checks; it silently produced "the user is interested in Octopus Energy" text in the Engie and EDF reports too. If you fork this script, grep for the brand string before shipping.
- **AI Generative fan-out queries must be genuine sub-questions, not keyword restatements.** The fix: take the *real* PAA questions from the SERP data, and for each one ask "what concrete, checkable fact would resolve this subjective question" (e.g. "is EDF reliable?" → "EDF official complaint rate at the national energy mediator", not "EDF avis fiabilité"). Document the `logic` field explaining the mapping — a reader should be able to see why each fan-out exists.
- **Target-page discovery can hit cannibalization.** `site:papernest.com <keyword>` sometimes ranks a sub-page (e.g. `.../avis/forum/`) above the intended `.../avis/` page. Always check the top 10 `site:` results, not just the first one, and flag cannibalization in the SERP tab's notes column when it happens.
- **SerpAPI keys rotate and run out.** Check `total_searches_left` via the account endpoint before assuming a key works — don't discover it's dead mid-run. If your team keeps several keys in one file, check them in parallel, not one at a time.
- **`--gsc-csv` must be pre-filtered to the target page**, not a sitewide dump. `fetch_gsc.py query` supports `--page <exact url>` (dimensionFilterGroups, operator=equals) — use it, otherwise you're sorting through the whole site's top query+page rows by hand to find the ones for your page.

## Runtime Rules (max 8)
1. Always resolve the exact target page URL first (`site:papernest.com <keyword>`, check top 10, not just #1) before running anything else — every other step depends on it.
2. Ahrefs secondary keywords come from `keywords-explorer-matching-terms` only. If you also want SERP co-occurrence signal, keep it in a separate, clearly labeled section — never merge it into the same ranked list as real secondary keywords.
3. Never write Ahrefs tool output into a JSON file by memory/paraphrase — copy the raw response, or transform it with a short script, and verify the row count matches the tool's own count before moving on.
4. `--brand` is a required CLI argument for `build_report.py`. If you're about to hardcode a brand name anywhere in a script, stop — pass it as a parameter instead.
5. Every fan-out query in the AI Generative tab must trace back to a real PAA or related-search entry from the SERP data, transformed into a checkable fact — not a paraphrase of the seed keyword. Include the `logic` explaining the transformation.
6. Filter the GSC CSV to the target page (`--page`) before handing it to `build_report.py` — never pass a sitewide export.
7. Never commit `.env`, `secrets/gsc_client_secret.json`, `secrets/gsc_token.json`, or any real API key — they're gitignored in this folder; keep it that way. Placeholder values only in files that get committed.
8. Output is always in French (headers, notes, intent descriptions) — this is a deliverable for a French-speaking team, not an internal debug artifact.

## Test Prompt
> "Run the FR revamp report for keyword '<french keyword>'"

Expected: target page found on papernest.com (with cannibalization check), Ahrefs matching-terms pulled (not related-terms), live SERP fetched, the 4 competitor pages found and their H1-H3 structure extracted, GSC queried and filtered to the target page, and a 4-or-5-tab xlsx produced with `--brand` correctly set — no hardcoded brand leaking from a previous run.

---

## Input
- One French keyword (e.g. `"octopus energy avis"`)
- The brand/provider name as it should read in French sentences (e.g. `"Octopus Energy"`, `"Engie"`, `"EDF"`)
- Papernest.com FR property access in your GSC OAuth account (see SETUP.md)
- Ahrefs MCP connector available in your Claude Code session
- A SerpAPI key with remaining credit (see SETUP.md)

## Workflow
This is a hybrid workflow: most steps are live tool calls an agent (Claude) makes; only the final assembly is a deterministic script.

1. **Find the target page.** `site:papernest.com <keyword>` (Serper or equivalent, `gl=fr hl=fr`). Check the top 10 results, not just the first — watch for a sub-page outranking the main page (cannibalization) and note it.
2. **Secondary keywords.** Ahrefs MCP → `keywords-explorer-matching-terms` (`country=fr`, `terms=all`, `match_mode=terms`, `limit=100`). Write the raw JSON response to a file verbatim; convert `cpc` from cents to dollars with a short script (never by hand). This becomes `--matching-json`.
3. **Live SERP.** SerpAPI `engine=google&gl=fr&hl=fr&q=<keyword>` (check credit first). Extract organic top ~10 with a one-line summary per result, PAA, related searches, AI Overview presence/content, knowledge graph. Save as the `--serp-json` structure (see any existing `serp_*.json` example produced by a prior run for the exact shape `build_report.py` expects).
4. **Competitors.** For each of selectra.info, kelwatt.fr, fournisseurs-electricite.com, hellowatt.fr: `site:<competitor> <keyword>` to find their equivalent page (note if one has no dedicated page — it happens, e.g. Kelwatt sometimes only covers reviews inside the general provider page), then fetch and extract H1 + H2/H3 structure + a short summary. Save as `--competitors-json`.
5. **GSC.** `python scripts/fetch_gsc.py query --site "https://www.papernest.com/" --start <6 months ago> --end <today> --dimensions query --page "<exact target URL>" --row-limit 500 --out <file>.csv`.
6. **(Optional) AI Generative tab.** Take the real PAA questions from step 3. For each, derive the concrete fact an AI would need to verify to answer it (not a keyword restatement) — 4-5 fan-out queries total, plus a short `logic` explanation and the usual DevTools-extraction caveat. Also note real cited sources from the SERP (which review aggregators, forums, comparators appear) and a short "what would an AI answer without live browsing" simulation. Save as `--ia-generative-json`.
7. **Assemble.** `python scripts/build_report.py --keyword "<kw>" --brand "<Brand>" --matching-json ... --serp-json ... --competitors-json ... --gsc-csv ... --page-url "<exact url>" [--ia-generative-json ...] --out <name>_FR_report.xlsx`

## Output
- One `.xlsx` file, 4 or 5 tabs depending on whether step 6 was run: **SERP**, **Ahrefs**, **GSC**, **Concurrents**, optionally **IA générative**.
- Everything in French — headers, notes, intent descriptions.
- Every finding traceable to a real tool call — no fabricated numbers.

## QA Gates
- [ ] Target page confirmed from the top 10 `site:` results, cannibalization checked
- [ ] Ahrefs tab built from `matching-terms` only — no generic co-occurrence terms mixed into the ranked list
- [ ] Row count in the final JSON matches the row count the Ahrefs tool actually returned (no silent data loss from hand-retyping)
- [ ] `--brand` passed and no brand name hardcoded anywhere in `build_report.py`
- [ ] Every AI Generative fan-out query traces to a real PAA/related-search + a stated transformation logic, not a keyword restatement
- [ ] GSC CSV pre-filtered to the exact target page
- [ ] No real credential (`.env`, `gsc_client_secret.json`, `gsc_token.json`, API key) present in any committed file

---
v0.1 — 12 Aug 2026, extracted from three real runs (octopus energy avis, engie avis, edf avis) on the France revamp checklist. Stabilize after 3-5 more real uses — in particular, verify the AI Generative tab's fan-out heuristic against an actual ChatGPT DevTools extraction if/when browser access becomes available.
