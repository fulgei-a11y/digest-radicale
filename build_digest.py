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
