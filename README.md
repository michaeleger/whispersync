# whispersync

Second-opinion podcast transcripts: keep Riverside's speakers, take Whisper's words, and get a short list of every spot where they disagree.

## Why

Riverside's automatic transcript gets speaker turns right but garbles names, numbers, drug names and book titles. Whisper, run locally, hears the words better but has no idea who is talking. Fixing a 50-minute transcript by hand means re-listening to the whole thing. whispersync merges the two (plus an optional Gemini pass as a tie-breaker) and hands you only the few dozen spots worth checking by ear.

Built for the [Eager to Be Healthy](https://www.eagertobehealthy.com) podcast. It works for any two-person interview recorded on Riverside.

## What it does

- Pulls episodes from a podcast RSS feed (MP3 plus the feed's transcript), or takes a local audio file and Riverside transcript for pre-publish checks.
- Transcribes locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (`large-v3-turbo`, CPU only, int8).
- Re-listens where Whisper skipped speech that Riverside has.
- Optional Gemini pass: a three-way vote settles names and numbers.
- Writes a synced transcript, `review.md` (names, numbers and skipped speech first) and `review.csv`.
- `fixes.txt` per episode: your corrections are re-applied on every rebuild.
- Hands-off mode: a systemd path unit runs jobs dropped into `inbox/`, one at a time.

**Status:** in production use on the author's server since October 2026. Single-file Python, with a built-in `selftest`.

## Requirements

- Linux (tested on Ubuntu, Python 3.9+; developed on 3.14)
- `ffmpeg` (recommended; it falls back to PyAV)
- About 2 to 3 GB of free RAM and about 1.5 GB of disk for the model
- Optional: a Google AI Studio API key (`GEMINI_API_KEY`) for the Gemini pass

Speed: about 4.5x real time on 6 CPU threads (51 minutes of audio in 11 minutes).

## Install

```bash
git clone https://github.com/michaeleger/whispersync.git
cd whispersync
./setup.sh                 # creates .venv, installs deps, downloads the model, runs the self-test
cp .env.example .env       # optional: add GEMINI_API_KEY for the Gemini pass
```

To do it by hand instead: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then `./whispersync selftest`. The model downloads on first run into `models/`.

Edit `glossary.txt` with the names Whisper should always spell right (your show, your host name). Keep it short.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `WS_FEED` | the Eager to Be Healthy RSS feed | Your podcast's RSS feed URL |
| `WS_HOST` | `Michael Scott Eger` | Host's name as it appears in transcripts |
| `WS_WORK` | `./work` | Per-episode working files |
| `WS_OUT` | `./out` | Where the finished human-facing files get copied |
| `WS_INBOX` | `./inbox` | Hands-off job inbox |
| `WS_MODELS` | `./models` | Whisper model cache |
| `WS_MODEL` | `large-v3-turbo` | Whisper model |
| `WS_THREADS` | `6` | CPU threads |
| `WS_GEMINI_MODEL` | `gemini-flash-latest` | Gemini model for the third opinion |

## Usage

```bash
./whispersync episodes                 # list recent episodes from the feed
./whispersync run latest               # or: run 3, or run <word from the title>
./whispersync sync <episode>           # re-run comparison + fixes only (seconds)
./whispersync repair <episode> --force # redo the re-listen pass
./whispersync google <episode>         # Gemini third opinion only
./whispersync selftest                 # alignment logic check, no model or network

# Pre-publish check, no feed:
./whispersync run --audio export.mp3 --riverside transcript.txt \
    --name jane-doe --guest "Jane Doe"
```

Run `./whispersync --help` and `./whispersync run --help` for all options.

### Fixes

Put corrections in `work/<episode>/fixes.txt`, one per line. They are applied every time the transcript is rebuilt:

```
palpazo beans => garbanzo beans          # replace everywhere
@14:45 Jon Smyth => Jon Smith            # replace only near 14:45
@40:55 >> Jane Doe: NAC?                 # make those words their own turn for that speaker
@40:45 [Riverside only, Whisper missed this] N A C, mm. =>   # empty right side drops it
```

Per-episode glossary words (book titles, company names) go in `work/<episode>/glossary.txt`; rerun with `--force`.

## Optional: hands-off mode

```bash
./install-watcher.sh     # systemd *user* path unit that wakes when a job lands in inbox/
# or poll on a timer:
./install-cron.sh 20     # cron every 20 minutes, also checks the RSS feed
```

Run `sudo loginctl enable-linger $USER` once if you want the watcher to run while you are logged out. To remove it: `systemctl --user disable --now whispersync-inbox.path`.

An inbox job is a JSON file such as `inbox/anything.json`:

```json
{"name": "jane-doe", "guest": "Jane Doe", "title": "Jane Doe pre-publish",
 "audio_url": "https://example.com/path/to/episode.mp3",
 "riverside_text": "Jane Doe (0:00)\nHi, I'm Jane...\n\nHost Name (0:09)\n...",
 "glossary": ["Jane Doe", "Her Book Title"]}
```

`audio` (a file path) works in place of `audio_url`, and `riverside` (a path) or `riverside_url` in place of `riverside_text`. `{"name": "jane-doe", "action": "sync"}` only rebuilds. Finished jobs move to `inbox/done/` and failed ones to `inbox/failed/` with an `.error.txt` file. If Ollama has a model loaded, the job waits rather than running both at once.

## Limitations

- Assumes two-person interviews in Riverside's transcript format (`Name (mm:ss)` turns) or `Name: text` feed transcripts.
- CPU only. There is no GPU path wired up.
- Some defaults (the feed URL and the host name) point at Eager to Be Healthy. Override them with the environment variables above.
- The review still needs a human ear. It narrows the work down but does not replace listening.

## License

MIT. See [LICENSE](LICENSE).
