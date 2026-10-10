#!/usr/bin/env python3
"""MP3 del digest con la voce italiana Paola (sherpa-onnx + piper, tutto offline e gratuito).

Stesso motore della Rassegna ER e del Bollettino GU.

Uso:
  python3 tools/build_audio.py --auto 3          # i 3 digest più recenti, se l'MP3 manca o il digest è cambiato
  python3 tools/build_audio.py --date 2026-10-08 # un giorno preciso

Produce audio/AAAA-MM-GG.mp3 e audio/AAAA-MM-GG.json:
  {"date", "audio", "duration", "source": <campo "generated" del digest>,
   "segments": [{"id": "brief" | id del tema, "title", "t": secondi}]}
Gli "id" dei temi sono gli stessi che la pagina dà alle schede dei temi: così il sito apre e
evidenzia il tema in ascolto, e un tocco su "Ascolta da qui" salta al punto giusto.
Gli MP3 più vecchi di AUDIO_KEEP_DAYS giorni vengono cancellati (GitHub Pages ha un limite di 1 GB).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unicodedata
import wave

SHERPA_VER = "1.12.14"
SHERPA_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/v{SHERPA_VER}/sherpa-onnx-v{SHERPA_VER}-linux-x64-shared.tar.bz2"
VOICE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/vits-piper-it_IT-paola-medium.tar.bz2"
CACHE = os.path.expanduser("~/.cache/digest-tts")
DIGESTS_DIR = "digests"
AUDIO_DIR = "audio"
KEEP_DAYS = int(os.environ.get("AUDIO_KEEP_DAYS", "30"))

MESI = ['', 'gennaio', 'febbraio', 'marzo', 'aprile', 'maggio', 'giugno',
        'luglio', 'agosto', 'settembre', 'ottobre', 'novembre', 'dicembre']
GIORNI = ['lunedì', 'martedì', 'mercoledì', 'giovedì', 'venerdì', 'sabato', 'domenica']

# Sezioni che non si leggono: elenchi di link, orari, glossario
SKIP_AREAS = ("le altre registrazioni", "glossario", "cosa non", "fonti")
# Etichette dentro un tema che non si leggono
SKIP_LABELS = ("le registrazioni", "per verificare")


# --------------------------------------------------------------------------- #
#  Motore di sintesi (come in rassegna/tools/build_audio.py)
# --------------------------------------------------------------------------- #
def ensure_engine():
    root = os.path.join(CACHE, f"sherpa-onnx-v{SHERPA_VER}-linux-x64-shared")
    vdir = os.path.join(CACHE, "vits-piper-it_IT-paola-medium")
    os.makedirs(CACHE, exist_ok=True)
    for url, check in ((SHERPA_URL, root), (VOICE_URL, vdir)):
        if not os.path.isdir(check):
            arc = os.path.join(CACHE, os.path.basename(url))
            subprocess.run(["curl", "-sSL", "--fail", "--retry", "3", "-o", arc, url], check=True)
            with tarfile.open(arc) as t:
                try:
                    t.extractall(CACHE, filter="data")
                except TypeError:   # Python senza il parametro filter
                    t.extractall(CACHE)
    return root, vdir


def synth_all(jobs, root, vdir, workers=3):
    from concurrent.futures import ThreadPoolExecutor
    exe = os.path.join(root, "bin", "sherpa-onnx-offline-tts")
    env = dict(os.environ, LD_LIBRARY_PATH=os.path.join(root, "lib"))
    base = [exe,
            f"--vits-model={vdir}/it_IT-paola-medium.onnx",
            f"--vits-tokens={vdir}/tokens.txt",
            f"--vits-data-dir={vdir}/espeak-ng-data",
            "--num-threads=1",
            "--vits-length-scale=1.0"]

    def one(job):
        text, out = job
        for _ in range(2):
            r = subprocess.run(base + [f"--output-filename={out}", text], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.exists(out):
                return True
        return False

    with ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, jobs))


# --------------------------------------------------------------------------- #
#  Dal Markdown del digest al testo da leggere
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """Identico a slugify() di build_digest.py e della pagina."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:60]


