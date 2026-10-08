"""
Digest quotidiano di Radio Radicale.

Come funziona:
1. Legge direttamente l'agenda del giorno su radioradicale.it e apre la scheda di ogni
   registrazione (descrizione, elenco degli interventi con oratori e orari, eventuale trascrizione).
2. Per le registrazioni più importanti scarica l'audio e lo fa ascoltare a Gemini, che annota
   chi ha parlato e cosa ha sostenuto. È questo che rende l'analisi concreta e non generica.
3. Con tutto il materiale raccolto Gemini scrive il digest tematico, con i link alle schede
   come fonti. Le fonti vengono salvate anche a parte, così la pagina web le mostra sempre.
"""

import os
import re
import json
import time
import shutil
import subprocess
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urlparse, urljoin
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from google.genai import errors

DIGESTS_DIR = "digests"
INDEX_PATH = os.path.join(DIGESTS_DIR, "index.json")
BASE = "https://www.radioradicale.it"

# Quanti giorni indietro controllare per recuperare digest mancanti
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "7"))
# Massimo numero di digest generati per esecuzione (per non esaurire la quota)
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "2"))
# Quante schede di registrazione aprire al massimo per giorno
MAX_SCHEDE = int(os.environ.get("MAX_SCHEDE", "60"))
# Ascolto audio: quante registrazioni e quanti minuti ciascuna (0 = disattivato)
AUDIO_MAX_RECORDINGS = int(os.environ.get("AUDIO_MAX_RECORDINGS", "1"))
AUDIO_MAX_MINUTES = int(os.environ.get("AUDIO_MAX_MINUTES", "60"))
# Tempo massimo per scaricare ciascun audio (secondi) e per tutta la fase di ascolto di un giorno
AUDIO_DOWNLOAD_TIMEOUT = int(os.environ.get("AUDIO_DOWNLOAD_TIMEOUT", "480"))
AUDIO_TOTAL_BUDGET = int(os.environ.get("AUDIO_TOTAL_BUDGET", "1500"))

MODELS_TO_TRY = [
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 digest-radicale/2.0"
)
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "it-IT,it;q=0.9"})

SYSTEM_INSTRUCTION = (
    "Sei un giornalista parlamentare e giudiziario esperto, con anni di lavoro a Radio Radicale. "
    "Scrivi analisi concrete e dettagliate, mai generiche: ogni frase deve contenere un fatto preciso "
    "(chi, cosa, quando, quale atto, quale cifra, quale argomento). Evita formule vuote come "
    "'momento significativo', 'tema centrale', 'ampio dibattito', 'offre approfondimenti'. "
    "Riporta nomi, ruoli e gruppi di chi interviene e la sostanza dei loro argomenti. "
    "Distingui i fatti dalle opinioni. Non inventare mai nomi, citazioni, dati o link: "
    "se un'informazione non è nel materiale fornito o in una fonte verificabile, non scriverla."
)

# Parole chiave per scegliere quali registrazioni ascoltare (le più "di sostanza")
AUDIO_PRIORITY = [
    "seduta", "commissione", "audizione", "processo", "udienza", "conferenza stampa",
    "convegno", "congresso", "presentazione", "dibattito", "assemblea", "consiglio",
    "comitato", "direzione", "tavola rotonda", "incontro", "intervista",
]
AUDIO_SKIP = [
    "notiziario", "rassegna", "stampa e regime", "replica", "radiogiornale", "gr ",
    "musica", "sigla", "fai notizia",
]


# --------------------------------------------------------------------------- #
#  Chiamate a Gemini con modelli di riserva
# --------------------------------------------------------------------------- #
def call_gemini(client: genai.Client, contents, config: types.GenerateContentConfig):
    """Prova i modelli in ordine; passa al successivo in caso di 429, 503, 404/400 o risposta vuota."""
    last_error = None
    for model in MODELS_TO_TRY:
        try:
            print(f"   🔄 Modello '{model}'...")
            response = client.models.generate_content(model=model, contents=contents, config=config)
            text = (response.text or "").strip()
            if not text:
                print(f"   ⚠️ Risposta vuota da '{model}'.")
                continue
            print(f"   ✅ Risposta da '{model}'")
            return text, response
        except errors.APIError as e:
            msg = str(e)
            last_error = e
            if e.code == 429 or "RESOURCE_EXHAUSTED" in msg:
                reason = "quota esaurita (429)"
            elif e.code == 503 or "UNAVAILABLE" in msg:
                reason = "server sovraccarico (503)"
            elif e.code in (404, 400) or "NOT_FOUND" in msg:
                reason = "modello non disponibile (404/400)"
            else:
                raise
            print(f"   ⚠️ {reason} per '{model}'.")
            time.sleep(3)
    raise RuntimeError(f"Nessun modello disponibile. Ultimo errore: {last_error}")


