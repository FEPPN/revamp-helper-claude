# Revamp Helper (Claude) — tool copywriter su GitHub Issues

Stesso obiettivo del tool Streamlit "Revamp Helper" di Georges (e della skill `revamp-report-fr`):
dato keyword + brand, produce un report di keyword research per un revamp — SERP (organici, PAA,
related, AI Overview), keyword secondarie Ahrefs, performance GSC reale, struttura H1-H3 dei 4
competitor FR fissi, in un unico file Excel. Gira su GitHub Actions (nessun server da mantenere),
autenticato con l'abbonamento Claude del team invece di una API key a pagamento. Si usa aprendo
un **Issue** e conversando a commenti con `@claude`.

## Setup una tantum (chi amministra il repo)

### 0. Crea la label "revamp-helper"

Repo → tab **Issues** → **Labels** → **New label** → Nome: `revamp-helper` → colore a piacere →
**Create label**. Il workflow gira solo su issue con questa label, e il template la applica da
solo ma solo se esiste già.

### 1. Installa la Claude GitHub App

**https://github.com/apps/claude** → **Install** → org **FEPPN** → **Only select
repositories** → **revamp-helper-claude** → **Install**.

### 2. Genera il token Claude (abbonamento del team)

Stessa procedura di `page-audit-claude` — se hai già un `CLAUDE_CODE_OAUTH_TOKEN` generato per
quel repo, **puoi riusare lo stesso valore qui** (è legato all'abbonamento, non al repo). Se
serve rigenerarlo:

```
claude setup-token
```

da un browser incognito loggato sull'account condiviso del team — copia il token stampato.

### 3. Prepara le credenziali GSC come secret (base64)

`gsc-connector` usa due file JSON (`ZZ_file ENV/Keys/gsc_client_secret.json` e
`gsc_token.json`) — vanno codificati in base64 per poterli mettere in un secret GitHub (che
accetta solo testo):

PowerShell:
```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("C:\Users\diego.branzaglia\Demo\ZZ_file ENV\Keys\gsc_client_secret.json")) | Set-Clipboard
```
Incolla il risultato (è già negli appunti) come valore del secret `GSC_CLIENT_SECRET_JSON`.
Ripeti per `gsc_token.json` → secret `GSC_TOKEN_JSON`.

### 4. Aggiungi i secret al repo GitHub

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Nome secret | Valore | Obbligatorio |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | il token del punto 2 | Sì |
| `SERPAPI_API_KEY` | `ZZ_file ENV/Keys/SERPapi` | Sì — senza, niente SERP/pagine/competitor |
| `GSC_CLIENT_SECRET_JSON` | base64 di `gsc_client_secret.json` (punto 3) | Sì — senza, niente tab GSC |
| `GSC_TOKEN_JSON` | base64 di `gsc_token.json` (punto 3) | Sì — senza, niente tab GSC |
| `GOOGLE_NLP_API_KEY` | `ZZ_file ENV/API Google.env.txt` | No — senza, il gap entità (a richiesta) viene saltato |

**Attenzione al budget SerpAPI**: è la stessa key condivisa con `serp-snapshot`,
`brief-keyword-table` e `page-audit-claude` — piano gratis 250 ricerche/mese in totale, e questo
tool ne consuma 5 a run (1 pagina target + 4 competitor) più 1 per il SERP live, quindi ~6.
Claude dichiara sempre il saldo prima/dopo nel commento di risposta.

### 5. Verifica che tutto sia attivo

Repo → tab **Issues** → **New issue** → dovresti vedere il template **"Rapport de revamp"**.

## Comment un(e) copywriter l'utilise (en français — c'est ce qu'iel voit)

1. Repo → onglet **Issues** → **New issue** → modèle **"Rapport de revamp"**
2. Remplissez **mot-clé** et **marque** (obligatoires) ; URL de la page cible et CSV Ahrefs si
   vous les avez déjà → **Submit new issue**
3. En quelques minutes, Claude répond en commentaire avec un **résumé** : page cible trouvée
   (ou alerte de cannibalisation), les 4 concurrents, un aperçu des mots-clés secondaires (si
   CSV fourni), un aperçu SERP et GSC, le solde SerpAPI
4. Le **fichier Excel complet** (5 onglets) est téléchargeable en bas de la page de la run
   GitHub Actions, sous **"Artifacts"**
5. Pour relancer avec une page cible différente, un CSV Ahrefs, ou pour demander l'onglet gap
   d'entités : commentez `@claude` avec la nouvelle information

## Differenze rispetto al tool Streamlit di riferimento

- Ahrefs: CSV esportato a mano (stesso meccanismo di Georges) — l'MCP Ahrefs live (che usa la
  skill `revamp-report-fr` in sessione normale) non è raggiungibile da GitHub Actions
- Output come artifact scaricabile della run, non un Google Sheet condiviso (evita un terzo
  flusso OAuth oltre a quello già necessario per GSC)
- Interazione via commenti GitHub invece di un form web

## Struttura del repo

```
CLAUDE.md                                Istruzioni di progetto
CI-INSTRUCTIONS.md                       Come Claude orchestra gli script in CI
SKILL.md                                 Workflow di riferimento (revamp-report-fr, sessione normale)
.github/ISSUE_TEMPLATE/revamp-helper.yml Il modulo per aprire una richiesta di report
.github/workflows/revamp-helper.yml      Il workflow GitHub Actions
scripts/find_pages.py                    Trova pagina target + 4 competitor (SerpAPI, no LLM)
scripts/scrape_competitors.py            Estrae H1-H3 dei competitor (no LLM)
scripts/fetch_serp.py                    SERP live: organici/PAA/related/AI Overview (SerpAPI)
scripts/parse_ahrefs_csv.py              Converte il CSV Ahrefs "Matching terms" nel JSON atteso
scripts/fetch_gsc.py                     Query GSC via OAuth (gsc-connector)
scripts/analyze_entities.py              Gap entità vs competitor (a richiesta, Google NL API)
scripts/build_report.py                  Assembla tutto in un .xlsx multi-tab
```

## Nota sul drift

`SKILL.md` e gli script sono copie da `FEPPN/Revamp-helper-test` (a sua volta basato sulla tua
skill `revamp-report-fr` in `FEPPN/revamp-helper`) — non un submodule/sync automatico. Controlla
periodicamente se sono stati aggiornati altrove.