def inline_text(md: str) -> str:
    """Testo come lo mostra la pagina (serve per calcolare gli id dei temi)."""
    s = re.sub(r"<!--.*?-->", "", md)
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"(\*\*|__|\*|_|`)(.+?)\1", r"\2", s)
    return s.strip()


SIGLE = [
    (r"\bA\.\s?C\.\s?", "Atto Camera "), (r"\bA\.\s?S\.\s?", "Atto Senato "),
    (r"\bC\.\s?(\d)", r"Atto Camera \1"), (r"\bS\.\s?(\d)", r"Atto Senato \1"),
    (r"\bA\.?\s?G\.\s?(?:n\.\s?)?(\d)", r"atto del governo \1"),
    (r"\bd\.?\s?d\.?\s?l\.?\b|\bDDL\b|\bddl\b", "disegno di legge"),
    (r"\bd\.\s?l\.(?=\s)|\bDL\b", "decreto-legge"), (r"\bd\.\s?lgs\.?|\bD\.\s?Lgs\.?", "decreto legislativo"),
    (r"\bd\.\s?p\.\s?r\.", "decreto del presidente della Repubblica"),
    (r"\bartt?\.\s?", "articolo "), (r"\bcomma\s", "comma "), (r"\bn\.\s?(\d)", r"numero \1"),
    (r"\bCSM\b", "Ci Esse Emme"), (r"\bM5S\b", "Movimento 5 Stelle"), (r"\bUe\b|\bUE\b", "Unione europea"),
    (r"\bPD\b", "Pd"), (r"\bFdI\b|\bFDI\b", "Fratelli d'Italia"), (r"\bAVS\b", "Alleanza Verdi e Sinistra"),
    (r"\bIV\b", "Italia Viva"), (r"\bFI\b", "Forza Italia"), (r"\bPNRR\b", "Pnrr"), (r"\bNATO\b", "Nato"),
    (r"\bS\.p\.A\.", "spa"), (r"\becc\.", "eccetera"), (r"\bon\.\s", "onorevole "), (r"\bsen\.\s", "senatore "),
    (r"\bdott\.\s", "dottor "), (r"\bprof\.\s", "professor "), (r"\bvs\.?\s", "contro "),
]