# --------------------------------------------------------------------------- #
#  1. Lettura dell'agenda e delle schede di Radio Radicale
# --------------------------------------------------------------------------- #
SCHEDA_RE = re.compile(r"/scheda/(\d+)/[^\s\"'#?]+")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")
INTERVENT_RE = re.compile(r"^\s*(\d{1,2}:\d{2}:\d{2})\s+(\d{1,2}:\d{2})\s+(.+)$")


def get_html(url: str, timeout: int = 30) -> str:
    for attempt in range(3):
        try:
            r = HTTP.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            print(f"   ⚠️ {url} → HTTP {r.status_code}")
            if r.status_code in (404, 410):
                return ""
        except requests.RequestException as e:
            print(f"   ⚠️ {url} → {e}")
        time.sleep(2 * (attempt + 1))
    return ""


def clean_text(s: str) -> str:
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def scrape_agenda(date_iso: str) -> list:
    """Elenco delle registrazioni in agenda: titolo, ora, luogo, descrizione breve, link alla scheda."""
    html = get_html(f"{BASE}/agenda?data={date_iso}")
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()

    for a in soup.find_all("a", href=True):
        m = SCHEDA_RE.search(a["href"])
        if not m:
            continue
        sid = m.group(1)
        title = clean_text(a.get_text(" "))
        # salta i link "leggi tutto" e simili: il titolo vero è nel primo link della riga
        if sid in seen or not title or title.lower() in ("leggi tutto", "video", "audio"):
            continue
        seen.add(sid)

        # la "riga" dell'agenda: risale fino a un contenitore con ora e luogo
        row = a.find_parent("tr") or a.find_parent("li") or a.find_parent("div")
        row_text = clean_text(row.get_text("\n")) if row else title
        tm = TIME_RE.search(row_text)
        place = ""
        if row and row.name == "tr":
            cells = row.find_all(["td", "th"])
            if cells:
                place = clean_text(cells[0].get_text(" "))

        desc = row_text.replace(title, "").strip()
        items.append({
            "id": sid,
            "title": title,
            "url": urljoin(BASE, m.group(0)),
            "time": f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else "",
            "place": place,
            "agenda_text": desc[:800],
            "in_table": bool(row and row.name == "tr"),
        })

    # La pagina contiene anche riquadri laterali ("ultimi inseriti", rubriche di altri giorni):
    # se l'agenda vera è una tabella, si tengono solo le righe della tabella.
    in_table = [i for i in items if i["in_table"]]
    if in_table:
        skipped = len(items) - len(in_table)
        if skipped:
            print(f"   (scartati {skipped} link estranei all'agenda del giorno)")
        items = in_table
    return items


def clean_markdown(md: str, date_it: str) -> str:
    """Toglie eventuali ragionamenti del modello prima del digest e garantisce il titolo."""
    md = re.sub(r"^```(?:markdown)?\s*|\s*```\s*$", "", md.strip())
    m = re.search(r"^# Radio Radicale.*$", md, re.M) or re.search(r"^## In breve.*$", md, re.M)
    if m and m.start() > 0:
        print(f"   🧽 Rimosse {m.start()} battute di testo prima del digest.")
        md = md[m.start():]
    if not md.lstrip().startswith("# "):
        md = f"# Radio Radicale, {date_it}\n*Analisi tematica delle registrazioni della giornata.*\n\n" + md
    return md


