"""
Bollettino della Gazzetta Ufficiale (Serie Generale).

Per ogni edizione:
1. Trova il NUMERO GIUSTO della Serie Generale (prima veniva confuso con le serie speciali:
   il 5, 6 e 7 ottobre erano stati letti la 2ª serie UE, i Concorsi e la Corte costituzionale).
2. Legge l'indice ufficiale dell'edizione: elenco completo degli atti con codice redazionale.
3. Per leggi, decreti-legge e decreti legislativi scarica il TESTO dell'atto (dal PDF dell'edizione)
   e la data di entrata in vigore indicata dalla Gazzetta.
4. Gemini sceglie gli atti delle materie seguite e scrive le schede partendo dal testo vero.
5. Salva data/AAAA-MM-GG.json e aggiorna data/index.json.
"""

import io
import os
import re
import json
import time
import html
import datetime as dt
from zoneinfo import ZoneInfo

import requests
from google import genai
from google.genai import types

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

ROME = ZoneInfo("Europe/Rome")
BASE = "https://www.gazzettaufficiale.it"
DATA_DIR = "data"
VERSION = 2

BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "10"))
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "4"))
FORCE_DATE = os.environ.get("GU_DATE", "").strip()

MODELS = ["gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro", "gemini-3.5-flash-lite"]
GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9",
})

CODE_RE = r"\d{2}[A-Z]\d{5}"
PRIMARY = ("LEGGE", "DECRETO-LEGGE", "DECRETO LEGISLATIVO", "LEGGE COSTITUZIONALE")


def get(url, timeout=30, binary=False):
    for attempt in range(3):
        try:
            r = HTTP.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.content if binary else r.text
            print(f"   ⚠️ HTTP {r.status_code}: {url[:110]}")
            if r.status_code == 404:
                return None
        except requests.RequestException as e:
            print(f"   ⚠️ {type(e).__name__}: {url[:110]}")
        time.sleep(3 * (attempt + 1))
    return None


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def giorno_it(d):
    return GIORNI[d.weekday()]


def is_publication_day(d):
    """La Serie Generale esce dal lunedì al sabato, non nei giorni festivi nazionali."""
    if d.weekday() == 6:
        return False
    fixed = {(1, 1), (1, 6), (4, 25), (5, 1), (6, 2), (8, 15), (11, 1), (12, 8), (12, 25), (12, 26)}
    return (d.month, d.day) not in fixed


# ---------------------------------------------------------------------------
# 1. Numero dell'edizione
# ---------------------------------------------------------------------------
def anchor_from_rss():
    """Data e numero dell'ultima Serie Generale, letti dal feed RSS ufficiale + pagina ELI."""
    xml = get(f"{BASE}/rss/SG")
    if not xml:
        return None
    m = re.search(r"/eli/id/(\d{4})/(\d{2})/(\d{2})/(" + CODE_RE + r")/SG", xml, re.I)
    if not m:
        return None
    y, mo, d, code = m.groups()
    page = get(f"{BASE}/eli/id/{y}/{mo}/{d}/{code}/sg")
    if not page:
        return None
    n = re.search(rf"/eli/gu/{y}/{mo}/{d}/(\d+)/sg", page, re.I)
    if not n:
        return None
    return dt.date(int(y), int(mo), int(d)), int(n.group(1))


def known_anchors():
    """Coppie data/numero già verificate nelle edizioni salvate."""
    out = []
    for name in os.listdir(DATA_DIR):
        if not re.match(r"\d{4}-\d{2}-\d{2}\.json$", name):
            continue
        try:
            with open(os.path.join(DATA_DIR, name), encoding="utf-8") as f:
                d = json.load(f)
            if d.get("verificato") and str(d.get("numero_gu", "")).isdigit():
                out.append((dt.date.fromisoformat(d["date"]), int(d["numero_gu"])))
        except Exception:
            pass
    return out


def estimate_number(target, anchors):
    best = min(anchors, key=lambda a: abs((a[0] - target).days))
    a_date, a_num = best
    step = 1 if target > a_date else -1
    n, d = a_num, a_date
    while d != target:
        d += dt.timedelta(days=step)
        if is_publication_day(d):
            n += step
    return n


def issue_page(date, num):
    url = (f"{BASE}/gazzetta/serie_generale/caricaDettaglio/home?"
           f"dataPubblicazioneGazzetta={date.isoformat()}&numeroGazzetta={num}")
    page = get(url)
    if not page:
        return None, url
    ok_heading = re.search(rf"n\.\s*{num}\s+del\s+0?{date.day}-{date.month:02d}-{date.year}", strip_tags(page))
    ok_acts = f"atto.dataPubblicazioneGazzetta={date.isoformat()}" in page
    return (page if (ok_heading or ok_acts) else None), url


