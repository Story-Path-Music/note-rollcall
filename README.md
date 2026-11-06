# note-rollcall

Optical music recognition (OMR) service for note-name labeling.
音名標記用的樂譜辨識服務。

The service reads an uploaded score (PDF / image), detects noteheads, clefs and accidentals with the [homr](https://github.com/liebharc/homr) engine, and returns note positions with pitch names. It also renders labeled PDF / PNG exports. This repository contains the recognition pipeline and the HTTP server (FastAPI); the web frontend code is not part of this repository.

This repository is the corresponding source offered under AGPL-3.0 section 13 for the hosted service built from it.
本 repo 是依 AGPL-3.0 第 13 條，對以此原始碼提供的線上服務所公開的對應原始碼。

## License 授權

Licensed under the **GNU Affero General Public License v3.0** (see [LICENSE](LICENSE)).

The recognition engine [homr](https://github.com/liebharc/homr) (AGPL-3.0, (c) its authors) is imported directly by `omr/run_engine.py`, so the combined work is distributed under the same license. Model weights are downloaded from the homr project at build time and are subject to that project's terms. The accidental glyph subset in `web/fonts/` is derived from Bravura (SIL Open Font License 1.1, see `web/fonts/OFL-Bravura.txt`).

## Layout 內容

| Path | Description |
|---|---|
| `omr/` | Recognition pipeline (`run_engine.py`), clef and accidental detection (`clefs.py`, `accidentals.py`) and their templates |
| `server/` | FastAPI service: upload, job queue, progress, export, optional remote page workers |
| `deploy/` | Dockerfile, pinned dependency versions, model checksums, reference compose file, optional Cloud Run worker scripts |
| `docs/CONTRACT.md` | Engine CLI and HTTP API specification |
| `web/fonts/` | Accidental glyph font subset used by the PDF / PNG export |

## Build and run locally 建置與執行

Local development server (uses the bundled mock engine unless an engine is configured):

```
cd server
python -m venv .venv
.venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
./run_dev.sh                                # Windows: powershell -File run_dev.ps1
```

The dev server listens on http://localhost:8787. To run the real engine, install `homr` in a Python 3.12 environment, see `omr/README.md`.

Container image (downloads the homr models at build time and verifies them against `deploy/models.sha256`):

```
docker build -f deploy/Dockerfile -t notes-omr:1 .
```

Tests:

```
server/.venv/bin/python -m pytest -q server/tests
```

More details: `server/README.md`, `omr/README.md`, `deploy/cloudrun/README.md`.

## Issues 回報問題

Please open a GitHub Issue.
