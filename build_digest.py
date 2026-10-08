import os
import re
import json
import time
import urllib.request
import urllib.error
from urllib.parse import urlparse
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
    "Sei un giornalista parlamentare e giudiziario esperto, con anni di lavoro su Radio Radicale. "
    "Il tuo compito è analizzare in profondità la giornata di Radio Radicale e produrre un digest "
    "il più dettagliato e completo possibile, organizzato PER TEMI e scritto in prosa chiara e precisa. "
    "Riporta nomi, cognomi, ruoli e gruppi politici di chi interviene, numeri, date, articoli di legge, "
    "nomi dei provvedimenti e degli organi coinvolti. Distingui sempre i fatti dalle opinioni dei relatori. "
    "Usa solo informazioni verificabili: non inventare mai citazioni, nomi o dati; "
    "se qualcosa non è ricostruibile, dichiaralo esplicitamente."
)

