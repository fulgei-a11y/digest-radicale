import os
import json
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types
from google.genai import errors


def generate_digest_with_smart_fallback(client: genai.Client, prompt: str, system_instruction: str) -> str:
    """
    Tenta prima l'elaborazione con i modelli PRO.
    Se incontra blocchi di quota (429), sovraccarico (503) o limiti del Free Tier,
    scala automaticamente sui modelli FLASH e FLASH-LITE.
    """
    models_to_try = [
        "gemini-2.5-pro",         # Massima qualità giornalistica
        "gemini-2.5-flash",       # Bilanciato, veloce e con ampie quote
        "gemini-2.5-flash-lite",  # Backup ultraleggero ad alta tolleranza
    ]

    last_exception = None

    for model in models_to_try:
        print(f"🔄 Inizio tentativo con il modello: '{model}'...")

        # --- FASE 1: Generazione con Google Search (Grounding) ---
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

                if is_rate_limit:
                    if "pro" in model:
                        print(f"⚠️ Quota del piano gratuito per '{model}' esaurita (429). Scalo subito sui modelli Flash...")
                        break
                    else:
                        wait_time = attempt * 20
                        print(f"⚠️ Quota o frequenza superata per '{model}'. Attesa di {wait_time}s...")
                        time.sleep(wait_time)
                elif is_unavailable:
                    print(f"⚠️ Server sovraccarico (503) per '{model}'. Attesa breve...")
                    time.sleep(5)
                else:
                    print(f"⚠️ Errore con i tools su '{model}': {e}. Interruzione tentativi con Search.")
                    break

            except Exception as e:
                last_exception = e
                print(f"❌ Errore di connessione su '{model}': {e}")
                break

        # --- FASE 2: Fallback SENZA Google Search ---
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
            print(f"⚠️ Quota/accesso negato anche in modalità pulita per '{model}': {e}")
            print(f"➡️ Passaggio al modello successivo...")
            time.sleep(3)
        except Exception as e:
            last_exception = e
            print(f"❌ Errore imprevisto su '{model}': {e}")

    raise RuntimeError(
        f"❌ Impossibile generare il digest. Nessun modello è riuscito a completare la richiesta.\n"
        f"Dettaglio ultimo errore: {last_exception}"
    )


def generate_index_html():
    """Genera la pagina index.html per la visualizzazione web dei digest."""
    html_content = """<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Digest Radio Radicale</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        .prose h1 { font-size: 1.875rem; font-weight: 700; margin-bottom: 1rem; color: #1e293b; }
        .prose h2 { font-size: 1.35rem; font-weight: 600; margin-top: 1.5rem; margin-bottom: 0.75rem; color: #334155; border-bottom: 2px solid #e2e8f0; padding-bottom: 0.25rem; }
        .prose p { margin-bottom: 1rem; line-height: 1.6; color: #475569; }
        .prose ul { list-style-type: disc; padding-left: 1.25rem; margin-bottom: 1rem; color: #475569; }
        .prose li { margin-bottom: 0.5rem; }
        .prose strong { color: #0f172a; }
    </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen flex flex-col">
    <header class="bg-indigo-900 text-white shadow-md">
        <div class="max-w-6xl mx-auto px-4 py-6">
            <h1 class="text-2xl font-bold tracking-tight">📻 Digest Radio Radicale</h1>
            <p class="text-indigo-200 text-sm mt-1">Sintesi automatizzata dei lavori parlamentari, giudiziari ed emittenti</p>
        </div>
    </header>

    <main class="flex-1 max-w-6xl w-full mx-auto px-4 py-8 grid grid-cols-1 md:grid-cols-4 gap-8">
        <!-- Sidebar date -->
        <aside class="md:col-span-1 bg-white p-4 rounded-xl shadow-sm border border-slate-200 h-fit">
            <h2 class="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Archivio Edizioni</h2>
            <ul id="date-list" class="space-y-1">
                <li class="text-slate-400 text-sm">Caricamento archivio...</li>
            </ul>
        </aside>

        <!-- Contenuto del Digest -->
        <section class="md:col-span-3 bg-white p-6 md:p-8 rounded-xl shadow-sm border border-slate-200">
            <div id="content" class="prose max-w-none">
                <p class="text-slate-400">Seleziona una data dall'archivio per leggere il digest.</p>
            </div>
        </section>
    </main>

    <footer class="bg-white border-t border-slate-200 mt-12 py-4 text-center text-xs text-slate-500">
        Generato automaticamente tramite Gemini API & GitHub Actions
    </footer>

    <script>
        async function loadDigest(date) {
            const contentDiv = document.getElementById('content');
            contentDiv.innerHTML = '<p class="text-slate-400">Caricamento in corso...</p>';
            
            try {
                const res = await fetch(`digests/${date}.json`);
                if (!res.ok) throw new Error('Digest non trovato');
                const data = await res.json();
                contentDiv.innerHTML = marked.parse(data.markdown);
                
                // Aggiorna stato attivo nella sidebar
                document.querySelectorAll('#date-list button').forEach(btn => {
                    btn.classList.remove('bg-indigo-50', 'text-indigo-700', 'font-semibold');
                    if (btn.dataset.date === date) {
                        btn.classList.add('bg-indigo-50', 'text-indigo-700', 'font-semibold');
                    }
                });
            } catch (err) {
                contentDiv.innerHTML = `<p class="text-red-500">Errore nel caricamento del digest: ${err.message}</p>`;
            }
        }

        async function init() {
            try {
                const res = await fetch('digests/index.json');
                const index = await res.json();
                const listEl = document.getElementById('date-list');
                
                if (index.length === 0) {
                    listEl.innerHTML = '<li class="text-slate-400 text-sm">Nessun digest presente.</li>';
                    return;
                }

                listEl.innerHTML = index.map(item => `
                    <li>
                        <button 
                            data-date="${item.date}"
                            onclick="loadDigest('${item.date}')" 
                            class="w-full text-left px-3 py-2 rounded-lg text-sm transition hover:bg-slate-100 flex items-center justify-between">
                            <span>${item.label}</span>
                            <span class="text-xs text-slate-400">›</span>
                        </button>
                    </li>
                `).join('');

                // Carica di default il più recente
                loadDigest(index[0].date);
            } catch (err) {
                document.getElementById('date-list').innerHTML = '<li class="text-red-500 text-sm">Errore indice.</li>';
            }
        }

        init();
    </script>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("🌐 File 'index.html' generato e aggiornato con successo!")


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

    # 3. Prompt di istruzioni
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

    # Generazione con il sistema di fallback
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

    # 6. Generazione della pagina HTML per la pubblicazione web (GitHub Pages)
    generate_index_html()

    print("✨ Processo completato: Digest, index.json e index.html aggiornati con successo!")
if __name__ == "__main__":
    main()