def find_issue(date, anchors):
    if not anchors:
        return None, None, None
    est = estimate_number(date, anchors)
    for delta in (0, -1, 1, -2, 2, -3, 3):
        num = est + delta
        page, url = issue_page(date, num)
        if page:
            print(f"   ✅ Serie Generale n. {num} del {date.strftime('%d/%m/%Y')} (stima {est})")
            return num, page, url
    return None, None, None


# ---------------------------------------------------------------------------
# 2. Elenco degli atti e testi
# ---------------------------------------------------------------------------
def parse_acts(page_html, date):
    """Dall'indice ufficiale: sezione, intestazione, titolo e codice di ogni atto."""
    text = strip_tags(page_html)
    # ogni atto nell'indice termina con il codice redazionale tra parentesi
    parts = re.split(rf"\(({CODE_RE})\)", text)
    acts, seen = [], set()
    for i in range(1, len(parts), 2):
        code, chunk = parts[i], parts[i - 1]
        if code in seen:
            continue
        seen.add(code)
        chunk = re.sub(r"\s*Pag\.\s*\d+\s*$", "", chunk)
        chunk = re.sub(r"^\s*Pag\.\s*\d+\s*", "", chunk).strip()
        chunk = chunk[-700:]
        m = re.search(r"(LEGGE COSTITUZIONALE|DECRETO-LEGGE|DECRETO LEGISLATIVO|LEGGE|DECRETO DEL PRESIDENTE "
                      r"DEL CONSIGLIO DEI MINISTRI|DECRETO DEL PRESIDENTE DELLA REPUBBLICA|DECRETO|DELIBERA|"
                      r"ORDINANZA|PROVVEDIMENTO|COMUNICATO|DETERMINA|CIRCOLARE)\b", chunk)
        head = chunk[m.start():] if m else chunk
        # l'ente che emana l'atto (es. "Ministero della Salute") sta subito prima dell'intestazione
        pre = chunk[:m.start()].strip() if m else ""
        if pre and len(pre) < 160 and "Serie Generale" not in pre and not re.search(r"\d{4}", pre):
            head = f"{pre} - {head}"
        acts.append({
            "codice": code,
            "intestazione": head[:600],
            "tipo": m.group(1) if m else "",
            "link": f"{BASE}/eli/id/{date.year}/{date.month:02d}/{date.day:02d}/{code}/sg",
        })
    return acts


def pdf_texts(date, num, codes):
    """Testo degli atti ricavato dal PDF dell'edizione, tagliato sui codici redazionali."""
    if not PdfReader or not codes:
        return {}
    data = get(f"{BASE}/eli/gu/{date.year}/{date.month:02d}/{date.day:02d}/{num}/sg/pdf", timeout=90, binary=True)
    if not data or not data[:5] == b"%PDF-":
        print("   ⚠️ PDF dell'edizione non disponibile.")
        return {}
    try:
        reader = PdfReader(io.BytesIO(data))
        full = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:
        print(f"   ⚠️ PDF non leggibile: {e}")
        return {}
    full = re.sub(r"[ \t]+", " ", full)
    # ultima comparsa di ciascun codice = fine del testo dell'atto (la prima è nel sommario)
    last, marks = {}, []
    for m in re.finditer(rf"\b({CODE_RE})\b", full):
        last[m.group(1)] = (m.start(), m.end())
        marks.append(m.end())
    out = {}
    for code in codes:
        if code not in last:
            continue
        s0, end = last[code]
        start = max([p for p in marks if p <= s0] or [0])
        out[code] = full[start:end].strip()[:25000]
    print(f"   📄 Testo letto dal PDF per {len(out)}/{len(codes)} atti principali.")
    return out


def entry_into_force(act):
    page = get(act["link"])
    if not page:
        return ""
    m = re.search(r"Entrata in vigore del provvedimento:\s*(\d{2}/\d{2}/\d{4})", strip_tags(page))
    return m.group(1) if m else ""


def first_article(date, act):
    url = (f"{BASE}/atto/serie_generale/caricaArticoloDefault/originario?"
           f"atto.dataPubblicazioneGazzetta={date.isoformat()}&atto.codiceRedazionale={act['codice']}"
           f"&atto.tipoProvvedimento={requests.utils.quote(act['tipo'])}")
    page = get(url)
    return strip_tags(page)[:15000] if page else ""


