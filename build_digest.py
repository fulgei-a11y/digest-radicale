import os
import json
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from google.genai import errors


def generate_digest_with_fallback(client: genai.Client, prompt: str, system_instruction: str) -> str:
    """
    Tenta di generare il contenuto ciclando attraverso una lista di modelli aggiornati.
    Se un modello è sovraccarico (503), ha superato la quota (429) o è deprecato/non trovato (404),
    passa automaticamente al successivo.
    """
    # Lista ordinata di modelli aggiornati e supportati
    models_to_try = [
        "gemini-3.8-flash",  # Modello consigliato da Google per massima disponibilità
        "gemini-2.5-flash",  # Backup temporaneo ad alte prestazioni
        "gemini-2.5-pro",    # Modello Pro avanzato
        "gemini-1.5-flash",  # Fallback di sicurezza
    ]

    config = types.GenerateContentConfig(
        system_instruction=system_instruction,
        tools=[{"google_search": {}}],  # Grounding con Google Search attivo
        temperature=0.3,
    )

    for model in models_to_try:
        try:
            print(f"🔄 Tentativo di generazione con il modello: '{model}'...")
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            )
            print(f"✅ Generazione completata con successo con il modello: '{model}'")
            return response.text

        except errors.APIError as e:
            # Riconosce errori di quota (429), sovraccarico (503) o modello non trovato/deprecato (404/400)
            is_rate_limit = e.code == 429 or "RESOURCE_EXHAUSTED" in str(e)
            is_server_unavailable = e.code == 503 or "UNAVAILABLE" in str(e)
            is_not_found = e.code in (404, 400) or "NOT_FOUND" in str(e)

            if is_rate_limit or is_server_unavailable or is_not_found:
                if is_rate_limit:
                    reason = "Quota esaurita (429)"
                elif is_server_unavailable:
                    reason = "Server momentaneamente sovraccarico (503)"
                else:
                    reason = "Modello non disponibile o deprecato (404)"

                print(f"⚠️ {reason} per '{model}'. Passaggio al modello successivo...")
                time.sleep(2)  # Pausa precauzionale prima del prossimo tentativo
                continue
            else:
                # Errore critico non gestibile (es. API Key non valida) -> interrompe
                print(f"❌ Errore API critico con il modello '{model}': {e}")
                raise e
        except Exception as e:
            print(f"❌ Errore generico con il modello '{model}': {e}")
            raise e

    raise RuntimeError("❌ Impossibile generare il digest: nessun modello tra quelli configurati è disponibile.")


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

    # Generazione del digest gestita con fallback sui modelli
    digest_md = generate_digest_with_fallback(client, prompt, system_instruction)

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

    print("✨ Digest e indice aggiornati con successo!")


if __name__ == "__main__":
    main()
