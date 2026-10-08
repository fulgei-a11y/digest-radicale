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