def scrape_scheda(url: str) -> dict:
    """Contenuto di una scheda: descrizione, interventi (oratore e orario), trascrizione, flusso audio."""
    html = get_html(url)
    if not html:
        return {}
    m3u8 = re.findall(r"https?://[^\s\"'<>]+?\.m3u8", html)
    mp3 = re.findall(r"https?://[^\s\"'<>]+?\.mp3", html)

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "iframe"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    lines = [l.strip() for l in main.get_text("\n").split("\n") if l.strip()]

    interventions = []
    for l in lines:
        mm = INTERVENT_RE.match(l)
        if mm:
            interventions.append(f"{mm.group(2)} ({mm.group(1)}) {mm.group(3)}")
    if not interventions:
        # variante: durata, orario e oratore su tre righe separate
        for i in range(len(lines) - 2):
            if (re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", lines[i])
                    and re.fullmatch(r"\d{1,2}:\d{2}", lines[i + 1])):
                interventions.append(f"{lines[i + 1]} ({lines[i]}) {lines[i + 2]}")

    # trascrizione automatica, se presente nella pagina
    transcript = ""
    for i, l in enumerate(lines):
        if l.lower().startswith("trascrizione automatica"):
            tail = []
            for t in lines[i + 1:]:
                if t.upper().startswith(("CONDIVIDI", "INCORPORA", "COPIA LINK")):
                    break
                tail.append(t)
            transcript = " ".join(tail)
            break

    text = "\n".join(lines)
    # taglia i menu laterali ricorrenti
    for stop in ("Chi siamo", "CONDIVIDI QUESTO", "INCORPORA PLAYER"):
        pos = text.find(stop)
        if pos > 400:
            text = text[:pos]

    return {
        "text": text[:6000],
        "interventions": interventions[:150],
        "transcript": transcript[:20000],
        "stream": (mp3 or m3u8 or [""])[0],
    }


# --------------------------------------------------------------------------- #
#  2. Ascolto dell'audio delle registrazioni principali
# --------------------------------------------------------------------------- #
def audio_score(item: dict) -> int:
    t = (item["title"] + " " + item.get("agenda_text", "")).lower()
    if any(k in t for k in AUDIO_SKIP):
        return -1
    score = sum(5 for k in AUDIO_PRIORITY if k in t)
    score += min(len(item.get("interventions", [])), 30)  # più oratori = più contenuto
    if len(item.get("transcript", "")) > 3000:
        score -= 20  # c'è già una trascrizione corposa, l'audio serve meno
    return score


