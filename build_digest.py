import os
import json
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from google.genai import errors


def generate_digest_with_smart_fallback(client: genai.Client, prompt: str, system_instruction: str) -> str:
    """
    Gestisce la generazione usando modelli Flash/Flash-Lite leggeri.
    In caso di errori 429 (quota), attende qualche secondo prima di passare 
    alla chiamata senza search o al modello successivo.
    """
    # Lista di modelli ad alta quota nel piano gratuito
    models_to_try = [
        "gemini-2.5-flash",       # Modello principale veloce
        "gemini-2.5-flash-lite",  # Modello ultra-leggero di riserva
    ]

    last_exception = None

    for model in models_to_try:
        print(f"🔄 Avvio tentativo con il modello: '{model}'...")

        # --- FASE 1: Tentativo con Google Search (Grounding) ---
        for attempt in range(1, 3):
            try:
                print(f"   [Tentativo {attempt}/2] Generazione con Google Search...")
                config = types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    tools=[{"google_search": {}}],
                    temperature=0.3,
                )
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                print(f"✅ Generazione con Search completata con successo usando '{model}'!")
                return response.text

            except errors.APIError as e:
                last_exception = e
                is_rate_limit = e.code == 429 or "RESOURCE_EXHAUSTED" in str(e)
                is_unavailable = e.code == 503 or "UNAVAILABLE" in str(e)

                if is_rate_limit or is_unavailable:
                    wait_time = attempt * 25  # Attende 25s al primo blocco, 50s al secondo
                    print(f"⚠️ Quota o frequenza momentaneamente superata per '{model}'. Attesa di {wait_time} secondi...")
                    time.sleep(wait_time)
                else:
                    print(f"⚠️ Errore con i tools su '{model}': {e}. Interruzione dei tentativi con Search.")
                    break
            except Exception as e:
                last_exception = e
                print(f"❌ Errore generico di rete su '{model}': {e}")
                break

        # --- FASE 2: Fallback SENZA Google Search (consuma molti meno token/quota) ---
        print(f"🔄 Tentativo di generazione pulita (senza Search) su '{model}'...")
        try:
            clean_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.3,
            )
            clean_response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=clean_config,
            )
            print(f"✅ Generazione senza Search completata con successo con '{model}'!")
            return clean_response.text

        except errors.APIError as e:
            last_exception = e
            print(f"⚠️ Quota esaurita anche per la modalità pulita su '{model}': {e}")
            print(f"➡️ Passaggio al modello successivo...")
            time.sleep(5)
        except Exception as e:
            last_exception = e
            print(f"❌ Errore imprevisto su '{model}': {e}")

    raise RuntimeError(
        f"❌ Impossibile generare il digest. La quota del piano gratuito è momentaneamente esaurita per tutti i modelli.\n"
        f"Dettaglio ultimo errore: {last_exception}"
    )


def main():
    # 1. Calcola la data di riferimento (2 giorni fa)
    ref_date = datetime.now() - timedelta(days=2)
    date_str_iso = ref_date.strftime("%Y-%m-%d")
    date_str_it = ref_date.strftime("%d.%m.%Y")

    # 2. Inizializza il client verificando la chiave di ambiente
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

    print(f"🚀 Inizio processo per il digest del {date_str_it}...")

    # Generazione con il sistema di fallback intelligente
    digest_md = generate_digest_with_smart_fallback(client, prompt, system_instruction)

    # 4. Salvataggio del file JSON del giorno
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

    index_data = [item for item in index_data if item.get("date") != date_str_iso]
    index_data.insert(0, {"date": date_str_iso, "label": date_str_it})

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    print("✨ Processo completato: Digest e file index.json aggiornati con successo!")

if __name__ == "__main__":
    main()
