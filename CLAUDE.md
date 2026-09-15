# CLAUDE.md — revamp-helper-claude

Repo minimo e autonomo per generare un report di keyword research per il revamp di una pagina
papernest.com (mercato FR), tramite GitHub Issue invece di un'app Streamlit.

## Cos'è

Stesso obiettivo del tool Streamlit "Revamp Helper" di Georges (e della skill `revamp-report-fr`
scritta da Diego): dato keyword + brand, produce SERP + Ahrefs + GSC + struttura competitor in
un report Excel, ma:
- nessun server dedicato, gira on-demand su GitHub Actions
- autenticato con l'abbonamento Claude del team (`CLAUDE_CODE_OAUTH_TOKEN`), non una API key
  Anthropic a pagamento
- Ahrefs via CSV esportato a mano (l'MCP Ahrefs non è raggiungibile da un runner GitHub Actions
  — è legato alla sessione claude.ai personale, non una credenziale portabile)

## Entry point

`SKILL.md` (revamp-report-fr) — il workflow di riferimento, scritto per una sessione Claude Code
interattiva con Ahrefs MCP collegato. **In esecuzione da GitHub Issue è diverso**: leggi SEMPRE
`CI-INSTRUCTIONS.md` per primo quando vieni invocato da una menzione `@claude` su un'issue con
label `revamp-helper` — sostituisce le chiamate agente/MCP di `SKILL.md` con gli script
deterministici in `scripts/` (già pronti, non riscriverli).

## Come si usa

Vai su GitHub → tab **Issues** → **New issue** → template **"Rapport de revamp"** → compila
keyword + brand (+ URL/CSV Ahrefs/note opzionali) → Submit. Claude risponde come commento, **in
francese**, con un riepilogo; il file Excel completo è scaricabile come artifact della run.

## Setup una tantum

Vedi `README.md`: installazione Claude GitHub App, label da creare, secret da configurare
(`CLAUDE_CODE_OAUTH_TOKEN`, `SERPAPI_API_KEY`, `GSC_CLIENT_SECRET_JSON`, `GSC_TOKEN_JSON`,
`GOOGLE_NLP_API_KEY` opzionale).

## Cosa manca rispetto al tool Streamlit di riferimento

- Ahrefs richiede il CSV esportato a mano dal copywriter (Georges usava lo stesso meccanismo
  nella sua app; la skill di Diego usa invece l'MCP live, non portabile qui)
- Niente output Google Sheets condiviso — un file Excel scaricabile come artifact della run
  invece, per evitare un terzo flusso OAuth (Sheets) oltre a quello già necessario per GSC

## Contesto: mercato di riferimento

- **Paese**: Francia | **Settore**: energia elettricità/gas | **Target**: consumatori domestici
- **Competitor fissi FR**: selectra.info, kelwatt.fr, fournisseurs-electricite.com, hellowatt.fr
- **Lingua output**: sempre francese (report, riepiloghi, commenti)