def download_audio(stream: str, minutes: int, out_path: str) -> bool:
    if not shutil.which("ffmpeg"):
        print("   ⚠️ ffmpeg non installato: ascolto audio saltato.")
        return False
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-user_agent", USER_AGENT,
        "-i", stream, "-t", str(minutes * 60), "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "32k", out_path,
    ]
    try:
        # al massimo 8 minuti di download: se il server è lento si usa la parte già scaricata
        subprocess.run(cmd, check=True, timeout=AUDIO_DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        print("   ⏱️ Download lento: uso la parte di audio già scaricata.")
    except Exception as e:
        print(f"   ⚠️ Download audio fallito: {e}")
        return False
    ok = os.path.exists(out_path) and os.path.getsize(out_path) > 50_000
    if ok:
        print(f"   📦 Audio scaricato: {os.path.getsize(out_path) // 1024} KB")
    return ok


def listen_recording(client: genai.Client, item: dict) -> str:
    """Gemini ascolta l'audio e annota chi parla e cosa sostiene."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "audio.mp3")
        print(f"   🎧 Scarico {AUDIO_MAX_MINUTES} min di audio: {item['title'][:70]}")
        if not download_audio(item["stream"], AUDIO_MAX_MINUTES, path):
            return ""
        uploaded = client.files.upload(file=path, config={"mime_type": "audio/mpeg"})
        try:
            for _ in range(60):
                state = str(getattr(uploaded, "state", "")).upper()
                if "PROCESSING" not in state:
                    break
                time.sleep(5)
                uploaded = client.files.get(name=uploaded.name)

            speakers = "\n".join(item.get("interventions", [])[:80]) or "non disponibile"
            prompt = f"""
Ascolta questa registrazione di Radio Radicale (primi {AUDIO_MAX_MINUTES} minuti).
Titolo: {item['title']}
Ora di inizio: {item.get('time') or 'n.d.'}

Elenco ufficiale degli interventi (orario di inizio, durata, oratore e gruppo), da usare per dare un nome alle voci:
{speakers}

Scrivi in italiano APPUNTI DETTAGLIATI, intervento per intervento, nell'ordine:
- nome, ruolo e gruppo di chi parla (usa l'elenco sopra; se non sei sicuro scrivi "oratore non identificato")
- la tesi principale e gli argomenti a sostegno
- dati, cifre, articoli di legge, atti, nomi, date citati
- proposte, richieste, accuse, repliche, annunci
- al massimo una o due brevi frasi testuali per intervento, solo se le senti chiaramente, tra virgolette
Alla fine aggiungi: esito di eventuali votazioni o decisioni e prossimi passaggi annunciati.
Non riassumere in modo generico: riporta il contenuto concreto di ciò che viene detto.
"""
            config = types.GenerateContentConfig(temperature=0.2, max_output_tokens=12000)
            notes, _ = call_gemini(client, [uploaded, prompt], config)
            return notes
        finally:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
#  3. Scrittura del digest
# --------------------------------------------------------------------------- #
def build_dossier(items: list) -> str:
    parts = []
    for n, it in enumerate(items, 1):
        p = [f"### [{n}] {it['title']}",
             f"Link scheda: {it['url']}",
             f"Ora: {it.get('time') or 'n.d.'} | Luogo: {it.get('place') or 'n.d.'}"]
        if it.get("agenda_text"):
            p.append(f"Dall'agenda: {it['agenda_text']}")
        if it.get("text"):
            p.append(f"Testo della scheda:\n{it['text']}")
        if it.get("interventions"):
            p.append("Interventi (ora, durata, oratore):\n" + "\n".join(it["interventions"]))
        if it.get("transcript"):
            p.append(f"Trascrizione automatica (estratto):\n{it['transcript']}")
        if it.get("audio_notes"):
            p.append(f"APPUNTI DALL'ASCOLTO DELL'AUDIO:\n{it['audio_notes']}")
        parts.append("\n".join(p))
    return "\n\n".join(parts)


def build_prompt(date_it: str, date_iso: str, dossier: str) -> str:
    return f"""
Scrivi il digest di Radio Radicale del {date_it}.
Agenda: {BASE}/agenda?data={date_iso}

Qui sotto c'è il DOSSIER con tutto il materiale raccolto: per ogni registrazione il link alla scheda,
il testo della scheda, l'elenco degli interventi con gli oratori e, per le registrazioni principali,
gli appunti presi ascoltando l'audio. Il dossier è la tua fonte primaria: usalo fino in fondo.
Puoi integrare il contesto (antefatti, iter di una legge, notizie collegate) con la ricerca web.

=== DOSSIER ===
{dossier}
=== FINE DOSSIER ===

REGOLE DI CONTENUTO
- Organizza il digest in AREE (titoli ##) e, dentro ogni area, in TEMI SPECIFICI (titoli ###).
  Un tema = UN argomento preciso: una legge o un provvedimento, un processo, una vicenda, un convegno
  su un argomento. Esempi di temi giusti: "Legge elettorale: fiducia sugli articoli 1-3",
  "Ddl 1990 su sicurezza e disagio giovanile", "Processo Mezzarano", "Parco nazionale dell'Etna".
  NON mettere argomenti diversi nello stesso tema solo perché si sono svolti nella stessa sede.
- Aree possibili, in quest'ordine, usando solo quelle che hanno temi:
  ## Parlamento e governo
  ## Giustizia e diritti
  ## Politica e partiti
  ## Esteri, Europa e difesa
  ## Economia, lavoro e ambiente
  ## Società, salute e cultura
  ## Scienza, tecnologia e informazione
- Tratta tutti i temi con materiale sufficiente: di norma 8-20 temi in tutto.
- Sii CONCRETO: per ogni intervento noto scrivi chi ha parlato (nome, ruolo, gruppo) e cosa ha sostenuto,
  con argomenti, dati e proposte. Se dagli appunti audio emergono contenuti, riportali in dettaglio.
- Se di una registrazione conosci solo titolo e oratori, mettila in "Le altre registrazioni" invece di
  creare un tema vuoto.
- Cita numeri degli atti (es. C. 2822-B), articoli, voti, cifre, date.
- Vietate le frasi vuote: "momento significativo", "tema centrale", "ampio spazio", "offre approfondimenti",
  "punto di vista critico e informato", "scenari complessi e in evoluzione".

REGOLE SUI LINK (obbligatorie)
- Accanto a OGNI registrazione citata metti il link alla sua scheda, copiato ESATTAMENTE dal dossier,
  in formato Markdown: [ascolta](https://www.radioradicale.it/scheda/...).
- Per i fatti di contesto presi dalla ricerca web aggiungi il link alla pagina da cui li hai presi.
- Non inventare mai un URL.

STRUTTURA (Markdown)

# Radio Radicale, {date_it}
*Analisi tematica delle registrazioni della giornata.*

## In breve
(8-12 punti, ognuno con un fatto preciso e il link alla scheda)

## [Nome dell'area]

### [Titolo breve e specifico del tema]
**In una riga**: la notizia principale del tema in una sola frase.

**Le registrazioni**: elenco con ora, titolo e link.

**Contesto**: antefatti concreti (iter, numeri degli atti, scadenze). Breve.

**Cosa è successo**: racconto dettagliato.

**Le posizioni**:
- **Nome Cognome (ruolo, gruppo)**: cosa ha sostenuto, con argomenti e dati.
(un punto per ogni intervento)

**Numeri e dati**: elenco puntato (solo se ci sono numeri).

**Cosa succede adesso**: prossimi passaggi e scadenze.

**Per verificare**: elenco dei link (schede e fonti di contesto).

(ripeti ### per ogni tema dell'area, poi passa all'area successiva)

## Le altre registrazioni
(una riga per registrazione: ora, titolo con link, oratori, contenuto se noto)

## Glossario

## Cosa non è stato possibile ricostruire

Restituisci SOLO il digest in italiano: la prima riga deve essere "# Radio Radicale, {date_it}".
Non scrivere ragionamenti, piani, note di lavoro o commenti prima o dopo il digest, e niente blocchi di codice.
Usa solo le registrazioni dell'agenda di questo giorno.
"""


def build_fallback_prompt(date_it: str, date_iso: str) -> str:
    """Usato solo se l'agenda non è leggibile direttamente: Gemini la apre da solo."""
    return f"""
Apri {BASE}/agenda?data={date_iso} e le schede delle registrazioni collegate, e scrivi il digest di
Radio Radicale del {date_it} con questa struttura: In breve; poi aree (##) e dentro ogni area temi
specifici (###), ciascuno con In una riga / Le registrazioni / Contesto / Cosa è successo / Le posizioni /
Cosa succede adesso / Per verificare; poi Le altre registrazioni, Glossario, Cosa non è stato possibile ricostruire.
Metti accanto a ogni registrazione il link alla sua scheda e non inventare URL.
Restituisci solo il Markdown.
"""


# --------------------------------------------------------------------------- #
#  Fonti e controllo dei link
# --------------------------------------------------------------------------- #
def resolve_url(url: str) -> str:
    if "grounding-api-redirect" not in url:
        return url
    try:
        r = HTTP.head(url, allow_redirects=True, timeout=10)
        return r.url
    except Exception:
        return url


def grounding_sources(response) -> list:
    out = []
    try:
        cand = response.candidates[0]
    except (AttributeError, IndexError, TypeError):
        return out
    gm = getattr(cand, "grounding_metadata", None)
    for chunk in (getattr(gm, "grounding_chunks", None) or []):
        web = getattr(chunk, "web", None)
        if web and getattr(web, "uri", None):
            out.append({"url": resolve_url(web.uri), "title": getattr(web, "title", "") or ""})
    ucm = getattr(cand, "url_context_metadata", None)
    for meta in (getattr(ucm, "url_metadata", None) or []):
        url = getattr(meta, "retrieved_url", None)
        if url and "SUCCESS" in str(getattr(meta, "url_retrieval_status", "")).upper():
            out.append({"url": url, "title": ""})
    return out


LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def link_is_dead(url: str) -> bool:
    try:
        r = HTTP.get(url, timeout=10, stream=True)
        r.close()
        return r.status_code in (404, 410)
    except Exception:
        return False


def clean_dead_links(markdown: str, known_good: set, max_checks: int = 120) -> str:
    checked = {}
    for _, url in LINK_RE.findall(markdown):
        if url in known_good or url in checked or len(checked) >= max_checks:
            continue
        checked[url] = link_is_dead(url)
    dead = {u for u, d in checked.items() if d}
    if dead:
        print(f"🧹 Rimossi {len(dead)} link non funzionanti.")
    return LINK_RE.sub(lambda m: m.group(1) if m.group(2) in dead else m.group(0), markdown)


# --------------------------------------------------------------------------- #
#  Generazione di un giorno
# --------------------------------------------------------------------------- #
def generate_for_date(client: genai.Client, ref_date: datetime) -> None:
    date_iso = ref_date.strftime("%Y-%m-%d")
    date_it = ref_date.strftime("%d.%m.%Y")
    agenda_url = f"{BASE}/agenda?data={date_iso}"
    print(f"\n📰 Digest del {date_it}")

    print("📋 Lettura dell'agenda...")
    items = scrape_agenda(date_iso)
    print(f"   {len(items)} registrazioni trovate.")

    for it in items[:MAX_SCHEDE]:
        it.update(scrape_scheda(it["url"]))
        time.sleep(0.5)  # gentilezza verso il sito

    if AUDIO_MAX_RECORDINGS > 0 and items:
        candidates = [i for i in items if i.get("stream") and audio_score(i) >= 0]
        candidates.sort(key=audio_score, reverse=True)
        audio_start = time.time()
        for it in candidates[:AUDIO_MAX_RECORDINGS]:
            if time.time() - audio_start > AUDIO_TOTAL_BUDGET:
                print("   ⏱️ Tempo per l'ascolto esaurito: passo alla scrittura del digest.")
                break
            try:
                it["audio_notes"] = listen_recording(client, it)
            except Exception as e:
                print(f"   ⚠️ Ascolto non riuscito: {e}")

    tools_config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[{"google_search": {}}, {"url_context": {}}],
        temperature=0.3,
        max_output_tokens=32768,
    )

    print("✍️ Scrittura del digest...")
    if items:
        prompt = build_prompt(date_it, date_iso, build_dossier(items))
    else:
        print("   ⚠️ Agenda non leggibile: Gemini la aprirà da solo.")
        prompt = build_fallback_prompt(date_it, date_iso)
    digest_md, response = call_gemini(client, prompt, tools_config)

    # Fonti: agenda, tutte le schede lette, poi le pagine consultate da Gemini
    sources = [{"url": agenda_url, "title": f"Agenda di Radio Radicale del {date_it}"}]
    sources += [{"url": i["url"], "title": f"{i.get('time', '')} {i['title']}".strip()} for i in items]
    sources += grounding_sources(response)
    seen, unique = set(), []
    for s in sources:
        if s["url"] not in seen:
            seen.add(s["url"])
            unique.append(s)

    known_good = {i["url"] for i in items} | {agenda_url}
    digest_md = clean_markdown(digest_md, date_it)
    digest_md = clean_dead_links(digest_md, known_good)

    with open(os.path.join(DIGESTS_DIR, f"{date_iso}.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"version": SCRIPT_VERSION,
             "date": date_iso, "label": date_it, "markdown": digest_md, "sources": unique,
             "recordings": len(items), "listened": sum(1 for i in items if i.get("audio_notes"))},
            f, ensure_ascii=False, indent=2,
        )
    print(f"   💾 Salvato con {len(unique)} fonti.")

    index_data = [i for i in load_index() if i.get("date") != date_iso]
    index_data.append({"date": date_iso, "label": date_it})
    save_index(index_data)


# --------------------------------------------------------------------------- #
#  Indice e scelta dei giorni
# --------------------------------------------------------------------------- #
def load_index() -> list:
    if not os.path.exists(INDEX_PATH):
        return []
    try:
        with open(INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def save_index(index_data: list) -> None:
    known = {item["date"]: item for item in index_data if "date" in item}
    for name in os.listdir(DIGESTS_DIR):
        if re.match(r"\d{4}-\d{2}-\d{2}\.json$", name):
            d = name[:-5]
            if d not in known:
                try:
                    dt = datetime.strptime(d, "%Y-%m-%d")
                    known[d] = {"date": d, "label": dt.strftime("%d.%m.%Y")}
                except ValueError:
                    pass
    ordered = sorted(known.values(), key=lambda x: x["date"], reverse=True)
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        json.dump(ordered, f, ensure_ascii=False, indent=2)


SCRIPT_VERSION = 4


def needs_update(path: str) -> bool:
    """True se il digest manca oppure è stato fatto con una versione vecchia (senza fonti)."""
    if not os.path.exists(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return True
    return not data.get("sources") or data.get("version", 0) < SCRIPT_VERSION


def dates_to_generate() -> list:
    forced = os.environ.get("DIGEST_DATE", "").strip()
    if forced:
        return [datetime.strptime(forced, "%Y-%m-%d")]
    today = datetime.now()
    d_from = os.environ.get("DIGEST_FROM", "").strip()
    d_to = os.environ.get("DIGEST_TO", "").strip()
    if d_from:
        # recupero di un periodo: dal più recente al più vecchio
        start = datetime.strptime(d_from, "%Y-%m-%d")
        stop = datetime.strptime(d_to, "%Y-%m-%d") if d_to else today - timedelta(days=2)
        n_days = (stop - start).days + 1
        candidates = [stop - timedelta(days=n) for n in range(max(n_days, 0))]
    else:
        # dal giorno di 2 giorni fa all'indietro: prima i più recenti
        candidates = [today - timedelta(days=n) for n in range(2, 2 + BACKFILL_DAYS)]
    todo = []
    for d in candidates:
        path = os.path.join(DIGESTS_DIR, d.strftime("%Y-%m-%d") + ".json")
        if needs_update(path):
            reason = "mancante" if not os.path.exists(path) else "versione precedente"
            print(f"   • {d.strftime('%d.%m.%Y')}: {reason}")
            todo.append(d)
    if len(todo) > MAX_PER_RUN:
        print(f"   (ne faccio {MAX_PER_RUN} ora; gli altri {len(todo) - MAX_PER_RUN} alle prossime esecuzioni)")
    return todo[:MAX_PER_RUN]


def slugify(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:60]


SPECIAL_SECTIONS = ("in breve", "le altre registrazioni", "glossario", "cosa non")


def extract_themes(markdown: str) -> list:
    """Elenco dei temi (### dentro le aree ##) con la frase "In una riga"."""
    themes, area = [], ""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## "):
            area = re.sub(r"^\d+[.)]\s*", "", line[3:].strip())
        elif line.startswith("### ") and area and not area.lower().startswith(SPECIAL_SECTIONS):
            title = re.sub(r"^\d+[.)]\s*", "", line[4:].strip())
            summary = ""
            for nxt in lines[i + 1:i + 8]:
                m = re.match(r"\*\*In una riga\*\*\s*:?\s*(.+)", nxt.strip())
                if m:
                    summary = re.sub(r"\s*\[(ascolta|fonte|qui|link)\]\([^)]+\)", "", m.group(1), flags=re.I)
                    summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", summary).strip()
                    break
                if nxt.startswith("#"):
                    break
            themes.append({"area": area, "title": title, "summary": summary[:300], "id": slugify(title)})
    if not themes:
        # digest del formato precedente: i temi erano "## 1. Titolo"
        for line in lines:
            m = re.match(r"^##\s+\d+[.)]\s*(.+)", line)
            if m:
                themes.append({"area": "Temi del giorno", "title": m.group(1).strip(), "summary": "",
                               "id": slugify(m.group(1))})
    return themes


def save_themes_index() -> None:
    """digests/themes.json: tutti i temi di tutti i giorni, per l'archivio e la ricerca nella pagina."""
    out = []
    for name in sorted(os.listdir(DIGESTS_DIR), reverse=True):
        if not re.match(r"\d{4}-\d{2}-\d{2}\.json$", name):
            continue
        try:
            with open(os.path.join(DIGESTS_DIR, name), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for t in extract_themes(d.get("markdown", "")):
            out.append({"date": d.get("date", name[:10]), **t})
    with open(os.path.join(DIGESTS_DIR, "themes.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(f"🗂️ Archivio dei temi: {len(out)} temi.")


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata nelle variabili d'ambiente!")
    client = genai.Client(api_key=api_key)
    os.makedirs(DIGESTS_DIR, exist_ok=True)

    print(f"🚀 build_digest versione {SCRIPT_VERSION} (con fonti, schede e ascolto audio)")
    print("🗓️  Giorni da generare o aggiornare:")
    dates = dates_to_generate()
    if not dates:
        print("Nessun digest da generare: sono già tutti presenti e aggiornati.")
        save_index(load_index())
        save_themes_index()
        return

    failures = 0
    for d in dates:
        try:
            generate_for_date(client, d)
        except Exception as e:
            failures += 1
            print(f"❌ Digest del {d.strftime('%d.%m.%Y')} non generato: {e}")

    save_themes_index()
    print("\n✨ Fatto.")
    if failures == len(dates):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