# ---------------------------------------------------------------------------
# 3. Analisi con Gemini
# ---------------------------------------------------------------------------
SYSTEM = """
Sei un giurista esperto di legislazione italiana e scrivi per professionisti e cittadini informati.
Analizzi la Gazzetta Ufficiale (Serie Generale) di un giorno.

MATERIE SEGUITE
- economico, fiscale, finanziario, pubblica amministrazione ed enti territoriali  → categoria "Economico"
- giustizia, procedura, reati                                                   → categoria "Giustizia"
- energia, ambiente, sostenibilità, imballaggi, compostabilità, rifiuti          → categoria "Energia e Ambiente"

COSA SCHEDARE
- Leggi, decreti-legge e decreti legislativi delle materie seguite: una scheda completa ciascuno.
- Leggi, decreti-legge e decreti legislativi di altre materie: in "scartate", con la materia.
- Fra gli altri atti (decreti ministeriali, delibere, provvedimenti di autorità) elenca in "altri_atti"
  solo quelli rilevanti per le materie seguite, con una riga di sintesi ciascuno.

REGOLE
- Scrivi le schede partendo dal TESTO dell'atto fornito: articoli, commi, importi, scadenze, soggetti.
  Cita gli articoli ("Art. 3, comma 2: ...").
- Se di un atto hai solo il titolo, dillo nel campo "contesto" e non inventare misure: scrivi
  in "misure" solo ciò che risulta dal titolo.
- Mai formule ipotetiche come "potenzialmente", "e/o", "potrebbe prevedere".
- Per la decorrenza usa la data di entrata in vigore fornita, se presente.
- Usa i codici redazionali esattamente come forniti.

Restituisci SOLO JSON valido con questa struttura:
{
  "sintesi": "2-3 frasi sull'edizione nel suo complesso",
  "schede": [{
     "codice_atto": "...", "titolo": "titolo esplicativo breve", "riferimento": "es. DECRETO LEGISLATIVO 25 settembre 2026, n. 176",
     "categoria": "Economico | Giustizia | Energia e Ambiente", "iter": "es. In vigore dal ... / Decreto-legge da convertire entro il ...",
     "contesto": "...", "misure": ["Art. 1: ...", "..."], "chi_interessato": "...", "decorrenza": "...",
     "perche_conta": "...", "fonte_testo": "testo integrale | primo articolo | solo titolo"
  }],
  "scartate": [{"codice_atto": "...", "materia": "...", "titolo": "..."}],
  "altri_atti": [{"codice_atto": "...", "categoria": "...", "titolo": "...", "sintesi": "..."}]
}
"""


def call_gemini(prompt):
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    for model in MODELS:
        for attempt in range(3):
            try:
                print(f"   🤖 {model} (tentativo {attempt + 1})...")
                cfg = types.GenerateContentConfig(
                    system_instruction=SYSTEM, temperature=0.2, max_output_tokens=60000,
                    response_mime_type="application/json",
                    thinking_config=types.ThinkingConfig(thinking_budget=2048),
                )
                r = client.models.generate_content(model=model, contents=prompt, config=cfg)
                return json.loads(r.text)
            except Exception as e:
                msg = str(e)
                print(f"   ⚠️ {model}: {msg[:200]}")
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                    # limite al minuto: si aspetta e si riprova con lo stesso modello
                    if "per day" in msg.lower() or "PerDay" in msg:
                        break
                    print("   ⏳ Limite di richieste al minuto: attendo 65 secondi...")
                    time.sleep(65)
                elif "404" in msg or "NOT_FOUND" in msg:
                    break
                else:
                    time.sleep(5)
    raise RuntimeError("Nessun modello Gemini ha risposto.")


