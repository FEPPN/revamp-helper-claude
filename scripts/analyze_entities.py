#!/usr/bin/env python3
"""Estrazione entità + connessioni da URL/HTML via Google Cloud Natural Language API.

Nodi:  Google NLP v1 analyzeEntities  -> entità + salience + mid + wikipedia_url
Archi: co-occorrenza deterministica    -> due entità nella stessa frase = arco (peso = # frasi)
Enrich: Google Knowledge Graph Search  -> descrizione canonica per ogni entità con mid

Solo stdlib. Auth via API key (env GOOGLE_NLP_API_KEY / GOOGLE_API_KEY o --api-key).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
import unicodedata
from urllib.parse import quote, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

NLP_ENDPOINT = "https://language.googleapis.com/v1/documents:analyzeEntities"
KG_ENDPOINT = "https://kgsearch.googleapis.com/v1/entities:search"

FREE_TIER_UNITS = 5000  # unità/mese incluse nel free tier NL API
UNIT_CHARS = 1000       # 1 unità = 1000 caratteri
PRICE_PER_1K_UNITS = 1.0  # USD oltre il free tier (fascia 0-1M)

SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "canvas", "iframe",
             "nav", "footer", "header", "aside", "form"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "h1", "h2", "h3", "h4", "h5",
              "h6", "br", "td", "th", "tr", "ul", "ol", "table"}
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


# --------------------------------------------------------------------------- #
# 1. Estrazione testo visibile                                                #
# --------------------------------------------------------------------------- #
class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS:
            self.skip_depth += 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = VisibleTextParser()
    parser.feed(raw)
    parser.close()
    text = html.unescape("".join(parser.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def fetch_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (entity-extractor/1.0)"})
    try:
        with urlopen(request, timeout=25) as response:
            raw = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} sul fetch di {url}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Fetch fallito {url}: {exc}") from exc
    return raw.decode(charset, errors="replace")


def load_source(source: str) -> tuple[str, str]:
    """Ritorna (label, testo_pulito). Accetta URL, file .html o file .txt."""
    if is_url(source):
        return source, html_to_text(fetch_url(source))
    path = Path(source).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"File non trovato: {path}")
    raw = path.read_text(encoding="utf-8", errors="replace")
    # .txt/.md come testo puro, MA se il contenuto è HTML (es. pagina salvata/archiviata)
    # lo si pulisce comunque.
    looks_html = re.search(r"<!doctype html|<html[ >]", raw[:3000], re.IGNORECASE)
    text = raw if (path.suffix.lower() in {".txt", ".md"} and not looks_html) else html_to_text(raw)
    return str(path), text


# --------------------------------------------------------------------------- #
# 2. Google NLP + Knowledge Graph                                             #
# --------------------------------------------------------------------------- #
def post_json(url: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"NLP API HTTP {exc.code}: {body}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"NLP API irraggiungibile: {exc}") from exc


def analyze_entities(text: str, api_key: str, language: str = "it") -> list[dict]:
    payload = {
        "document": {"type": "PLAIN_TEXT", "content": text, "language": language},
        "encodingType": "UTF8",
    }
    resp = post_json(f"{NLP_ENDPOINT}?key={api_key}", payload)
    return resp.get("entities", [])


def kg_lookup(api_key: str, mid: str | None, name: str, language: str = "it") -> dict | None:
    params = {"key": api_key, "limit": 1, "languages": language}
    if mid:
        params["ids"] = mid
    else:
        params["query"] = name
    try:
        with urlopen(f"{KG_ENDPOINT}?{urlencode(params)}", timeout=20) as response:
            resp = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError):
        return None
    items = resp.get("itemListElement", [])
    if not items:
        return None
    result = items[0].get("result", {})
    detailed = result.get("detailedDescription", {}) or {}
    return {
        "kg_name": result.get("name"),
        "kg_types": result.get("@type", []),
        "kg_description": result.get("description"),
        "kg_detail": detailed.get("articleBody"),
        "kg_url": detailed.get("url"),
        "kg_score": items[0].get("resultScore"),
    }


# --------------------------------------------------------------------------- #
# 2b. Vicini attesi (Wikidata + Wikipedia) — gap topico di un'entità          #
# --------------------------------------------------------------------------- #
WIKI_UA = {"User-Agent": "entity-extractor/1.0 (papernest SEO analysis)"}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s)).strip()


# Rumore non-topico: indirizzi stradali + boilerplate generico (FR/IT/EN)
_ADDRESS_RE = re.compile(
    r"\b(rue|avenue|av|bd|boulevard|impasse|chemin|allee|quai|place|via|viale|"
    r"corso|piazza|strada|largo)\b", re.IGNORECASE)
_BOILERPLATE = {"annonce", "annonce annonce", "blog", "acteur", "cookie",
                "newsletter", "menu", "accueil", "home", "aggiungi"}


def _is_boilerplate(name: str) -> bool:
    if _norm(name) in _BOILERPLATE:
        return True
    return bool(_ADDRESS_RE.search(name))


def _get_json(url: str) -> dict:
    with urlopen(Request(url, headers=WIKI_UA), timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_wikipedia(url: str) -> tuple[str | None, str | None]:
    m = re.match(r"https?://([a-z-]+)\.wikipedia\.org/wiki/(.+)$", url)
    return (m.group(1), unquote(m.group(2))) if m else (None, None)


def qid_from_wikipedia(url: str) -> str | None:
    lang, title = _parse_wikipedia(url)
    if not lang:
        return None
    data = _get_json(f"https://{lang}.wikipedia.org/w/api.php?action=query"
                     f"&prop=pageprops&format=json&titles={quote(title)}")
    for page in data.get("query", {}).get("pages", {}).values():
        qid = page.get("pageprops", {}).get("wikibase_item")
        if qid:
            return qid
    return None


# Relazioni Wikidata rilevanti per il gap topico SEO (org/economiche).
# Escluse: persone (AD, CdA, fondatori), categorie Wikimedia, "precede/segue".
RELEVANT_PROPS = {
    "P452",   # settore/industry
    "P355",   # organisation fille / subsidiary
    "P1830",  # propriétaire de / owner of
    "P127",   # owned by
    "P749",   # parent organization
    "P463",   # member of
    "P361",   # part of
    "P1056",  # product or material produced
    "P199",   # business division
    "P17",    # country
    "P159",   # headquarters
    "P414",   # stock exchange
    "P366",   # has use
}


def wikidata_neighbors(qid: str, lang: str) -> dict[str, dict]:
    """{nome_entità: {"prop": Pxxx, "rel": etichetta}} dalle claim item-valued."""
    ent = _get_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json") \
        .get("entities", {}).get(qid, {})
    pairs: list[tuple[str, str]] = []
    for prop, snaks in ent.get("claims", {}).items():
        for sn in snaks:
            dv = sn.get("mainsnak", {}).get("datavalue", {})
            if dv.get("type") == "wikibase-entityid":
                oid = dv["value"].get("id", "")
                if oid.startswith("Q"):
                    pairs.append((prop, oid))
    ids = list({p for _, p in pairs} | {pr for pr, _ in pairs})
    labels: dict[str, str] = {}
    for i in range(0, len(ids), 50):
        chunk = "|".join(ids[i:i + 50])
        d = _get_json(f"https://www.wikidata.org/w/api.php?action=wbgetentities"
                      f"&ids={chunk}&props=labels&languages={lang}|en&format=json")
        for k, v in d.get("entities", {}).items():
            lab = v.get("labels", {})
            val = (lab.get(lang) or lab.get("en") or {}).get("value")
            if val:
                labels[k] = val
    out: dict[str, dict] = {}
    for prop, oid in pairs:
        name = labels.get(oid)
        if name and name not in out:
            out[name] = {"prop": prop, "rel": labels.get(prop, prop)}
    return out


def wikipedia_links(url: str, cap: int = 400) -> set[str]:
    lang, title = _parse_wikipedia(url)
    if not lang:
        return set()
    titles: set[str] = set()
    cont = ""
    while len(titles) < cap:
        extra = f"&plcontinue={quote(cont)}" if cont else ""
        d = _get_json(f"https://{lang}.wikipedia.org/w/api.php?action=query&prop=links"
                      f"&plnamespace=0&pllimit=max&format=json&titles={quote(title)}{extra}")
        for page in d.get("query", {}).get("pages", {}).values():
            for lk in page.get("links", []):
                titles.add(lk["title"])
        cont = d.get("continue", {}).get("plcontinue", "")
        if not cont:
            break
    return titles


def find_expected_neighbors(entity: dict, text: str, language: str,
                            relevant_only: bool = True) -> dict:
    """Entità canonicamente associate all'entità data ma ASSENTI nella pagina.

    relevant_only=True (default): tiene solo relazioni Wikidata topiche (RELEVANT_PROPS)
    e nasconde il tier Wikipedia-only (rumoroso). False (--neighbors-all): tutto.
    """
    wu = entity.get("wikipedia_url")
    if not wu:
        return {"entity": entity["name"],
                "error": f"'{entity['name']}' non ha wikipedia_url: impossibile risalire al QID Wikidata."}
    try:
        qid = qid_from_wikipedia(wu)
        if not qid:
            return {"entity": entity["name"], "error": f"QID Wikidata non trovato per '{entity['name']}'."}
        wd = wikidata_neighbors(qid, language)        # {name: {prop, rel}}
        wp = wikipedia_links(wu)                       # {title}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"entity": entity["name"],
                "error": f"Lookup Wikidata/Wikipedia fallito per '{entity['name']}': {exc}"}
    ntext = " " + _norm(text) + " "
    present = lambda name: f" {_norm(name)} " in ntext or _norm(name) in ntext

    merged: dict[str, dict] = {}
    for name, info in wd.items():
        if relevant_only and info["prop"] not in RELEVANT_PROPS:
            continue
        merged.setdefault(_norm(name), {"name": name, "wikidata": info["rel"], "wikipedia": False})
    for title in wp:
        k = _norm(title)
        if k in merged:
            merged[k]["wikipedia"] = True
        elif not relevant_only:  # Wikipedia-only entra solo in modalità completa
            merged[k] = {"name": title, "wikidata": None, "wikipedia": True}

    self_norm = _norm(entity["name"])
    both, wd_only, wp_only = [], [], []
    for k, v in merged.items():
        if k == self_norm or present(v["name"]):
            continue
        if v["wikidata"] and v["wikipedia"]:
            both.append(v)
        elif v["wikidata"]:
            wd_only.append(v)
        else:
            wp_only.append(v)
    return {
        "entity": entity["name"], "qid": qid, "relevant_only": relevant_only,
        "confirmed": sorted(both, key=lambda v: v["name"]),
        "wikidata_only": sorted(wd_only, key=lambda v: v["name"]),
        "wikipedia_only": sorted(wp_only, key=lambda v: v["name"])[:25],
        "wikipedia_only_total": len(wp_only),
    }


# --------------------------------------------------------------------------- #
# 3. Connessioni per co-occorrenza                                            #
# --------------------------------------------------------------------------- #
def sentences(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        out.extend(s.strip() for s in SENTENCE_SPLIT_RE.split(line) if s.strip())
    return out


def build_edges(entities: list[dict], text: str) -> list[dict]:
    """Arco fra due entità se compaiono nella stessa frase. Peso = # frasi condivise."""
    surfaces: list[tuple[str, list[re.Pattern]]] = []
    for ent in entities:
        forms = {ent["name"]}
        for m in ent.get("mentions", []):
            content = m.get("text", {}).get("content")
            if content:
                forms.add(content)
        pats = [re.compile(rf"\b{re.escape(f)}\b", re.IGNORECASE) for f in forms if len(f) > 1]
        surfaces.append((ent["name"], pats))

    pair_counts: dict[tuple[str, str], int] = {}
    for sent in sentences(text):
        present = [name for name, pats in surfaces if any(p.search(sent) for p in pats)]
        present = sorted(set(present))
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                key = (present[i], present[j])
                pair_counts[key] = pair_counts.get(key, 0) + 1

    return [
        {"source": a, "target": b, "weight": w}
        for (a, b), w in sorted(pair_counts.items(), key=lambda kv: -kv[1])
    ]


