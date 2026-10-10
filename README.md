# Digest Radio Radicale

Ogni mattina uno script legge l'agenda di Radio Radicale del giorno prima, apre le schede delle
registrazioni e fa scrivere a Gemini un digest diviso per aree e temi, con i link alle fonti.
Poi crea l'MP3 da ascoltare con la voce Paola.

Sito: https://fulgei-a11y.github.io/digest-radicale/

## Come funziona

- **Ogni mattina alle 05:00 UTC** (le 7 d'estate, le 6 d'inverno) parte il workflow
  *Generate Daily Radicale Digest* e genera il digest di **ieri**.
- **Da dove vengono i contenuti**
  - Sedute dell'Aula della Camera: il **resoconto stenografico ufficiale** (testo integrale di ogni intervento).
  - Sedute dell'Aula del Senato: Gemini cerca il resoconto stenografico su senato.it.
  - Convegni, processi, conferenze stampa: Gemini **ascolta l'audio** delle 2 registrazioni più importanti.
  - Tutto il resto: il testo delle schede e l'elenco degli oratori.
  - Gli orari e le durate degli interventi non vengono più scritti. Chi ha parlato senza che si sappia
    cosa ha detto finisce in una riga "Sono intervenuti anche".
- **Il giorno dopo** l'agenda viene ricontrollata. Il digest viene rifatto, una volta sola, se sono
  comparse almeno 3 registrazioni in più o se nel frattempo è uscito lo stenografico della Camera che mancava.
- Se mancano giorni negli ultimi 7, li recupera: al massimo 2 per esecuzione, per non esaurire la quota di Gemini.
- **Audio**: dopo i digest viene creato l'MP3 (voce Paola, gratuita e offline, come in Rassegna e Bollettino)
  per i 3 giorni più recenti, se manca o se il digest è cambiato. Gli MP3 più vecchi di 30 giorni
  vengono cancellati, perché GitHub Pages ha un limite di 1 GB.
- Se qualcosa va storto si apre una segnalazione nella scheda **Issues** e arriva un'email.
  La segnalazione si chiude da sola alla prima esecuzione riuscita.

## Lanciarlo a mano

Scheda **Actions** › *Generate Daily Radicale Digest* › **Run workflow**. Campi (tutti facoltativi):

| Campo | A cosa serve | Predefinito |
|---|---|---|
| Data da generare | Rifà un solo giorno preciso (AAAA-MM-GG) | vuoto = automatico |
| Quanti giorni indietro recuperare | Quanti giorni controllare per i digest mancanti | 7 |
| Quante registrazioni ascoltare | Quante registrazioni far ascoltare a Gemini (0 = nessuna) | 2 |
| Recupero di un periodo: dal giorno | Inizio di un periodo da recuperare | vuoto |
| Recupero di un periodo: al giorno | Fine del periodo | vuoto = fino a ieri |
| Quanti giorni generare al massimo | Limite di digest per esecuzione | 2 |

Esempi:
- **Rifare il 7 ottobre**: Data da generare = `2026-10-07`
- **Recuperare una settimana**: dal giorno = `2026-10-01`, al giorno = `2026-10-07`, massimo = `7`.
  Se la quota non basta, riprova e prosegue da dove si era fermato.
- **Digest più veloce o se la quota scarseggia**: registrazioni da ascoltare = `0`.

## Impostazioni nel workflow (`.github/workflows/daily_digest.yml`)

- `DAYS_LAG: '1'`: di quanti giorni stare indietro (1 = ieri)
- `AUDIO_MAX_MINUTES: '60'`: quanti minuti ascoltare di ogni registrazione
- `cron: '0 5 * * *'`: orario dell'esecuzione automatica, in UTC
- `python tools/build_audio.py --auto 3`: per quanti giorni recenti tenere aggiornato l'MP3

Altre impostazioni che si possono aggiungere nella sezione `env`:
- `RECHECK_MIN_NEW`: quante registrazioni nuove servono per rifare il digest (predefinito 3)
- `MAX_SCHEDE`: quante schede aprire al massimo per giorno (predefinito 60)
- `STENO_MAX_CHARS`: quanto testo dello stenografico passare a Gemini (predefinito 180000 caratteri)
- `AUDIO_KEEP_DAYS`: per quanti giorni tenere gli MP3 (predefinito 30)
- `STALE_TOLERANCE`: giorni di ritardo tollerati prima dell'avviso "sito fermo" (predefinito 1)

