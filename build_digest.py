import os
import json
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from google.genai import errors

DIGESTS_DIR = "digests"
INDEX_PATH = os.path.join(DIGESTS_DIR, "index.json")

# Quanti giorni indietro controllare per recuperare digest mancanti
BACKFILL_DAYS = int(os.environ.get("BACKFILL_DAYS", "7"))
# Massimo numero di digest generati per esecuzione (per non esaurire la quota)
MAX_PER_RUN = int(os.environ.get("MAX_PER_RUN", "3"))

MODELS_TO_TRY = [
    "gemini-3.8-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
]

SYSTEM_INSTRUCTION = (
    "Sei un giornalista professionista. Il tuo compito è analizzare la giornata di Radio Radicale "
    "e produrre un digest approfondito, organizzato PER TEMI e scritto in prosa chiara. "
    "Usa solo informazioni verificabili; se qualcosa non è ricostruibile, dichiaralo."
)


def generate_digest_with_fallback(client: genai.Client, prompt: str) -> str:
    """Prova i modelli in ordine; passa al successivo in caso di 429, 503 o 404/400."""
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        tools=[
            {"google_search": {}},  # ricerca web
            {"url_context": {}},    # lettura diretta della pagina dell'agenda
        ],
        temperature=0.3,
    )

    for model in MODELS_TO_TRY:
        try:
            print(f"🔄 Tentativo con il modello '{model}'...")
            response = client.models.generate_content(
                model=model, contents=prompt, config=config
            )
            text = (response.text or "").strip()
            if not text:
                print(f"⚠️ Risposta vuota da '{model}'. Passo al successivo...")
                continue
            print(f"✅ Generato con '{model}'")
            return text

        except errors.APIError as e:
            msg = str(e)
            if e.code == 429 or "RESOURCE_EXHAUSTED" in msg:
                reason = "Quota esaurita (429)"
            elif e.code == 503 or "UNAVAILABLE" in msg:
                reason = "Server sovraccarico (503)"
            elif e.code in (404, 400) or "NOT_FOUND" in msg:
                reason = "Modello non disponibile (404/400)"
            else:
                print(f"❌ Errore API critico con '{model}': {e}")
                raise
            print(f"⚠️ {reason} per '{model}'. Passo al successivo...")
            time.sleep(2)

    raise RuntimeError("❌ Nessun modello disponibile per generare il digest.")


def build_prompt(date_it: str, date_iso: str) -> str:
    return f"""
Produci un digest in italiano sui lavori e le trasmissioni di Radio Radicale relativi alla data: {date_it}.
URL agenda di riferimento: https://www.radioradicale.it/agenda?data={date_iso}

Leggi la pagina dell'agenda e raccogli le informazioni su registrazioni, interventi, audizioni e
rassegne stampa di quella giornata. Sintetizza i contenuti e genera il digest in Markdown con questa struttura:

# Radio Radicale, {date_it}
*Sintesi tematica delle registrazioni e dei lavori parlamentari e giudiziari.*

## In breve
(6-8 punti sintetici con i fatti principali)

## 1. [Titolo del tema principale]
- **Per capire**: contesto ed elementi chiave.
- **Cosa hanno detto / Le posizioni**: chi ha parlato e le tesi sostenute.
- **Cosa succede adesso**: prossimi passaggi.

(ripeti per gli altri temi principali)

## Le altre registrazioni, in breve
## Cosa non è stato possibile ricostruire

Restituisci solo il Markdown, senza blocchi di codice attorno.
"""


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
    # Ricostruisce l'indice anche dai file presenti, così nessun giorno va perso
    known = {item["date"]: item for item in index_data if "date" in item}
    for name in os.listdir(DIGESTS_DIR):
        if name.endswith(".json") and name != "index.json":
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


def generate_for_date(client: genai.Client, ref_date: datetime) -> None:
    date_iso = ref_date.strftime("%Y-%m-%d")
    date_it = ref_date.strftime("%d.%m.%Y")
    print(f"\n📰 Generazione del digest per il {date_it}...")

    digest_md = generate_digest_with_fallback(client, build_prompt(date_it, date_iso))

    with open(os.path.join(DIGESTS_DIR, f"{date_iso}.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"date": date_iso, "label": date_it, "markdown": digest_md},
            f, ensure_ascii=False, indent=2,
        )

    index_data = [i for i in load_index() if i.get("date") != date_iso]
    index_data.append({"date": date_iso, "label": date_it})
    save_index(index_data)


def dates_to_generate() -> list:
    # Data forzata a mano (dal pulsante "Run workflow"), formato AAAA-MM-GG
    forced = os.environ.get("DIGEST_DATE", "").strip()
    if forced:
        return [datetime.strptime(forced, "%Y-%m-%d")]

    # Altrimenti: il giorno di 2 giorni fa + eventuali giorni mancanti negli ultimi BACKFILL_DAYS
    today = datetime.now()
    candidates = [today - timedelta(days=n) for n in range(2, 2 + BACKFILL_DAYS)]
    missing = [
        d for d in candidates
        if not os.path.exists(os.path.join(DIGESTS_DIR, d.strftime("%Y-%m-%d") + ".json"))
    ]
    return missing[:MAX_PER_RUN]


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata nelle variabili d'ambiente!")

    client = genai.Client(api_key=api_key)
    os.makedirs(DIGESTS_DIR, exist_ok=True)

    dates = dates_to_generate()
    if not dates:
        print("Nessun digest da generare: sono già tutti presenti.")
        save_index(load_index())
        return

    failures = 0
    for d in dates:
        try:
            generate_for_date(client, d)
        except Exception as e:
            failures += 1
            print(f"❌ Digest del {d.strftime('%d.%m.%Y')} non generato: {e}")

    print("\n✨ Fatto.")
    if failures == len(dates):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