# --------------------------------------------------------------------------- #
# 4. Ponte con provider-entity-library.json                                   #
# --------------------------------------------------------------------------- #
def load_provider_library() -> dict:
    lib = (Path(__file__).resolve().parents[2]
           / "structured-data-mapper" / "references" / "provider-entity-library.json")
    if not lib.exists():
        return {}
    try:
        data = json.loads(lib.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return {v["nome_canonico"].lower(): v for k, v in data.items() if isinstance(v, dict) and "nome_canonico" in v}


def match_provider(name: str, library: dict) -> dict | None:
    return library.get(name.lower())


# --------------------------------------------------------------------------- #
# 5. Orchestrazione                                                           #
# --------------------------------------------------------------------------- #
def resolve_api_key(cli_key: str | None) -> str | None:
    if cli_key:
        return cli_key
    for var in ("GOOGLE_NLP_API_KEY", "GOOGLE_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def cache_path(source: str) -> Path:
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:16]
    cache_dir = Path(__file__).resolve().parent.parent / ".cache"
    cache_dir.mkdir(exist_ok=True)
    return cache_dir / f"{digest}.json"


def run(source: str, api_key: str | None, enrich: bool, use_cache: bool,
        no_google: bool, language: str, only_linked: bool = False,
        min_salience: float = 0.0, expected_neighbors: str | None = None,
        neighbors_all: bool = False, show_unrecognized: bool = False) -> dict:
    label, text = load_source(source)
    units = math.ceil(len(text) / UNIT_CHARS) or 1
    cost = max(0, units - 0) * (PRICE_PER_1K_UNITS / 1000)  # informativo (ignora free tier mensile)

    meta = {
        "source": label,
        "chars": len(text),
        "units": units,
        "free_tier_units": FREE_TIER_UNITS,
        "cost_estimate_usd_if_over_free_tier": round(cost, 4),
        "language": language,
    }

    if no_google:
        meta["mode"] = "no-google (nessuna chiamata API — solo testo estratto)"
        return {"meta": meta, "text_preview": text[:1500], "entities": [], "edges": []}

    if not api_key:
        raise RuntimeError(
            "API key mancante. Passa --api-key, o esporta GOOGLE_NLP_API_KEY.\n"
            "Per girare senza Google usa --no-google."
        )

    # La cache memorizza la parte COSTOSA e invariante (risposta NLP grezza + testo
    # + enrichment KG). Filtri, nodi e archi si ricalcolano sempre in locale, così
    # cambiare --only-linked / --min-salience non ricosta unità.
    cache = cache_path(label)
    enrich_map: dict[str, dict] = {}
    cached_blob = None
    if use_cache and cache.exists():
        candidate = json.loads(cache.read_text(encoding="utf-8"))
        if "raw_entities" in candidate:  # ignora cache in formato vecchio
            cached_blob = candidate
    if cached_blob is not None:
        raw_entities = cached_blob["raw_entities"]
        text = cached_blob.get("text", text)
        enrich_map = cached_blob.get("enrich", {})
        meta["from_cache"] = True
    else:
        raw_entities = analyze_entities(text, api_key, language)
        if enrich:  # solo entità con MID: la lookup per nome è rumore + costa chiamate
            for ent in raw_entities:
                mid = ent.get("metadata", {}).get("mid")
                if mid and mid not in enrich_map:
                    kg = kg_lookup(api_key, mid, ent["name"], language)
                    if kg:
                        enrich_map[mid] = kg
        if use_cache:
            cache.write_text(
                json.dumps({"text": text, "raw_entities": raw_entities, "enrich": enrich_map},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    # Filtri segnale/rumore: solo entità agganciate al KG e/o sopra soglia salience.
    # Con show_unrecognized: include anche entità SENZA MID ma prominenti (brand che
    # Google non riconosce ancora), con filtro anti-frammento-tabellare.
    UNREC_SAL_FLOOR = 0.001

    def _keep(e: dict) -> bool:
        if _is_boilerplate(e["name"]):
            return False
        has_mid = bool(e.get("metadata", {}).get("mid"))
        sal = e.get("salience", 0.0)
        if sal < min_salience:
            return False
        if not only_linked:
            return True
        if has_mid:
            return True
        if show_unrecognized and sal >= UNREC_SAL_FLOOR:
            words = e["name"].split()
            # brand-like: frase di 2-3 parole TUTTE con iniziale maiuscola (nome proprio
            # composto, es. "Magis Energia"), non tutto-maiuscolo (heading). Le singole
            # parole comuni maiuscole a inizio frase (Prezzo, Servizio...) vengono scartate.
            if (2 <= len(words) <= 3 and all(w[:1].isupper() for w in words)
                    and not e["name"].isupper()):
                return True
        return False

    kept_raw = [e for e in raw_entities if _keep(e)]
    # cap sulle non riconosciute per non riempire di brand-frammenti
    if show_unrecognized:
        unrec = [e for e in kept_raw if not e.get("metadata", {}).get("mid")]
        unrec_keep = set(id(e) for e in sorted(unrec, key=lambda e: -e.get("salience", 0))[:8])
        kept_raw = [e for e in kept_raw
                    if e.get("metadata", {}).get("mid") or id(e) in unrec_keep]
    meta["entities_total"] = len(raw_entities)
    meta["entities_kept"] = len(kept_raw)
    if only_linked or min_salience:
        meta["filter"] = f"only_linked={only_linked}, min_salience={min_salience}"

    library = load_provider_library()
    entities: list[dict] = []
    for ent in kept_raw:
        md = ent.get("metadata", {})
        node = {
            "name": ent["name"],
            "type": ent.get("type", "UNKNOWN"),
            "salience": round(ent.get("salience", 0.0), 4),
            "mentions": len(ent.get("mentions", [])),
            "mid": md.get("mid"),
            "recognized": bool(md.get("mid")),
            "wikipedia_url": md.get("wikipedia_url"),
        }
        provider = match_provider(ent["name"], library)
        if provider:
            node["provider_match"] = {
                "nome_canonico": provider["nome_canonico"],
                "wikidata_url": provider.get("wikidata_url"),
                "sameAs": provider.get("sameAs", []),
            }
        kg = enrich_map.get(md.get("mid")) if md.get("mid") else None
        if kg:
            node["kg"] = kg
        entities.append(node)

    # Unisci entità con lo stesso nome: la co-occorrenza è basata sulla stringa, non
    # può distinguere sensi diversi (es. 3 "TotalEnergies" = parent, retailer, variante).
    entities.sort(key=lambda e: -e["salience"])
    merged: dict[str, dict] = {}
    for e in entities:
        primary = merged.get(e["name"])
        if primary is None:
            e["alt_mids"] = []
            merged[e["name"]] = e
        else:
            primary["mentions"] += e["mentions"]
            primary["salience"] = round(primary["salience"] + e["salience"], 4)
            if e["mid"] and e["mid"] != primary["mid"]:
                primary["alt_mids"].append(e["mid"])
    entities = sorted(merged.values(), key=lambda e: -e["salience"])
    edges = build_edges(kept_raw, text)

    # Metriche di grafo (informative per il gate grill-me, NON prescrittive)
    degree: dict[str, int] = {e["name"]: 0 for e in entities}
    for edge in edges:
        if edge["source"] in degree:
            degree[edge["source"]] += 1
        if edge["target"] in degree:
            degree[edge["target"]] += 1
    for e in entities:
        e["degree"] = degree.get(e["name"], 0)
    isolated = [e["name"] for e in entities if e["degree"] == 0]
    # "forti ma poco connesse": nella metà alta per salience ma nella metà bassa per degree
    n = len(entities)
    strong_weak = [
        e["name"] for i, e in enumerate(entities)
        if i < max(1, n // 2) and e["degree"] <= 1 and e["degree"] < (max(degree.values()) if degree else 0)
    ]
    meta["graph"] = {
        "nodes": n,
        "edges": len(edges),
        "isolated": isolated,
        "strong_but_weakly_connected": strong_weak,
    }
    result = {"meta": meta, "entities": entities, "edges": edges}

    if expected_neighbors:
        names = [s.strip() for s in expected_neighbors.split(",") if s.strip()]
        avail = ", ".join(e["name"] for e in entities)
        out_en = []
        for nm in names:
            target = next((e for e in entities if e["name"].lower() == nm.lower()), None)
            if target is None:
                out_en.append({"entity": nm, "error": f"'{nm}' non tra le entità estratte. Disponibili: {avail}"})
            else:
                out_en.append(find_expected_neighbors(target, text, language,
                                                       relevant_only=not neighbors_all))
        result["expected_neighbors"] = out_en
    return result


# --------------------------------------------------------------------------- #
# 6. Rendering                                                                #
# --------------------------------------------------------------------------- #
def to_mermaid(result: dict) -> str:
    lines = ["```mermaid", "graph TD"]
    ids: dict[str, str] = {}
    for i, ent in enumerate(result["entities"]):
        node_id = f"E{i}"
        ids[ent["name"]] = node_id
        label = ent["name"].replace('"', "'")
        tag = "🔗" if ent.get("mid") else ""
        lines.append(f'  {node_id}["{label} {tag}<br/>{ent["type"]} · sal {ent["salience"]}"]')
    for edge in result["edges"]:
        s, t = ids.get(edge["source"]), ids.get(edge["target"])
        if s and t:
            lines.append(f'  {s} ---|{edge["weight"]}| {t}')
    lines.append("```")
    return "\n".join(lines)


HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entity cohesion — __SOURCE__</title>
<style>
  *{box-sizing:border-box;} body{margin:0;font-family:Inter,system-ui,sans-serif;
    color:#212430;background:#fbfbfe;display:flex;height:100vh;overflow:hidden;}
  #graph{flex:1;height:100%;} #panel{width:300px;height:100%;overflow-y:auto;
    background:#fff;border-left:1px solid #ececf3;padding:18px 20px;}
  h1{font-size:15px;margin:0 0 4px;} .sub{font-size:12px;color:#6b7280;margin:0 0 14px;word-break:break-all;}
  h2{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;margin:16px 0 6px;}
  #verdict{border-radius:10px;padding:11px 13px;font-size:12px;line-height:1.45;}
  .row{display:flex;align-items:center;gap:8px;margin:5px 0;font-size:12px;}
  .dot{width:12px;height:12px;border-radius:50%;display:inline-block;}
  .chip{display:inline-block;font-size:12px;padding:2px 8px;border-radius:999px;margin:2px 3px 2px 0;}
  .chip.iso{background:#ffe4ec;color:#be123c;} .chip.frag{background:#fff3e0;color:#b45309;}
  text{font-family:Inter,sans-serif;pointer-events:none;} .node{cursor:grab;}
  .note{font-size:11px;color:#9ca3af;margin-top:16px;line-height:1.5;}
</style></head>
<body>
<svg id="graph"></svg>
<div id="panel">
  <h1>Entity cohesion</h1>
  <p class="sub">__SOURCE__</p>
  <div id="verdict"></div>
  <h2>Connection strength</h2>
  <div class="row"><svg width="34" height="8"><line x1="0" y1="4" x2="34" y2="4" stroke="#3730a3" stroke-width="5"/></svg>Strong (≥5 shared)</div>
  <div class="row"><svg width="34" height="8"><line x1="0" y1="4" x2="34" y2="4" stroke="#8b90f0" stroke-width="3"/></svg>Medium (2–4)</div>
  <div class="row"><svg width="34" height="8"><line x1="0" y1="4" x2="34" y2="4" stroke="#c7c9d9" stroke-width="1.5" stroke-dasharray="3 2"/></svg>Fragile (1 sentence)</div>
  <h2>Node health (semantic co-occurrence, not internal links)</h2>
  <div class="row"><i class="dot" style="background:#ff2056"></i>Isolated — no connection</div>
  <div class="row"><i class="dot" style="background:#fd9a00"></i>Fragile connections only</div>
  <div class="row"><i class="dot" style="background:#02c5ae"></i>Has a solid connection</div>
  <div class="row"><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="#02c5ae" fill-opacity="0.9" stroke="#7c3aed" stroke-width="2.5" stroke-dasharray="3 2"/></svg>Seen but not recognized (no MID)</div>
  <div class="row"><svg width="16" height="16"><circle cx="8" cy="8" r="6" fill="#fff" stroke="#b0b4c8" stroke-width="1.5" stroke-dasharray="2 2"/></svg>Expected but absent (ghost)</div>
  <h2>Isolated entities</h2>
  <div id="isolated"></div>
  <div id="ghostbox"></div>
  <p class="note">Node size ∝ salience · edge weight = shared sentences. Bold ring = hub.
  Drag nodes. Material for the grill-me gate.</p>
</div>
<script>
const DATA = __DATA__;
const svg = document.getElementById('graph');
let W = svg.clientWidth, H = svg.clientHeight;
const nodes = DATA.entities.map((e,i)=>({...e, x:W/2+Math.cos(i*1.8)*130, y:H/2+Math.sin(i*1.8)*130, vx:0, vy:0}));
const idx = {}; nodes.forEach((n,i)=>idx[n.name]=i);
const links = DATA.edges.filter(e=>idx[e.source]!=null&&idx[e.target]!=null)
  .map(e=>({s:idx[e.source],t:idx[e.target],w:e.weight}));
const deg={}, maxw={}; nodes.forEach(n=>{deg[n.name]=0;maxw[n.name]=0;});
for(const l of links){const a=nodes[l.s].name,b=nodes[l.t].name;deg[a]++;deg[b]++;
  maxw[a]=Math.max(maxw[a],l.w);maxw[b]=Math.max(maxw[b],l.w);}
nodes.forEach(n=>{n.hub=deg[n.name]>=5;});
const health = n => deg[n.name]===0?'#ff2056':(maxw[n.name]>=2?'#02c5ae':'#fd9a00');
const etier = w => w>=5?{c:'#3730a3',wd:5,d:''}:w>=2?{c:'#8b90f0',wd:3,d:''}:{c:'#c7c9d9',wd:1.5,d:'3 2'};
const maxSal = Math.max(...nodes.map(n=>n.salience),0.0001);
const radius = n => n.ghost ? 7 : 9 + Math.sqrt(n.salience/maxSal)*24;

// Ghost nodes: expected neighbors (Wikidata/Wikipedia) ABSENT from the page,
// per each entity the user chose to "explode" in the grill-me gate.
const ghostLinks = [];
(DATA.expected_neighbors||[]).forEach(EN=>{
  if(!EN || EN.error || idx[EN.entity]==null) return;
  const ti = idx[EN.entity];
  const cand = (EN.confirmed||[]).concat(EN.wikidata_only||[]).slice(0,16);
  cand.forEach((c,k)=>{
    const gi = nodes.length;
    nodes.push({name:c.name, salience:0, ghost:true, rel:c.wikidata||'',
      x:nodes[ti].x+Math.cos(k*2.4)*90, y:nodes[ti].y+Math.sin(k*2.4)*90, vx:0, vy:0});
    ghostLinks.push({s:ti,t:gi});
  });
});
let alpha=1, drag=null;
function step(){
  for(let i=0;i<nodes.length;i++)for(let j=i+1;j<nodes.length;j++){
    const a=nodes[i],b=nodes[j];let dx=a.x-b.x,dy=a.y-b.y,d2=dx*dx+dy*dy||1,d=Math.sqrt(d2),f=4200/d2;
    a.vx+=f*dx/d;a.vy+=f*dy/d;b.vx-=f*dx/d;b.vy-=f*dy/d;}
  for(const l of links){const a=nodes[l.s],b=nodes[l.t];let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-115)*0.018*(0.5+l.w*0.2);
    a.vx+=f*dx/d;a.vy+=f*dy/d;b.vx-=f*dx/d;b.vy-=f*dy/d;}
  for(const l of ghostLinks){const a=nodes[l.s],b=nodes[l.t];let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)||1,f=(d-70)*0.03;
    a.vx+=f*dx/d;a.vy+=f*dy/d;b.vx-=f*dx/d;b.vy-=f*dy/d;}
  for(const n of nodes){n.vx+=(W/2-n.x)*0.0025;n.vy+=(H/2-n.y)*0.0025;
    if(n===drag)continue;n.x+=n.vx*alpha;n.y+=n.vy*alpha;n.vx*=0.86;n.vy*=0.86;
    n.x=Math.max(38,Math.min(W-38,n.x));n.y=Math.max(26,Math.min(H-26,n.y));}
  alpha*=0.994;if(alpha<0.02)alpha=0.02;
}
function render(){
  let s='';
  for(const l of ghostLinks){const a=nodes[l.s],b=nodes[l.t];
    s+=`<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="#fd9a00" stroke-width="1.2" stroke-dasharray="4 3" opacity="0.6"/>`;}
  for(const l of links){const t=etier(l.w),a=nodes[l.s],b=nodes[l.t];
    s+=`<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="${t.c}" stroke-width="${t.wd}"${t.d?` stroke-dasharray="${t.d}"`:''}/>`;
    if(l.w>=2){const mx=(a.x+b.x)/2,my=(a.y+b.y)/2;
      s+=`<circle cx="${mx}" cy="${my}" r="8" fill="#fff" stroke="${t.c}"/><text x="${mx}" y="${my+3}" text-anchor="middle" font-size="10" fill="${t.c}">${l.w}</text>`;}}
  nodes.forEach((n,i)=>{const r=radius(n);
    if(n.ghost){
      s+=`<g class="node" data-i="${i}"><circle cx="${n.x}" cy="${n.y}" r="${r}" fill="#fff" stroke="#b0b4c8" stroke-width="1.5" stroke-dasharray="2 2"/>`+
         `<text x="${n.x}" y="${n.y-r-4}" text-anchor="middle" font-size="10" font-style="italic" fill="#9096a8">${n.name}</text></g>`;
    } else {
      const unrec = n.recognized===false;
      const stroke = unrec?'#7c3aed':(n.hub?'#212430':'#fff');
      const dash = unrec?' stroke-dasharray="3 2"':'';
      s+=`<g class="node" data-i="${i}"><circle cx="${n.x}" cy="${n.y}" r="${r}" fill="${health(n)}" fill-opacity="0.9" `+
         `stroke="${stroke}" stroke-width="${unrec?3:(n.hub?3:1.5)}"${dash}/>`+
         `<text x="${n.x}" y="${n.y-r-5}" text-anchor="middle" font-size="${n.hub?13:11}" font-weight="${n.hub?'700':'500'}" fill="#212430">${n.name}</text></g>`;
    }});
  svg.innerHTML=s;
}
svg.addEventListener('mousedown',e=>{const g=e.target.closest('.node');if(g){drag=nodes[+g.dataset.i];alpha=0.5;}});
window.addEventListener('mousemove',e=>{if(drag){const r=svg.getBoundingClientRect();drag.x=e.clientX-r.left;drag.y=e.clientY-r.top;}});
window.addEventListener('mouseup',()=>drag=null);
window.addEventListener('resize',()=>{W=svg.clientWidth;H=svg.clientHeight;});
function loop(){step();render();requestAnimationFrame(loop);}
loop();
const g=DATA.meta.graph;
const frag=links.filter(l=>l.w===1).length, tot=links.length;
const v=document.getElementById('verdict');
v.style.background='#f7f7fb'; v.style.border='1px solid #e5e7eb';
v.innerHTML=`<b>Descriptive stats</b> <span style="color:#9ca3af;">(not a quality score)</span><br>`+
  `<span style="color:#4b5563;">${frag} of ${tot} connections are single-sentence · <b>${g.isolated.length}</b> isolated nodes.</span>`;
const chip=(a,c)=>a.length?a.map(x=>`<span class="chip ${c}">${x}</span>`).join(''):'<span class="note">none</span>';
document.getElementById('isolated').innerHTML=chip(g.isolated,'iso');
const ens=(DATA.expected_neighbors||[]).filter(e=>e&&!e.error);
if(ens.length){
  const rows=ens.map(e=>{const n=(e.confirmed||[]).length+(e.wikidata_only||[]).length;
    return `<b>${e.entity}</b>: ${n} absent`;}).join('<br>');
  document.getElementById('ghostbox').innerHTML=
    `<h2>Exploded — expected but absent</h2><p style="font-size:12px;color:#4b5563;margin:0;">${rows}</p>`;
}
</script>
</body></html>
"""


def to_html(result: dict) -> str:
    src = result["meta"]["source"]
    data = json.dumps({"meta": result["meta"], "entities": result["entities"],
                       "edges": result["edges"],
                       "expected_neighbors": result.get("expected_neighbors")},
                      ensure_ascii=False)
    return (HTML_TEMPLATE.replace("__DATA__", data)
            .replace("__SOURCE__", html.escape(src)))


def to_markdown(result: dict) -> str:
    m = result["meta"]
    out = [
        f"# Entity Extraction — {m['source']}",
        "",
        f"- Caratteri: **{m['chars']}** · Unità NLP: **{m['units']}** (free tier: {m['free_tier_units']}/mese)",
        f"- Costo se oltre free tier: **${m['cost_estimate_usd_if_over_free_tier']}**",
    ]
    if m.get("entities_total") is not None:
        out.append(f"- Entità: **{m.get('entities_kept')}** tenute su {m.get('entities_total')} totali"
                   + (f" (filtro: {m['filter']})" if m.get("filter") else ""))
    if m.get("from_cache"):
        out.append("- ⚡ risultato da cache (nessuna chiamata API)")
    if m.get("graph"):
        g = m["graph"]
        out.append(f"- Grafo: **{g['nodes']}** nodi, **{g['edges']}** connessioni, "
                   f"**{len(g['isolated'])}** isolate")
        if g["isolated"]:
            out.append(f"  - Isolate: {', '.join(g['isolated'])}")
        if g["strong_but_weakly_connected"]:
            out.append(f"  - Forti ma poco connesse: {', '.join(g['strong_but_weakly_connected'])}")
    if m.get("mode"):
        out.append(f"- Modalità: {m['mode']}")
        out += ["", "## Testo estratto (preview)", "", result.get("text_preview", "")]
        return "\n".join(out) + "\n"

    out += ["", "## Entità", "",
            "| Entità | Tipo | Salience | Mention | MID | Wikipedia | Provider noto |",
            "|---|---|---|---|---|---|---|"]
    for e in result["entities"]:
        wiki = f"[link]({e['wikipedia_url']})" if e.get("wikipedia_url") else "—"
        mid = e["mid"] or "—"
        prov = e.get("provider_match", {}).get("nome_canonico", "—")
        out.append(f"| {e['name']} | {e['type']} | {e['salience']} | {e['mentions']} "
                   f"| `{mid}` | {wiki} | {prov} |")

    out += ["", "## Connessioni (co-occorrenza per frase)", ""]
    if result["edges"]:
        out += ["| A | B | Frasi condivise |", "|---|---|---|"]
        for edge in result["edges"]:
            out.append(f"| {edge['source']} | {edge['target']} | {edge['weight']} |")
    else:
        out.append("- Nessuna co-occorrenza rilevata.")

    out += ["", "## Grafo", "", to_mermaid(result)]

    for en in result.get("expected_neighbors") or []:
        out += ["", f"## Expected neighbors — {en.get('entity','?')}"]
        if en.get("error"):
            out.append(f"- ⚠️ {en['error']}")
            continue
        out.append(f"Canonically associated with **{en['entity']}** ({en['qid']}) "
                   f"but **absent** from the page:")
        if en["confirmed"]:
            out += ["", "### High confidence (Wikidata + Wikipedia)", ""]
            for v in en["confirmed"]:
                out.append(f"- **{v['name']}** — {v['wikidata']}")
        if en["wikidata_only"]:
            out += ["", "### Wikidata relations", ""]
            for v in en["wikidata_only"]:
                out.append(f"- {v['name']} — {v['wikidata']}")
        if en["wikipedia_only"]:
            out += ["", f"### Wikipedia-linked (broad, {en['wikipedia_only_total']} total — top 25)", ""]
            out.append(", ".join(v["name"] for v in en["wikipedia_only"]))

    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# 7. Gap competitivo (tua pagina vs competitor SERP)                          #
# --------------------------------------------------------------------------- #
def _ekey(e: dict) -> str:
    # Match cross-pagina per NOME normalizzato: unifica varianti MID/no-MID e accenti
    # (es. "Primeo Energie"/"Primeo Énergie", Octopus con/senza MID) → niente falsi gap.
    return _norm(e["name"])


def build_gap(our: dict, competitors: list[dict]) -> dict:
    """Diff entità: cosa coprono i competitor che la nostra pagina non ha, e viceversa."""
    our_keys = {_ekey(e): e for e in our["entities"]}
    display: dict[str, dict] = dict(our_keys)
    comp_count: dict[str, int] = {}
    for comp in competitors:
        for e in comp["entities"]:
            k = _ekey(e)
            comp_count[k] = comp_count.get(k, 0) + 1
            display.setdefault(k, e)
    n = len(competitors)
    gaps = [{"name": display[k]["name"], "mid": display[k].get("mid"),
             "on_competitors": c, "recognized": display[k].get("recognized", True)}
            for k, c in comp_count.items() if k not in our_keys]
    gaps.sort(key=lambda g: (-g["on_competitors"], g["name"].lower()))
    shared = [{"name": display[k]["name"], "on_competitors": comp_count[k]}
              for k in our_keys if k in comp_count]
    shared.sort(key=lambda g: -g["on_competitors"])
    our_unique = [{"name": e["name"], "recognized": e.get("recognized", True)}
                  for k, e in our_keys.items() if k not in comp_count]
    return {"competitors": n, "gaps": gaps, "shared": shared, "our_unique": our_unique}


def render_gap(gap: dict, our_url: str) -> str:
    n = gap["competitors"]
    out = [f"# Competitive entity gap", "", f"Base (you): {our_url}",
           f"Competitors analyzed: {n}", "",
           "## Coverage gaps — on competitors, MISSING on your page", "",
           "| Entity | On # competitors | MID |", "|---|---|---|"]
    for g in gap["gaps"]:
        mid = g["mid"] or ("—" if g["recognized"] else "no MID")
        out.append(f"| {g['name']} | {g['on_competitors']}/{n} | {mid} |")
    if not gap["gaps"]:
        out.append("| _(none)_ | | |")
    out += ["", "## Shared (you + competitors) — table stakes present", "",
            ", ".join(f"{s['name']} ({s['on_competitors']}/{n})" for s in gap["shared"]) or "_(none)_"]
    out += ["", "## Your-unique (only on your page) — differentiator or off-topic?", "",
            ", ".join(e["name"] for e in gap["our_unique"]) or "_(none)_"]
    return "\n".join(out) + "\n"


def main() -> int:
    try:  # console Windows (cp1252) non regge emoji/accenti — forza UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    p = argparse.ArgumentParser(description="Estrazione entità + connessioni via Google NLP.")
    p.add_argument("source", help="URL, file .html o .txt")
    p.add_argument("--api-key", help="Google API key (o env GOOGLE_NLP_API_KEY)")
    p.add_argument("--language", default="it")
    p.add_argument("--no-enrich", action="store_true", help="Salta Knowledge Graph Search")
    p.add_argument("--no-cache", action="store_true", help="Ignora la cache locale")
    p.add_argument("--no-google", action="store_true", help="Solo estrazione testo, zero chiamate API")
    p.add_argument("--only-linked", action="store_true",
                   help="Solo entità con MID (agganciate al Knowledge Graph) — taglia il rumore")
    p.add_argument("--min-salience", type=float, default=0.0,
                   help="Scarta entità sotto questa salience (es. 0.01)")
    p.add_argument("--json", action="store_true", help="Output JSON invece di Markdown")
    p.add_argument("--html", metavar="PATH", help="Scrive il grafo force-directed in un file HTML self-contained")
    p.add_argument("--expected-neighbors", metavar="ENTITIES",
                   help="Una o più entità (nomi esatti, separati da virgola) di cui trovare i vicini "
                        "canonici (Wikidata+Wikipedia) assenti in pagina")
    p.add_argument("--neighbors-all", action="store_true",
                   help="Con --expected-neighbors: mostra TUTTO (no filtro relazioni, include Wikipedia-only)")
    p.add_argument("--show-unrecognized", action="store_true",
                   help="Include entità prominenti SENZA MID (brand che Google vede ma non riconosce)")
    p.add_argument("--gap-vs", metavar="URLS",
                   help="URL competitor (separati da virgola): calcola il gap entità tua-pagina vs competitor")
    args = p.parse_args()

    if args.gap_vs:
        api_key = resolve_api_key(args.api_key)
        common = dict(api_key=api_key, enrich=False, use_cache=not args.no_cache,
                      no_google=False, language=args.language, only_linked=True,
                      show_unrecognized=args.show_unrecognized)
        try:
            our = run(source=args.source, **common)
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"[ERROR] pagina base: {exc}", file=sys.stderr)
            return 2
        comps = []
        for u in (x.strip() for x in args.gap_vs.split(",") if x.strip()):
            try:
                comps.append(run(source=u, **common))
            except (FileNotFoundError, RuntimeError) as exc:
                print(f"[WARN] competitor saltato ({u}): {exc}", file=sys.stderr)
        if not comps:
            print("[ERROR] nessun competitor raggiungibile.", file=sys.stderr)
            return 2
        gap = build_gap(our, comps)
        if args.json:
            print(json.dumps({"our": args.source, "gap": gap}, ensure_ascii=False, indent=2))
        else:
            print(render_gap(gap, args.source))
        return 0

    try:
        result = run(
            source=args.source,
            api_key=resolve_api_key(args.api_key),
            enrich=not args.no_enrich,
            use_cache=not args.no_cache,
            no_google=args.no_google,
            language=args.language,
            only_linked=args.only_linked,
            min_salience=args.min_salience,
            expected_neighbors=args.expected_neighbors,
            neighbors_all=args.neighbors_all,
            show_unrecognized=args.show_unrecognized,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if args.html:
        if result["meta"].get("mode"):
            print("[ERROR] --html non disponibile in modalità --no-google", file=sys.stderr)
            return 2
        Path(args.html).expanduser().write_text(to_html(result), encoding="utf-8")
        print(f"[OK] Grafo scritto in {args.html}")

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