## Comandi utili

```
python tools/build_audio.py --date 2026-10-08   # rifà l'MP3 di un giorno
python tools/build_audio.py --auto 3            # MP3 dei 3 giorni più recenti, se mancano o sono vecchi
```

Si lanciano dal computer, nella cartella del repository (servono `ffmpeg` e Linux), oppure si
lascia fare al workflow.

## Configurazione necessaria

- **Secret** `GEMINI_API_KEY`: Settings › Secrets and variables › Actions.
- **GitHub Pages**: Settings › Pages › branch `main`, cartella `/ (root)`.
- **Email degli avvisi**: la segnalazione ti cita (@fulgei-a11y), quindi l'email arriva.
  Controlla anche github.com › Settings › Notifications.

## I file

| File | Cosa contiene |
|---|---|
| `build_digest.py` | Lo script che genera i digest, i dossier e le pagine di condivisione |
| `tools/build_audio.py` | Crea l'MP3 con la voce Paola |
| `check_health.py` | Il controllo finale che decide se aprire l'avviso |
| `index.html` | Il sito |
| `digests/AAAA-MM-GG.json` | Un digest per giorno (con data e ora di generazione) |
| `digests/index.json` | Elenco dei giorni |
| `digests/themes.json` | Tutti i temi di tutti i giorni (archivio "Per tema") |
| `digests/dossiers.json` | I temi raggruppati per vicenda (archivio "Dossier") |
| `audio/AAAA-MM-GG.mp3` e `.json` | L'audio del giorno e l'indice dei temi al suo interno |
| `d/` | Pagine per l'anteprima su WhatsApp, rigenerate a ogni esecuzione |
| `manifest.webmanifest`, `sw.js`, `icons/` | Sito installabile come app e lettura offline |
| `og-image.png` | Immagine dell'anteprima quando si condivide un link |

## Sul sito

- **Sotto la data**: quando il digest è stato pubblicato e, se è stato rifatto, quando è stato aggiornato.
- **Frecce o menu delle date**: cambia giorno. Il nome "Digest" in alto riporta all'ultimo.
- **Ascolta**: fa partire l'MP3 con la voce Paola. Continua a schermo spento, si comanda dalla
  schermata di blocco e dalle cuffie, riprende da dove avevi lasciato. Le frecce del lettore
  saltano da un tema all'altro. Se l'MP3 di quel giorno non c'è, usa la voce del dispositivo.
- **Ascolta da qui**: dentro ogni tema, fa partire l'audio da quel punto.
- **Segui il dossier**: dentro un tema, apre la cronologia di quella vicenda giorno per giorno.
- **Archivio**: per giorno, per tema (con ricerca) e per dossier.
- **Condividi**: link con anteprima per WhatsApp, del giorno o del dossier aperto.
- **PDF**: stampa o salva il digest o il dossier.
- **Installa**: su Android compare il pulsante. Su iPhone: Safari › Condividi › Aggiungi a Home.

## Problemi frequenti

- **Il controllo dice che uno script è incompleto**: il file è stato caricato a metà.
  Ricaricalo per intero: deve finire con `main()`.
- **"Quota giornaliera esaurita"**: aspetta il giorno dopo. I giorni mancanti vengono recuperati da soli.
  Se succede spesso, metti a 0 le registrazioni da ascoltare.
- **Il digest sembra generico e l'avviso parla di agenda illeggibile**: probabilmente
  radioradicale.it ha cambiato la struttura delle pagine.
- **Nel log compare "Stenografico della Camera non ancora disponibile"**: è normale se la Camera
  non l'ha ancora pubblicato. Il giorno dopo si riprova da solo.
- **L'MP3 non si aggiorna**: il digest scritto viene pubblicato lo stesso e arriva l'avviso.
  Rilancia il workflow a mano.
- **L'anteprima su WhatsApp mostra ancora la versione vecchia**: WhatsApp conserva le anteprime
  per un po'. Prova a condividere il link di un altro giorno.
