import os
import json
from datetime import datetime, timedelta
from google import genai
from google.genai import types

def main():
    # 1. Calcola la data di riferimento (2 giorni fa per avere palinsesto completo)
    ref_date = datetime.now() - timedelta(days=2)
    date_str_iso = ref_date.strftime("%Y-%m-%d")
    date_str_it = ref_date.strftime("%d.%m.%Y")

    # 2. Inizializza il client con la chiave presente nell'ambiente
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY non trovata nelle variabili d'ambiente!")

    client = genai.Client(api_key=api_key)

    # 3. Prompt di istruzioni per Gemini
    system_instruction = (
        "Sei un giornalista professionista. Il tuo compito è analizzare la giornata di Radio Radicale "
        "e produrre un digest approfondito, organizzato PER TEMI e scritto in prosa chiara."
    )

    prompt = f"""
Produci un digest in italiano sui lavori e le trasmissioni di Radio Radicale relativi alla data: {date_str_it}.
URL agenda di riferimento: https://www.radioradicale.it/agenda?data={date_str_iso}

Raccogli tutte le informazioni sulle registrazioni, interventi, audizioni e rassegne stampa di quella giornata.
Sintetizza i contenuti e genera il digest in formato Markdown seguendo questa struttura:

# Radio Radicale, {date_str_it}
*Sintesi tematica delle registrazioni e dei lavori parlamentari e giudiziari.*

## In breve
(6-8 punti sintetici con i fatti principali)

## 1. [Titolo del tema principale]
- **Per capire**: contesto ed elementi chiave.
- **Cosa hanno detto / Le posizioni**: chi ha parlato e le tesi sostenute.
- **Cosa succede adesso**: prossimi passaggi.

## Le altre registrazioni, in breve
## Cosa non è stato possibile ricostruire
"""

    print(f"Generazione del digest per la data {date_str_it} in corso...")

    # Chiamata al modello con Google Search abilitato (Grounding)
    response = client.models.generate_content(
        model='gemini-2.5-pro',
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[{"google_search": {}}],
            temperature=0.3,
        )
    )

    digest_md = response.text

    # 4. Salvataggio del digest in formato JSON
    os.makedirs("digests", exist_ok=True)
    digest_path = f"digests/{date_str_iso}.json"

    digest_data = {
        "date": date_str_iso,
        "label": date_str_it,
        "markdown": digest_md
    }

    with open(digest_path, "w", encoding="utf-8") as f:
        json.dump(digest_data, f, ensure_ascii=False, indent=2)

    # 5. Aggiornamento dell'indice globale (index.json)
    index_path = "digests/index.json"
    index_data = []

    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            try:
                index_data = json.load(f)
            except json.JSONDecodeError:
                index_data = []

    # Aggiungi o aggiorna l'elemento nell'indice
    index_data = [item for item in index_data if item.get("date") != date_str_iso]
    index_data.insert(0, {"date": date_str_iso, "label": date_str_it})

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    print("Digest e indice aggiornati con successo!")

if __name__ == "__main__":
    main()
