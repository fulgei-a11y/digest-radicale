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
# Di quanti giorni "in ritardo" lavorare: 1 = il digest di ieri, fatto la mattina dopo.
DAYS_LAG = max(1, int(os.environ.get("DAYS_LAG", "1")))
# Il giorno dopo si ricontrolla l'agenda: se nel frattempo sono comparse molte registrazioni
# nuove (schede pubblicate in ritardo), il digest viene rifatto una volta sola.
RECHECK_MIN_NEW = int(os.environ.get("RECHECK_MIN_NEW", "3"))
# Testo massimo dello stenografico di una seduta passato a Gemini (caratteri)
STENO_MAX_CHARS = int(os.environ.get("STENO_MAX_CHARS", "180000"))
# Indirizzo pubblico del sito (serve per le anteprime su WhatsApp e social)
SITE_URL = os.environ.get("SITE_URL", "https://fulgei-a11y.github.io/digest-radicale/").rstrip("/") + "/"
SHARE_DIR = "d"
# File con l'esito dell'esecuzione, letto dal controllo che apre l'avviso in caso di problemi
STATUS_PATH = os.environ.get("DIGEST_STATUS_FILE", "")
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

# Modelli preferiti, in ordine. Lo script controlla all'avvio quali sono davvero disponibili
# per la tua chiave e aggiunge in coda gli altri modelli "flash" più recenti: così, se Google
# ne ritira uno, non si blocca più tutto.
MODELS_TO_TRY = [
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-2.5-pro",
]
_MODELS_CHECKED = False


def available_models(client) -> list:
    """Restituisce l'elenco dei modelli da provare, solo fra quelli disponibili per questa chiave."""
    global MODELS_TO_TRY, _MODELS_CHECKED
    if _MODELS_CHECKED:
        return MODELS_TO_TRY
    _MODELS_CHECKED = True
    try:
        names = []
        for m in client.models.list():
            name = (getattr(m, "name", "") or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
            if name.startswith("gemini") and (not actions or "generateContent" in actions):
                names.append(name)
        if not names:
            return MODELS_TO_TRY
        def ver(n):
            m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
            return float(m.group(1)) if m else 0
        extra = sorted((n for n in names if "flash" in n and not re.search(r"preview|exp|tts|image|audio|live|embed", n)
                        and n not in MODELS_TO_TRY), key=ver, reverse=True)
        chosen = [m for m in MODELS_TO_TRY if m in names] + extra[:3]
        if chosen:
            MODELS_TO_TRY = chosen
        print("🤖 Modelli disponibili che userò:", ", ".join(MODELS_TO_TRY))
    except Exception as e:
        print(f"⚠️ Non riesco a leggere l'elenco dei modelli ({e}): uso quelli predefiniti.")
    return MODELS_TO_TRY

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
    """Prova i modelli disponibili in ordine. Con il limite al minuto (429) aspetta e riprova;
    se un modello è ritirato (404) passa subito al successivo."""
    last_error = None
    for model in available_models(client):
        for attempt in range(3):
            try:
                print(f"   🔄 Modello '{model}'...")
                response = client.models.generate_content(model=model, contents=contents, config=config)
                text = (response.text or "").strip()
                if not text:
                    print(f"   ⚠️ Risposta vuota da '{model}'.")
                    break
                print(f"   ✅ Risposta da '{model}'")
                return text, response
            except errors.APIError as e:
                msg = str(e)
                last_error = e
                if e.code == 429 or "RESOURCE_EXHAUSTED" in msg:
                    if "per day" in msg.lower() or "PerDay" in msg:
                        print(f"   ⚠️ Quota giornaliera esaurita per '{model}'.")
                        break
                    print(f"   ⏳ Limite al minuto per '{model}': attendo 65 secondi e riprovo...")
                    time.sleep(65)
                    continue
                if e.code == 503 or "UNAVAILABLE" in msg:
                    print(f"   ⚠️ Server sovraccarico (503) per '{model}': riprovo tra 20 secondi...")
                    time.sleep(20)
                    continue
                if e.code in (404, 400) or "NOT_FOUND" in msg:
                    print(f"   ⚠️ Modello '{model}' non disponibile: passo al successivo.")
                    break
                raise
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
    md = cut_repetition(md)
    return fix_bare_urls(md)


def cut_repetition(md: str) -> str:
    """A volte il modello, finito il digest, ricomincia da capo: si taglia alla prima sezione ## ripetuta."""
    seen = set()
    for m in re.finditer(r"^##\s+(.+?)\s*$", md, re.M):
        key = m.group(1).strip().lower()
        if key in seen:
            print(f"   ✂️ Il digest si ripeteva da \"## {m.group(1).strip()}\": tolta la parte ripetuta.")
            return md[:m.start()].rstrip() + "\n"
        seen.add(key)
    return md


MD_LINK_OR_URL = re.compile(r"(\[[^\]]*\]\([^)\s]+\))|\[(https?://[^\]\s]+)\]|<?(https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:])>?")


def fix_bare_urls(md: str) -> str:
    """Gli indirizzi scritti per intero (anche tra parentesi quadre) diventano link brevi:
    sul telefono una stringa lunga senza spazi allarga la pagina."""
    def sub(m):
        if m.group(1):
            return m.group(1)            # link Markdown già corretto
        url = m.group(2) or m.group(3)
        host = urlparse(url).netloc.replace("www.", "")
        return f"[{host or 'fonte'}]({url})"
    return MD_LINK_OR_URL.sub(sub, md)


def scrape_scheda(url: str) -> dict:
    """Contenuto di una scheda: descrizione, interventi (oratore e orario), trascrizione, flusso audio."""
    html = get_html(url)
    if not html:
        return {}
    m3u8 = re.findall(r"https?://[^\s\"'<>]+?\.m3u8", html)
    mp3 = re.findall(r"https?://[^\s\"'<>]+?\.mp3", html)
    # seduta dell'Aula della Camera: la scheda rimanda a camera.it/leg19/410?idSeduta=0723
    cam = re.search(r"camera\.it/leg(\d+)/410\?idSeduta=(\d+)", html)

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
        "camera_seduta": (cam.group(1), cam.group(2)) if cam else None,
    }