def normalize(s: str) -> str:
    s = re.sub(r"https?://\S+", "", s)
    for rx, rep in SIGLE:
        s = re.sub(rx, rep, s)
    s = re.sub(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
               lambda m: f"{int(m[1])} {MESI[int(m[2])]} {m[3]}" if 1 <= int(m[2]) <= 12 else m[0], s)
    s = re.sub(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b",
               lambda m: f"{int(m[1])} {MESI[int(m[2])]} {m[3]}" if 1 <= int(m[2]) <= 12 else m[0], s)
    s = re.sub(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b", lambda m: f"{int(m[1])} e {int(m[2])}" if m[2] != "00" else f"{int(m[1])}", s)
    s = re.sub(r"\b(\d+)-([A-Z])\b", r"\1 \2", s)          # 2822-B -> 2822 B
    s = re.sub(r"(\d)\.(\d{3})\b", r"\1\2", s)             # 1.500 -> 1500
    s = s.replace("ª", "").replace("º", "").replace("°", "")
    s = s.replace("€", " euro").replace("%", " per cento").replace("&", " e ")
    s = re.sub(r"(^|[\s(:])\+(\d)", r"\1più \2", s)
    s = re.sub(r"\s[—–]\s", ". ", s).replace("—", ", ").replace("·", ",").replace("/", " ")
    s = re.sub(r"[\"«»“”*_#`|<>\[\]]", "", s)
    s = re.sub(r"\(\s*[,;\s]*\)", "", s)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def sentence(s: str) -> str:
    s = normalize(s)
    if s and not re.search(r"[.!?:]$", s):
        s += "."
    return s


def parts_from_markdown(md: str):
    """Restituisce [(id, titolo, [frasi])]: un pezzo per "In breve" e uno per ogni tema."""
    md = re.sub(r"<!--.*?-->", "", md)
    lines = md.split("\n")
    parts, cur, area, skip_area, skip_label, area_said = [], None, "", False, False, False

    def start(pid, title, first):
        nonlocal cur
        cur = (pid, title, [first] if first else [])
        parts.append(cur)

    for raw in lines:
        line = raw.rstrip()
        if line.startswith("# ") or not line.strip():
            continue
        if line.startswith("## "):
            area = inline_text(re.sub(r"^\d+[.)]\s*", "", line[3:]))
            skip_area = area.lower().startswith(SKIP_AREAS)
            area_said = False
            skip_label = False
            if area.lower().startswith("in breve"):
                start("brief", "In breve", "In breve.")
            else:
                cur = None
            continue
        if skip_area:
            continue
        if line.startswith("### "):
            title = inline_text(re.sub(r"^\d+[.)]\s*", "", line[4:]))
            intro = (sentence(area) + " " if not area_said else "") + sentence(title)
            area_said = True
            skip_label = False
            start(slugify(title), title, intro)
            continue
        if cur is None:
            continue
        text = line.strip()
        m = re.match(r"^(?:[-*]\s+)?\*\*([^*]+?)\*\*\s*:?\s*(.*)$", text)
        bullet = re.match(r"^[-*]\s+(.*)$", text)
        if m and not bullet:
            label, rest = m.group(1).strip().rstrip(":"), m.group(2)
            skip_label = label.lower().startswith(SKIP_LABELS)
            if skip_label:
                continue
            if label.lower() == "in una riga":
                cur[2].append(sentence(rest))
            else:
                cur[2].append(sentence(label) + (" " + sentence(rest) if rest.strip() else ""))
            continue
        if skip_label:
            continue
        if bullet:
            item = bullet.group(1)
            bm = re.match(r"^\*\*([^*]+?)\*\*\s*:?\s*(.*)$", item)
            if bm:   # "**Nome (ruolo, gruppo)**: cosa ha detto"
                who = re.sub(r"\s*\(([^)]*)\)", r", \1", bm.group(1)).rstrip(":")
                item = who + ": " + bm.group(2)
            item = re.sub(r"\[(ascolta|fonte|qui|link|scheda)\]\([^)]*\)", "", item, flags=re.I)
            cur[2].append(sentence(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", item)))
            continue
        text = re.sub(r"\[(ascolta|fonte|qui|link|scheda)\]\([^)]*\)", "", text, flags=re.I)
        cur[2].append(sentence(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)))
    return [p for p in parts if len(" ".join(p[2])) > 20]


def split_long(text, limit=500):
    out, buf = [], ""
    for p in re.split(r"(?<=[.!?;:])\s+", text):
        if len(buf) + len(p) + 1 > limit and buf:
            out.append(buf)
            buf = p
        else:
            buf = (buf + " " + p).strip()
    if buf:
        out.append(buf)
    return out


# --------------------------------------------------------------------------- #
#  Un giorno
# --------------------------------------------------------------------------- #
def build_day(date: str, root: str, vdir: str) -> None:
    with open(os.path.join(DIGESTS_DIR, f"{date}.json"), encoding="utf-8") as f:
        digest = json.load(f)
    parts = parts_from_markdown(digest.get("markdown", ""))
    if not parts:
        print(f"⚠️ {date}: niente da leggere")
        return
    y, m, d = map(int, date.split("-"))
    wd = GIORNI[datetime.date(y, m, d).weekday()]
    intro = f"Digest di Radio Radicale di {wd} {d} {MESI[m]} {y}."

    tmp = tempfile.mkdtemp()
    jobs, owner = [(intro, os.path.join(tmp, "intro.wav"))], [-1]
    for i, (_, _, sentences) in enumerate(parts):
        k = 0
        for s in sentences:
            for piece in split_long(s):
                jobs.append((piece, os.path.join(tmp, f"{i:04d}_{k:03d}.wav")))
                owner.append(i)
                k += 1
    print(f"🎙️ {date}: {len(parts)} parti, {len(jobs)} frasi da sintetizzare")
    ok = synth_all(jobs, root, vdir)
    if sum(ok) < len(jobs) * 0.95:
        raise RuntimeError(f"sintesi fallita per {len(jobs) - sum(ok)} frasi su {len(jobs)}")

    full = os.path.join(tmp, "full.wav")
    rate, t, last, segs = None, 0.0, None, []
    with wave.open(full, "wb") as w:
        for (_, p), i in zip(jobs, owner):
            if not os.path.exists(p):
                continue
            with wave.open(p, "rb") as r:
                if rate is None:
                    rate = r.getframerate()
                    w.setnchannels(1)
                    w.setsampwidth(r.getsampwidth())
                    w.setframerate(rate)
                if i != last and i >= 0:
                    gap = 1.0
                    w.writeframes(b"\x00\x00" * int(rate * gap))
                    t += gap
                    segs.append({"id": parts[i][0], "title": parts[i][1], "t": round(t, 2)})
                    last = i
                w.writeframes(r.readframes(r.getnframes()))
                t += r.getnframes() / rate
                w.writeframes(b"\x00\x00" * int(rate * 0.25))
                t += 0.25

    os.makedirs(AUDIO_DIR, exist_ok=True)
    mp3 = os.path.join(AUDIO_DIR, f"{date}.mp3")
    for br in ("40k", "32k", "24k"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", full, "-ac", "1", "-ar", "22050",
                        "-codec:a", "libmp3lame", "-b:a", br,
                        "-metadata", f"title=Digest Radio Radicale {d} {MESI[m]} {y}",
                        "-metadata", "artist=Digest Radio Radicale", mp3], check=True)
        if os.path.getsize(mp3) < 18 * 1024 * 1024:
            break
    meta = {"date": date, "audio": f"audio/{date}.mp3", "duration": round(t, 1),
            "source": digest.get("generated", ""), "segments": segs}
    with open(os.path.join(AUDIO_DIR, f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"✅ {mp3}: {os.path.getsize(mp3) // 1024} KB, {t / 60:.1f} minuti")


def needs_audio(date: str) -> bool:
    try:
        with open(os.path.join(DIGESTS_DIR, f"{date}.json"), encoding="utf-8") as f:
            generated = json.load(f).get("generated", "")
    except Exception:
        return False
    if not os.path.exists(os.path.join(AUDIO_DIR, f"{date}.mp3")):
        return True
    try:
        with open(os.path.join(AUDIO_DIR, f"{date}.json"), encoding="utf-8") as f:
            return json.load(f).get("source", "") != generated
    except Exception:
        return True


def cleanup() -> None:
    if not os.path.isdir(AUDIO_DIR):
        return
    limit = (datetime.date.today() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    removed = 0
    for name in os.listdir(AUDIO_DIR):
        m = re.match(r"(\d{4}-\d{2}-\d{2})\.(mp3|json)$", name)
        if m and m.group(1) < limit:
            os.remove(os.path.join(AUDIO_DIR, name))
            removed += 1
    if removed:
        print(f"🧹 Tolti {removed} file audio più vecchi di {KEEP_DAYS} giorni.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", action="append", default=[])
    ap.add_argument("--auto", type=int, default=0, help="controlla i N digest più recenti")
    a = ap.parse_args()

    dates = list(a.date)
    if a.auto:
        with open(os.path.join(DIGESTS_DIR, "index.json"), encoding="utf-8") as f:
            recent = sorted((i["date"] for i in json.load(f)), reverse=True)[:a.auto]
        dates += [d for d in recent if needs_audio(d) and d not in dates]
    if not dates:
        print("Audio già aggiornato.")
        cleanup()
        return
    root, vdir = ensure_engine()
    failed = []
    for d in dates:
        try:
            build_day(d, root, vdir)
        except Exception as e:
            failed.append(d)
            print(f"❌ Audio del {d} non generato: {e}")
    cleanup()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
