# Istruzioni di esecuzione — modalità GitHub Issue (conversazione)

Questo file si applica quando `SKILL.md` (revamp-report-fr) gira dentro un GitHub Issue,
invocato da una menzione `@claude`. In ogni altro contesto (Claude Code locale, sessione con
Ahrefs MCP collegato) ignoralo e segui `SKILL.md` così com'è scritto — lì la scoperta
pagine/competitor e i dati Ahrefs sono chiamate agente/MCP live; qui sono script deterministici
perché l'ambiente CI non ha MCP né sessione claude.ai collegata.

## Perché script e non chiamate agente per tutto

`find_pages.py` e `scrape_competitors.py` fanno esattamente ciò che `SKILL.md` Workflow §1/§4
chiederebbe a un agente di fare a mano (site: search, estrazione H1-H3) ma in modo deterministico
e già nella forma esatta che `build_report.py` si aspetta — usali sempre, non rifare la ricerca
con altri strumenti. Ahrefs invece **non ha un percorso scriptabile con le sole credenziali
disponibili qui** (l'MCP Ahrefs è legato alla sessione claude.ai personale, non arriva su un
runner GitHub Actions — vedi la Gotcha di `SKILL.md` stesso su questo) — per questo si usa il CSV
esportato a mano dal copywriter invece della chiamata MCP `keywords-explorer-matching-terms`.

## Input da questa issue

Dal corpo dell'issue (o dal commento che ha menzionato `@claude`, se è un follow-up): `keyword`
(obbligatoria), `brand` (obbligatoria — mai hardcodare un brand da un run precedente, vedi
Gotcha #4 di `SKILL.md`), `page_url` (opzionale — se assente la trova `find_pages.py`), il CSV
Ahrefs incollato (opzionale), note extra (opzionale).

## Pipeline (ordine fisso)

Crea `output/` se non esiste; tutti i file intermedi e il report finale vanno lì.

1. **Pagine**: `python scripts/find_pages.py --keyword "<keyword>" --out output/pages.json`
   - Se `target` è `null`: dillo subito nella risposta, non proseguire con un URL inventato.
   - Se `target_alternatives` non è vuoto: c'è cannibalizzazione potenziale — segnalalo nel
     riepilogo finale con gli URL alternativi trovati.
   - Se `page_url` era stato dato nell'issue ed è diverso dal `target` trovato: usa quello
     dell'issue per il resto della pipeline (preferenza umana esplicita), ma segnala la
     discrepanza.
   - Se un competitor ha `no_dedicated_avis_page: true`: normale, succede (es. Kelwatt a volte
     non ha una pagina dedicata) — non è un errore, dillo e basta.

2. **Struttura competitor**: `python scripts/scrape_competitors.py --pages output/pages.json
   --out output/competitors.json`

3. **SERP live**: `python scripts/fetch_serp.py --keyword "<keyword>" --out output/serp.json`
   — prima e dopo, controlla il credito SerpAPI residuo (`GET
   https://serpapi.com/account?api_key=$SERPAPI_API_KEY` → `total_searches_left`) e dichiaralo
   nel riepilogo finale (convenzione già in uso nel team per questa chiave condivisa).

4. **Ahrefs (opzionale)**:
   - Se il copywriter ha incollato un CSV nell'issue: salvalo in `output/ahrefs_export.csv`
     (contenuto esatto incollato, non riformattato), poi `python scripts/parse_ahrefs_csv.py
     --csv output/ahrefs_export.csv --out output/matching.json`.
   - Se non l'ha incollato: crea comunque `output/matching.json` con contenuto `[]` (lista
     vuota) — serve perché il flag è obbligatorio per `build_report.py` — e dichiara
     esplicitamente nel riepilogo che il tab Ahrefs è vuoto perché nessun CSV è stato fornito.