# --------------------------------------------------------------------------- #
#  1b. Resoconti stenografici (testo integrale degli interventi in Aula)
# --------------------------------------------------------------------------- #
def fetch_text_page(url: str, timeout: int = 60) -> str:
    try:
        r = HTTP.get(url, timeout=timeout)
    except requests.RequestException as e:
        print(f"   ⚠️ {url} → {e}")
        return ""
    if r.status_code != 200:
        print(f"   ⚠️ {url} → HTTP {r.status_code}")
        return ""
    if url.lower().endswith(".pdf") or r.headers.get("content-type", "").startswith("application/pdf"):
        try:
            import io
            from pypdf import PdfReader
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(r.content)).pages)
        except Exception as e:
            print(f"   ⚠️ PDF non leggibile ({e})")
            return ""
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
        tag.decompose()
    return clean_text((soup.body or soup).get_text("\n"))


def camera_stenografico(leg: str, seduta: str) -> str:
    """Resoconto stenografico di una seduta dell'Aula della Camera (prima HTML, poi PDF)."""
    n = f"{int(seduta):04d}"
    base = f"https://documenti.camera.it/leg{leg}/resoconti/assemblea/html/sed{n}/"
    for url in (base + "stenografico.htm", base + "stenografico.pdf"):
        text = fetch_text_page(url)
        if len(text) > 5000:   # sotto questa soglia di solito è solo l'intestazione: testo non ancora pubblicato
            print(f"   📜 Stenografico della Camera, seduta {int(seduta)}: {len(text) // 1000}k caratteri")
            if len(text) > STENO_MAX_CHARS:
                # si tengono l'inizio (ordine dei lavori) e soprattutto la fine, dove ci sono
                # le dichiarazioni di voto e le votazioni finali
                head = STENO_MAX_CHARS // 5
                text = text[:head] + "\n[...parte centrale omessa...]\n" + text[-(STENO_MAX_CHARS - head):]
            return text
    print(f"   ⏳ Stenografico della Camera, seduta {int(seduta)}: non ancora disponibile")
    return ""