def build_prompt(date, num, acts, texts, vigore):
    lines = [f"Gazzetta Ufficiale, Serie Generale n. {num} del {date.strftime('%d/%m/%Y')} "
             f"({giorno_it(date)}). Atti dell'edizione ({len(acts)}):", ""]
    for a in acts:
        lines.append(f"[{a['codice']}] {a['intestazione']}")
        if a["codice"] in vigore and vigore[a["codice"]]:
            lines.append(f"   Entrata in vigore: {vigore[a['codice']]}")
        if a["codice"] in texts:
            src, txt = texts[a["codice"]]
            lines.append(f"   TESTO ({src}):\n{txt}\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. Elaborazione di una data
# ---------------------------------------------------------------------------
def process(date, anchors):
    print(f"\n━━ {giorno_it(date)} {date.isoformat()} ━━")
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = {"date": date.isoformat(), "giorno": giorno_it(date), "updatedAt": now, "version": VERSION}

    num, page, url = find_issue(date, anchors)
    if not num:
        print("   ⏳ Edizione non ancora disponibile.")
        return {**base, "stato": "non_disponibile", "numero_gu": "", "schede": [], "scartate": [],
                "altri_atti": [], "atti": [],
                "note": "Edizione non ancora pubblicata o non raggiungibile al momento del controllo: "
                        "verrà ricontrollata automaticamente."}

    acts = parse_acts(page, date)
    print(f"   📑 {len(acts)} atti nell'indice.")
    primary = [a for a in acts if a["tipo"] in PRIMARY]

    texts = {c: ("testo integrale", t) for c, t in pdf_texts(date, num, [a["codice"] for a in primary]).items()}
    vigore = {}
    for a in primary:
        vigore[a["codice"]] = entry_into_force(a)
        if a["codice"] not in texts:
            t = first_article(date, a)
            if t:
                texts[a["codice"]] = ("primo articolo", t)

    res = call_gemini(build_prompt(date, num, acts, texts, vigore)) if acts else {}
    by_code = {a["codice"]: a for a in acts}

    def fix(items):
        out = []
        for it in items or []:
            code = str(it.get("codice_atto", "")).strip().upper()
            it["codice_atto"] = code
            it["link"] = by_code[code]["link"] if code in by_code else url
            if code in vigore and vigore[code] and "vigore" not in it:
                it["vigore"] = vigore[code]
            out.append(it)
        return out

    schede = fix(res.get("schede"))
    data = {
        **base,
        "stato": "con_schede" if schede else "nessuna_legge_interesse",
        "numero_gu": str(num),
        "verificato": True,
        "link_gu": url,
        "pdf_gu": f"{BASE}/eli/gu/{date.year}/{date.month:02d}/{date.day:02d}/{num}/sg/pdf",
        "sintesi": res.get("sintesi", ""),
        "schede": schede,
        "scartate": fix(res.get("scartate")),
        "altri_atti": fix(res.get("altri_atti")),
        "atti": [{"codice": a["codice"], "titolo": a["intestazione"], "link": a["link"]} for a in acts],
    }
    print(f"   ✅ {len(schede)} schede, {len(data['scartate'])} scartate, {len(data['altri_atti'])} altri atti.")
    return data


def load(date):
    p = os.path.join(DATA_DIR, f"{date.isoformat()}.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def needs_work(date, today):
    d = load(date)
    if d is None:
        return True
    if d.get("version", 0) < VERSION or not d.get("verificato"):
        return True   # edizioni fatte con il vecchio metodo (numero sbagliato)
    if d.get("stato") == "non_disponibile":
        return True
    return date == today and d.get("stato") != "con_schede" and not d.get("atti")


def save(data):
    old = load(dt.date.fromisoformat(data["date"])) or {}
    # conserva l'audio se il contenuto non è cambiato
    if old.get("audio") and old.get("schede") == data.get("schede") and old.get("altri_atti") == data.get("altri_atti"):
        data["audio"], data["duration"] = old["audio"], old.get("duration")
    with open(os.path.join(DATA_DIR, f"{data['date']}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_index():
    idx = []
    for name in sorted(os.listdir(DATA_DIR), reverse=True):
        if re.match(r"\d{4}-\d{2}-\d{2}\.json$", name):
            d = load(dt.date.fromisoformat(name[:10]))
            if d:
                idx.append({"date": d["date"], "giorno": d.get("giorno", ""), "numero_gu": str(d.get("numero_gu", "")),
                            "stato": d.get("stato", ""), "schede": len(d.get("schede") or [])})
    with open(os.path.join(DATA_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=2)


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY non impostata.")
    os.makedirs(DATA_DIR, exist_ok=True)
    today = dt.datetime.now(ROME).date()

    anchors = known_anchors()
    rss = anchor_from_rss()
    if rss:
        print(f"Ultima Serie Generale dal feed: n. {rss[1]} del {rss[0].strftime('%d/%m/%Y')}")
        anchors.append(rss)
    if not anchors:
        anchors = [(dt.date(2026, 10, 7), 233)]   # riferimento verificato manualmente

    if FORCE_DATE:
        dates = [dt.date.fromisoformat(FORCE_DATE)]
    else:
        cands = [today - dt.timedelta(days=k) for k in range(BACKFILL_DAYS)]
        dates = [d for d in cands if is_publication_day(d) and needs_work(d, today)][:MAX_PER_RUN]
    print("Da elaborare:", ", ".join(d.isoformat() for d in dates) or "nessuna data")

    for k, d in enumerate(dates):
        if k:
            time.sleep(30)   # pausa tra un'edizione e l'altra, per non superare i limiti di Gemini
        try:
            data = process(d, anchors)
            save(data)
            if data.get("verificato"):
                anchors.append((d, int(data["numero_gu"])))
        except Exception as e:
            print(f"❌ {d}: {e}")
    update_index()


if __name__ == "__main__":
    main()
