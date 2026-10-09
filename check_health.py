"""
Controllo dopo ogni aggiornamento automatico.

Legge l'esito scritto da build_digest.py e lo stato dell'archivio. Se qualcosa non va
scrive la segnalazione in health.md e comunica al workflow "problems=true": il workflow
apre (o aggiorna) una segnalazione nella scheda Issues del repository, e GitHub manda
una email. Quando tutto torna a posto la segnalazione viene chiusa da sola.
"""

import json
import os
from datetime import datetime, timedelta, timezone

DIGESTS_DIR = "digests"
STATUS_PATH = os.environ.get("DIGEST_STATUS_FILE", "digest_status.json")
DAYS_LAG = max(1, int(os.environ.get("DAYS_LAG", "1") or "1"))
# Quanti giorni di ritardo in più si tollerano prima di segnalare che il sito è fermo
STALE_TOLERANCE = int(os.environ.get("STALE_TOLERANCE", "1"))
GEN_OUTCOME = os.environ.get("GEN_OUTCOME", "")
RUN_URL = os.environ.get("RUN_URL", "")
SITE_URL = os.environ.get("SITE_URL", "https://fulgei-a11y.github.io/digest-radicale/")


def it_date(iso: str) -> str:
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return iso


def main():
    problems, notes = [], []

    status = None
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            status = json.load(f)
    except FileNotFoundError:
        problems.append("Lo script si è fermato prima di finire (nessun esito registrato). "
                        "Di solito è un errore nel codice o nell'installazione delle dipendenze.")
    except Exception as e:
        problems.append(f"Esito dell'esecuzione illeggibile: {e}")

    if status:
        if status.get("fatal"):
            problems.append(status["fatal"])
        for f in status.get("failed", []):
            problems.append(f"Digest del {it_date(f['date'])} non generato: `{f.get('error', '')}`")
        for d in status.get("empty_agenda", []):
            notes.append(f"Il digest del {it_date(d)} è stato scritto senza riuscire a leggere l'agenda "
                         "di radioradicale.it: potrebbe essere generico. Il sito potrebbe aver cambiato struttura.")
        if status.get("generated"):
            notes.append("Digest generati in questa esecuzione: " +
                         ", ".join(it_date(d) for d in status["generated"]) + ".")

    if GEN_OUTCOME and GEN_OUTCOME != "success" and not problems:
        problems.append(f"Il passaggio di generazione è terminato con esito '{GEN_OUTCOME}'.")

    # Il sito è fermo? Si guarda il digest più recente in archivio.
    try:
        with open(os.path.join(DIGESTS_DIR, "index.json"), encoding="utf-8") as f:
            index = json.load(f)
        latest = max((i["date"] for i in index if "date" in i), default="")
    except Exception:
        latest = ""
    today = datetime.now(timezone.utc).date()
    expected = today - timedelta(days=DAYS_LAG)
    if not latest:
        problems.append("L'archivio dei digest è vuoto o illeggibile (digests/index.json).")
    else:
        latest_d = datetime.strptime(latest, "%Y-%m-%d").date()
        late = (expected - latest_d).days
        if late > STALE_TOLERANCE:
            problems.append(f"Il digest più recente è del {it_date(latest)}: mancano {late} giorni "
                            f"(atteso quello del {expected.strftime('%d.%m.%Y')}).")

    has_problems = bool(problems)
    body = []
    if has_problems:
        body.append("L'aggiornamento automatico del digest non è andato a buon fine.\n")
        body.append("**Cosa non va**")
        body += [f"- {p}" for p in problems]
    else:
        body.append("✅ Aggiornamento riuscito.")
    if notes:
        body.append("\n**Note**")
        body += [f"- {n}" for n in notes]
    body.append("")
    if RUN_URL:
        body.append(f"Dettagli dell'esecuzione (log completo): {RUN_URL}")
    body.append(f"Sito: {SITE_URL}")
    if has_problems:
        body.append("\nPer riprovare: scheda **Actions** › *Generate Daily Radicale Digest* › **Run workflow**. "
                    "Questa segnalazione si chiude da sola alla prima esecuzione riuscita.")

    with open("health.md", "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    print("\n".join(body))

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"problems={'true' if has_problems else 'false'}\n")


if __name__ == "__main__":
    main()