def is_senato_aula(item: dict) -> bool:
    t = (item.get("title", "") + " " + item.get("place", "") + " " + item.get("text", "")[:600]).lower()
    return bool(re.search(r"seduta\s+\d+", t)) and "senato" in t and "commission" not in item.get("title", "").lower()


# --------------------------------------------------------------------------- #
#  2. Ascolto dell'audio delle registrazioni principali
# --------------------------------------------------------------------------- #
def audio_score(item: dict) -> int:
    t = (item["title"] + " " + item.get("agenda_text", "")).lower()
    if any(k in t for k in AUDIO_SKIP):
        return -1
    score = sum(5 for k in AUDIO_PRIORITY if k in t)
    score += min(len(item.get("interventions", [])), 30)  # più oratori = più contenuto
    if len(item.get("transcript", "")) > 3000 or item.get("steno"):
        return -1  # c'è già il testo integrale: l'audio non serve
    if is_senato_aula(item):
        score -= 15  # per l'Aula del Senato Gemini cerca il resoconto ufficiale
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
        if it.get("steno"):
            p.append(f"RESOCONTO STENOGRAFICO UFFICIALE (testo integrale degli interventi, fonte: {it['steno_url']}):\n{it['steno']}")
        elif it.get("steno_hint"):
            p.append(it["steno_hint"])
        if it.get("audio_notes"):
            p.append(f"APPUNTI DALL'ASCOLTO DELL'AUDIO:\n{it['audio_notes']}")
        parts.append("\n".join(p))
    return "\n\n".join(parts)