5. **GSC**: finestra di 6 mesi che termina a "oggi meno 3 giorni" (dati non ancora consolidati
   negli ultimi giorni). `python scripts/fetch_gsc.py query --site "https://www.papernest.com/"
   --start <6 mesi fa, YYYY-MM-DD> --end <oggi-3gg, YYYY-MM-DD> --dimensions query --page
   "<target url dal punto 1>" --row-limit 500 --out output/gsc.csv`
   - Se il comando fallisce o produce zero righe: crea comunque `output/gsc.csv` con solo
     l'header (`query,clicks,impressions,ctr,position` o equivalente) così `build_report.py` non
     si blocca, e dichiaralo nel riepilogo (può essere un content gap reale, non sempre un bug —
     vedi la stessa distinzione già nota in `page-audit-claude`).

6. **Gap entità (solo se richiesto esplicitamente in un commento)**: usa `analyze_entities.py`
   come in `page-audit/SKILL.md` (stesso script) — `--gap-vs` con gli URL competitor trovati al
   punto 1, `--json --out output/entity_gap.json`. Se `GOOGLE_NLP_API_KEY` non è configurata,
   dillo e salta, non bloccare il resto.

7. **Assemblaggio**: `python scripts/build_report.py --keyword "<keyword>" --brand "<brand>"
   --matching-json output/matching.json --serp-json output/serp.json --competitors-json
   output/competitors.json --gsc-csv output/gsc.csv --page-url "<target url>" [--entity-gap-json
   output/entity_gap.json se eseguito al punto 6] --out
   output/<brand-slug>_<keyword-slug>_FR_report.xlsx`

## Cosa scrivere nella risposta (commento sull'issue)

In francese (il copywriter è francofono — Runtime Rule 8 di `SKILL.md`). Non incollare l'intero
contenuto del file Excel: un **riepilogo leggibile a colpo d'occhio**, poi il file scaricabile è
già allegato alla run come artifact (dillo, con "voir Artifacts en bas de la page de cette run").

Struttura del commento:
```
## Rapport de revamp — <keyword> (<brand>)

Page cible : <url> (trouvée automatiquement / fournie par vous)
⚠️ Cannibalisation potentielle : <url alternatives> (seulement si target_alternatives non vide)

### Concurrents trouvés
| Site | Page | Statut |
(4 lignes, note "pas de page dédiée" si no_dedicated_avis_page)

### Aperçu mots-clés secondaires (Ahrefs)
(top 10 par volume si le CSV a été fourni, sinon : "Aucun CSV Ahrefs fourni — onglet vide, voir
comment en ajouter un dans un commentaire")

### Aperçu SERP
PAA (nombre trouvé), related searches (nombre), AI Overview présent : oui/non

### Aperçu GSC
Top 5 requêtes par clics sur cette page (ou "aucune donnée trouvée pour cette page")

### Budget SerpAPI
Avant : N recherches restantes ce mois-ci · Après : N

📎 Le rapport Excel complet (5 onglets) est disponible en téléchargement dans les "Artifacts"
de cette exécution GitHub Actions.
```

## Regole valide sempre

- **Mai inventare numeri**: se un dato non è disponibile (Ahrefs vuoto, GSC vuoto, competitor
  senza pagina), dillo esplicitamente — mai riempire con una stima.
- **`--brand` non va mai hardcodato**: usa sempre quello dato nell'issue/commento corrente, mai
  quello di un run precedente in questo stesso repo.
- **Ahrefs CPC**: se mai ricevi dati Ahrefs via un percorso diverso dal CSV (es. incollati a
  mano nel commento come JSON), ricorda che l'API Ahrefs restituisce CPC in centesimi — il CSV
  da UI di solito no, verifica una riga prima di assumere (`parse_ahrefs_csv.py --cpc-in-cents`
  se serve).
- Non fare mai una domanda e aspettare una risposta nello stesso turno — finisci il turno,
  il copywriter risponde in un commento successivo che menziona di nuovo `@claude`.