def known_dossiers_text(before_iso: str, days: int = 30, limit: int = 120) -> str:
    """Elenco dei dossier aperti nelle ultime settimane, da passare a Gemini perché riusi le chiavi."""
    path = os.path.join(DIGESTS_DIR, "dossiers.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "(nessun dossier ancora)"
    limit_date = (datetime.strptime(before_iso, "%Y-%m-%d") - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = [d for d in data if limit_date <= d.get("last", "") < before_iso]
    rows.sort(key=lambda d: (d.get("days", 0), d.get("last", "")), reverse=True)
    if not rows:
        return "(nessun dossier ancora)"
    return "\n".join(f"- {d['key']} | {d['label']} | ultimo giorno {d['last']}" for d in rows[:limit])


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
- ORARI E DURATE NON SONO CONTENUTO. Non scrivere mai a che ora ha iniziato a parlare qualcuno o quanto
  è durato il suo intervento, e non scrivere frasi vuote come "ha espresso la posizione del suo gruppo"
  o "è intervenuto nel dibattito". Nelle "Le posizioni" metti SOLO chi ha detto qualcosa di cui conosci
  il contenuto (dal resoconto stenografico, dagli appunti audio, dal testo della scheda o da fonti web
  verificabili). Gli altri vanno in una riga finale: "**Sono intervenuti anche**: Nome (gruppo), Nome (gruppo)...".
  Se non conosci il contenuto di nessun intervento, ometti del tutto "Le posizioni".
- Quando c'è il RESOCONTO STENOGRAFICO usalo come fonte principale per le posizioni: riporta per ciascun
  oratore la tesi, gli argomenti, i dati citati e, se utile, una breve frase testuale tra virgolette.
  Aggiungi il link allo stenografico in "Per verificare".
- Cita numeri degli atti (es. C. 2822-B), articoli, voti, cifre, date.
- Vietate le frasi vuote: "momento significativo", "tema centrale", "ampio spazio", "offre approfondimenti",
  "punto di vista critico e informato", "scenari complessi e in evoluzione".

REGOLE SUI LINK (obbligatorie)
- Accanto a OGNI registrazione citata metti il link alla sua scheda, copiato ESATTAMENTE dal dossier,
  in formato Markdown: [ascolta](https://www.radioradicale.it/scheda/...).
- Per i fatti di contesto presi dalla ricerca web aggiungi il link alla pagina da cui li hai presi.
- Non inventare mai un URL.
- Scrivi SEMPRE i link nella forma [testo breve](indirizzo): mai l'indirizzo da solo o tra parentesi quadre.

DOSSIER (per seguire un tema giorno per giorno)
- Subito sotto il titolo ### di ogni tema scrivi una riga così, da sola:
  <!-- dossier: chiave | Nome del dossier -->
- Se il tema è la prosecuzione di una vicenda già seguita nei giorni scorsi (stessa legge o atto, anche
  se cambia ramo del Parlamento o numero; stesso processo; stessa indagine conoscitiva; stessa crisi
  aziendale o vertenza), RIUSA ESATTAMENTE la chiave e il nome dell'elenco qui sotto.
- Altrimenti crea una chiave nuova: minuscole e trattini, 2-5 parole, che descriva la vicenda e non
  il singolo passaggio (giusto: "legge-elettorale", "processo-santa-maria-capua-vetere";
  sbagliato: "legge-elettorale-fiducia-art-3"). Il nome è breve e leggibile: "Legge elettorale".
Dossier dei giorni scorsi (chiave | nome | ultimo giorno):
{known_dossiers_text(date_iso)}

STRUTTURA (Markdown)

# Radio Radicale, {date_it}
*Analisi tematica delle registrazioni della giornata.*

## In breve
(8-12 punti, ognuno con un fatto preciso e il link alla scheda)

## [Nome dell'area]

### [Titolo breve e specifico del tema]
<!-- dossier: chiave-della-vicenda | Nome del dossier -->
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
Scrivi il digest una volta sola: dopo "## Cosa non è stato possibile ricostruire" fermati.
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

    steno_missing = []
    for it in items:
        if it.get("camera_seduta"):
            leg, sed = it["camera_seduta"]
            it["steno"] = camera_stenografico(leg, sed)
            it["steno_url"] = f"https://documenti.camera.it/leg{leg}/resoconti/assemblea/html/sed{int(sed):04d}/stenografico.htm"
            if not it["steno"]:
                steno_missing.append([leg, sed])
        elif is_senato_aula(it):
            m = re.search(r"seduta\s+(\d+)", it["title"], re.I)
            it["steno_hint"] = (
                "Per questa seduta dell'Aula del Senato cerca con la ricerca web il resoconto stenografico ufficiale "
                f"su senato.it (seduta n. {m.group(1) if m else '?'} del {date_it}) e usalo per le posizioni degli oratori. "
                "Se non lo trovi, non inventare: elenca solo i nomi in \"Sono intervenuti anche\".")

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

    out_path = os.path.join(DIGESTS_DIR, f"{date_iso}.json")
    rechecked, first_generated = False, ""
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                old = json.load(f)
            rechecked = bool(old.get("rechecked"))
            first_generated = old.get("first_generated") or old.get("generated", "")
        except Exception:
            pass
    now_stamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {"version": SCRIPT_VERSION,
             "date": date_iso, "label": date_it, "markdown": digest_md, "sources": unique,
             "recordings": len(items), "listened": sum(1 for i in items if i.get("audio_notes")),
             "generated": now_stamp, "first_generated": first_generated or now_stamp,
             "rechecked": rechecked, "steno_missing": steno_missing},
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


SCRIPT_VERSION = 5


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
        stop = datetime.strptime(d_to, "%Y-%m-%d") if d_to else today - timedelta(days=DAYS_LAG)
        n_days = (stop - start).days + 1
        candidates = [stop - timedelta(days=n) for n in range(max(n_days, 0))]
    else:
        # da ieri (DAYS_LAG) all'indietro: prima i più recenti
        candidates = [today - timedelta(days=n) for n in range(DAYS_LAG, DAYS_LAG + BACKFILL_DAYS)]
    todo = []
    for d in candidates:
        path = os.path.join(DIGESTS_DIR, d.strftime("%Y-%m-%d") + ".json")
        if needs_update(path):
            reason = "mancante" if not os.path.exists(path) else "versione precedente"
            print(f"   • {d.strftime('%d.%m.%Y')}: {reason}")
            todo.append(d)
    if not d_from:
        # Ricontrollo del giorno prima: il digest è stato fatto la mattina dopo, quando alcune schede
        # (soprattutto delle registrazioni serali) potevano non essere ancora pubblicate.
        d = today - timedelta(days=DAYS_LAG + 1)
        if d not in todo and recheck_needed(d):
            todo.insert(1 if todo else 0, d)
    if len(todo) > MAX_PER_RUN:
        print(f"   (ne faccio {MAX_PER_RUN} ora; gli altri {len(todo) - MAX_PER_RUN} alle prossime esecuzioni)")
    return todo[:MAX_PER_RUN]


def recheck_needed(d: datetime) -> bool:
    """True se l'agenda di quel giorno ora contiene parecchie registrazioni in più rispetto al digest.
    Il ricontrollo si fa una volta sola per giorno (campo "rechecked" nel file)."""
    path = os.path.join(DIGESTS_DIR, d.strftime("%Y-%m-%d") + ".json")
    if not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    if data.get("rechecked"):
        return False
    before = int(data.get("recordings") or 0)
    now = len(scrape_agenda(d.strftime("%Y-%m-%d")))
    if not now:
        return False  # agenda non leggibile adesso: si riprova alla prossima esecuzione
    redo = now >= before + RECHECK_MIN_NEW
    if not redo and data.get("steno_missing"):
        leg, sed = data["steno_missing"][0]
        if camera_stenografico(leg, sed):
            print(f"   • {d.strftime('%d.%m.%Y')}: ora c'è lo stenografico della Camera che mancava")
            redo = True
    data["rechecked"] = True
    if redo:
        data["rechecked_from"] = before
        print(f"   • {d.strftime('%d.%m.%Y')}: da rifare, l'agenda è passata da {before} a {now} registrazioni")
    else:
        print(f"   • {d.strftime('%d.%m.%Y')}: ricontrollato, nessuna novità rilevante ({before} → {now})")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return redo


def slugify(text: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:60]


SPECIAL_SECTIONS = ("in breve", "le altre registrazioni", "glossario", "cosa non")
DOSSIER_TAG_RE = re.compile(r"<!--\s*dossier\s*:\s*([^|>]+?)\s*(?:\|\s*([^>]*?))?\s*-->", re.I)


def extract_themes(markdown: str) -> list:
    """Elenco dei temi (### dentro le aree ##) con la frase "In una riga" e l'eventuale etichetta dossier."""
    themes, area = [], ""
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("## "):
            area = re.sub(r"^\d+[.)]\s*", "", line[3:].strip())
        elif line.startswith("### ") and area and not area.lower().startswith(SPECIAL_SECTIONS):
            raw = line[4:]
            tag = DOSSIER_TAG_RE.search(raw)
            title = re.sub(r"^\d+[.)]\s*", "", DOSSIER_TAG_RE.sub("", raw).strip())
            summary = ""
            for nxt in lines[i + 1:i + 8]:
                if nxt.startswith("#"):
                    break
                if not tag:
                    tag = DOSSIER_TAG_RE.search(nxt)
                m = re.match(r"\*\*In una riga\*\*\s*:?\s*(.+)", nxt.strip())
                if m and not summary:
                    summary = re.sub(r"\s*\[(ascolta|fonte|qui|link)\]\([^)]+\)", "", m.group(1), flags=re.I)
                    summary = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", summary).strip()
            t = {"area": area, "title": title, "summary": summary[:300], "id": slugify(title)}
            if tag:
                t["tag"] = slugify(tag.group(1))
                if tag.group(2):
                    t["tag_label"] = tag.group(2).strip()[:80]
            themes.append(t)
    if not themes:
        # digest del formato precedente: i temi erano "## 1. Titolo"
        for line in lines:
            m = re.match(r"^##\s+\d+[.)]\s*(.+)", line)
            if m:
                themes.append({"area": "Temi del giorno", "title": m.group(1).strip(), "summary": "",
                               "id": slugify(m.group(1))})
    return themes


# --------------------------------------------------------------------------- #
#  Dossier: lo stesso tema seguito giorno per giorno
# --------------------------------------------------------------------------- #
# Numeri degli atti: identificano con certezza la stessa vicenda anche se il titolo cambia.
ACT_PATTERNS = [
    (re.compile(r"\b(?:decreto[\s-]*legge|d\.?\s?l\.?)\s*(?:n\.?\s*)?(\d{1,3})\s*/\s*(\d{4})", re.I), "dl-{0}-{1}"),
    (re.compile(r"\bd\.?\s?lgs\.?\s*(?:n\.?\s*)?(\d{1,3})\s*/\s*(\d{4})", re.I), "dlgs-{0}-{1}"),
    (re.compile(r"\b(?:A\.?\s?G\.?|atto\s+(?:del\s+)?governo)\s*(?:n\.?\s*)?(\d{2,4})\b", re.I), "atto-governo-{0}"),
    (re.compile(r"\b(?:A\.?\s?C\.?|C\.)\s*(?:n\.?\s*)?(\d{2,5})(?:-[A-Z])?\b"), "camera-{0}"),
    (re.compile(r"\b(?:d\.?\s?d\.?\s?l\.?|A\.?\s?S\.?|S\.)\s*(?:n\.?\s*)?(\d{2,5})(?:-[A-Z])?\b", re.I), "ddl-{0}"),
]
STOPWORDS = set("""
a ad al alla alle allo agli ai all anche che chi con cui da dal dalla dalle dai degli dei del della delle dello
di e ed fra gli i il in la le lo nel nella nelle nei negli o per su sul sulla sulle sui tra un una uno
seguito discussione esame audizione audizioni sul sulla presentazione libro convegno incontro dibattito
seduta proposta disegno legge ddl decreto materia tema temi nuovo nuova parte prima seconda giornata
""".split())


def act_key(text: str) -> str:
    for rx, fmt in ACT_PATTERNS:
        m = rx.search(text)
        if m:
            return fmt.format(*m.groups())
    return ""


def words_of(text: str) -> set:
    import unicodedata
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return {w[:7] for w in re.findall(r"[a-z]{3,}", t) if w not in STOPWORDS}


def assign_dossiers(themes: list) -> None:
    """Dà a ogni tema la chiave del suo dossier. Ordine di preferenza:
    1) l'etichetta scritta da Gemini nel digest; 2) il numero dell'atto (legge, decreto, atto del governo);
    3) il nome del processo; 4) un titolo molto simile a un tema delle tre settimane precedenti."""
    by_key = {}          # chiave -> {"words": set, "last": data}
    act_to_key = {}      # numero dell'atto -> chiave dossier usata
    for t in sorted(themes, key=lambda x: x["date"]):
        text = t["title"] + " " + t.get("summary", "")
        act = act_key(t["title"]) or act_key(t.get("summary", ""))
        key = t.get("tag", "")
        if not key and act:
            key = act_to_key.get(act, act)
        if not key:
            m = re.match(r"processo\s+(?:d['’]appello\s+|di\s+appello\s+)?(?:a\s+)?([A-Za-zÀ-ÿ']+)", t["title"], re.I)
            if m and m.group(1).lower() not in ("per", "sulla", "sul", "contro"):
                key = "processo-" + slugify(m.group(1))
        if not key:
            w = words_of(t["title"])
            best, best_score = "", 0.0
            limit = (datetime.strptime(t["date"], "%Y-%m-%d") - timedelta(days=21)).strftime("%Y-%m-%d")
            for k, info in by_key.items():
                if info["last"] < limit or info["last"] >= t["date"] or not w or not info["words"]:
                    continue
                score = len(w & info["words"]) / len(w | info["words"])
                if score > best_score:
                    best, best_score = k, score
            key = best if best_score >= 0.5 else slugify(t["title"])
        if act and act not in act_to_key:
            act_to_key[act] = key
        t["dossier"] = key
        info = by_key.setdefault(key, {"words": set(), "last": ""})
        info["words"] = words_of(t["title"]) | (info["words"] if not t.get("tag") else set())
        info["last"] = t["date"]


def build_dossiers(themes: list) -> list:
    groups = {}
    for t in themes:
        groups.setdefault(t["dossier"], []).append(t)
    out = []
    for key, items in groups.items():
        items.sort(key=lambda x: x["date"])
        labels = [i["tag_label"] for i in items if i.get("tag_label")]
        label = labels[-1] if labels else items[-1]["title"]
        if not labels and ":" in label and len(label.split(":")[0]) >= 10:
            label = label.split(":")[0].strip()   # "Legge elettorale: fiducia sull'art. 3" -> "Legge elettorale"
        dates = sorted({i["date"] for i in items})
        out.append({"key": key, "label": label, "area": items[-1]["area"],
                    "first": dates[0], "last": dates[-1], "days": len(dates)})
    out.sort(key=lambda d: (d["last"], d["days"]), reverse=True)
    return out


def save_themes_index() -> None:
    """digests/themes.json (tutti i temi) e digests/dossiers.json (i temi raggruppati per vicenda)."""
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
    assign_dossiers(out)
    dossiers = build_dossiers(out)
    for t in out:
        t.pop("tag", None)
        t.pop("tag_label", None)
    out.sort(key=lambda t: t["date"], reverse=True)
    with open(os.path.join(DIGESTS_DIR, "themes.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    with open(os.path.join(DIGESTS_DIR, "dossiers.json"), "w", encoding="utf-8") as f:
        json.dump(dossiers, f, ensure_ascii=False, indent=0)
    multi = sum(1 for d in dossiers if d["days"] > 1)
    print(f"🗂️ Archivio dei temi: {len(out)} temi, {multi} dossier seguiti per più giorni.")


# --------------------------------------------------------------------------- #
#  Pagine di condivisione (anteprima su WhatsApp, Telegram, social)
# --------------------------------------------------------------------------- #
def _plain(md: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)
    s = re.sub(r"[*_`#>]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def share_description(markdown: str, limit: int = 280) -> str:
    """I primi punti di "In breve", per il testo dell'anteprima."""
    m = re.search(r"^##\s*In breve.*?$(.*?)(?=^##\s)", markdown, re.M | re.S)
    block = m.group(1) if m else markdown
    points = [_plain(l[2:]) for l in block.split("\n") if l.strip().startswith(("- ", "* "))]
    text = " · ".join(p for p in points if p)
    if not text:
        text = _plain(block)
    return text[:limit - 1].rsplit(" ", 1)[0] + "…" if len(text) > limit else text


def html_attr(s: str) -> str:
    return (s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;"))


SHARE_TEMPLATE = """<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta property="og:type" content="article">
<meta property="og:locale" content="it_IT">
<meta property="og:site_name" content="Digest Radio Radicale">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{image}">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta name="twitter:card" content="summary_large_image">
<script>location.replace({target_js});</script>
</head>
<body><p><a href="{target}">{title}</a></p></body>
</html>
"""


def write_share_page(key: str, title: str, desc: str, target_hash: str) -> None:
    os.makedirs(SHARE_DIR, exist_ok=True)
    target = SITE_URL + "#" + target_hash
    html = SHARE_TEMPLATE.format(
        title=html_attr(title), desc=html_attr(desc), url=html_attr(f"{SITE_URL}{SHARE_DIR}/{key}.html"),
        image=html_attr(SITE_URL + "og-image.png"), target=html_attr(target), target_js=json.dumps(target),
    )
    with open(os.path.join(SHARE_DIR, f"{key}.html"), "w", encoding="utf-8") as f:
        f.write(html)


def save_share_pages() -> None:
    """Una piccola pagina statica per ogni giorno e per ogni dossier: WhatsApp non esegue JavaScript,
    quindi l'anteprima (titolo e testo) deve stare già scritta nell'HTML. Chi apre il link viene
    portato subito al digest vero."""
    n = 0
    for item in load_index():
        path = os.path.join(DIGESTS_DIR, item["date"] + ".json")
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        dt = datetime.strptime(item["date"], "%Y-%m-%d")
        title = f"Radio Radicale, {GIORNI[dt.weekday()]} {dt.day} {MESI[dt.month - 1]} {dt.year}"
        write_share_page(item["date"], title, share_description(d.get("markdown", "")), item["date"])
        n += 1
    try:
        with open(os.path.join(DIGESTS_DIR, "dossiers.json"), encoding="utf-8") as f:
            dossiers = json.load(f)
    except Exception:
        dossiers = []
    for ds in dossiers:
        if ds.get("days", 0) < 2:
            continue
        first = datetime.strptime(ds["first"], "%Y-%m-%d")
        last = datetime.strptime(ds["last"], "%Y-%m-%d")
        desc = (f"Dossier di Radio Radicale: {ds['days']} giorni seguiti, "
                f"dal {first.day} {MESI[first.month - 1]} al {last.day} {MESI[last.month - 1]} {last.year}.")
        write_share_page("dossier-" + ds["key"], f"Dossier: {ds['label']}", desc, "dossier/" + ds["key"])
        n += 1
    print(f"🔗 Pagine di condivisione aggiornate: {n}.")


GIORNI = ["lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"]
MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
        "settembre", "ottobre", "novembre", "dicembre"]


def write_status(status: dict) -> None:
    if not STATUS_PATH:
        return
    with open(STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


def main():
    status = {"generated": [], "failed": [], "empty_agenda": [], "steno_missing": []}
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        status["fatal"] = "GEMINI_API_KEY non trovata nei Secrets del repository."
        write_status(status)
        raise ValueError("GEMINI_API_KEY non trovata nelle variabili d'ambiente!")
    client = genai.Client(api_key=api_key)
    os.makedirs(DIGESTS_DIR, exist_ok=True)

    print(f"🚀 build_digest versione {SCRIPT_VERSION} (con fonti, schede, dossier e ascolto audio)")
    print("🗓️  Giorni da generare o aggiornare:")
    dates = dates_to_generate()
    if not dates:
        print("Nessun digest da generare: sono già tutti presenti e aggiornati.")

    for d in dates:
        try:
            generate_for_date(client, d)
            status["generated"].append(d.strftime("%Y-%m-%d"))
            with open(os.path.join(DIGESTS_DIR, d.strftime("%Y-%m-%d") + ".json"), encoding="utf-8") as f:
                saved = json.load(f)
            if not saved.get("recordings"):
                status["empty_agenda"].append(d.strftime("%Y-%m-%d"))
            if saved.get("steno_missing"):
                status["steno_missing"].append(d.strftime("%Y-%m-%d"))
        except Exception as e:
            status["failed"].append({"date": d.strftime("%Y-%m-%d"), "error": str(e)[:500]})
            print(f"❌ Digest del {d.strftime('%d.%m.%Y')} non generato: {e}")

    save_index(load_index())
    save_themes_index()
    save_share_pages()
    write_status(status)
    print("\n✨ Fatto.")
    if dates and len(status["failed"]) == len(dates):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
