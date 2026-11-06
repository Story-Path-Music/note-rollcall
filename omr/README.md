# omr/ — OMR engine adapter

`run_engine.py` implements `docs/CONTRACT.md` section 1:

```
<engine-python> omr/run_engine.py --engine <oemer|homr|mock> --image <page.png|jpg> --out <result.json>
```

- Exit 0 and write result.json on success; exit != 0 with a one-line reason on stderr on failure (`OMR_DEBUG=1` prints the full traceback).
- Environment variables: `OMR_THREADS` (default 4), `OMR_GPU=1` (only effective if onnxruntime has CUDA),
  `OMR_HOMR_PITCH=cv|tr` (homr pitch source: `cv` = notehead geometry + clef, default; `tr` = the transformer's step/octave).
- Non-mock engines re-launch themselves in UTF-8 mode (the default Windows code page makes homr's musicxml package fail to read files).
- `engines.json`: per-engine venv python path and the recommended PDF rasterization dpi. Paths are machine-local; adjust when deploying (the container image ships `deploy/engines.container.json`).

## Setting up an environment (Python 3.12)

```
python3.12 -m venv omr/.venv-homr
omr/.venv-homr/bin/python -m pip install homr psutil     # Windows: omr/.venv-homr/Scripts/python
```

Model weights are downloaded on first run via `homr.main.download_weights` (about 150 MiB, stored in site-packages). To pre-download, run once on any small image. The `oemer` engine is optional and needs its own environment with older, pinned onnxruntime / OpenCV / scikit-learn versions; homr is the default engine.

## Licensing of components

| Component | License | Notes |
|---|---|---|
| homr (code) | **AGPL-3.0** | Offering recognition as a network service requires offering the corresponding source to its users, including this adapter, which uses homr's internal API in the same process. The repository root `LICENSE` covers this. |
| oemer (code, optional) | MIT | Model weights are published separately by their authors; check their terms before use. |

## Known limitations

- **homr**: notehead coordinates come from classical CV detection (accurate); pitch, clef, key signature and accidentals come from the transformer; the two are aligned per staff with DP. Noteheads that cannot be aligned get pitch from geometry plus key signature (confidence 0.3, recorded in `warnings`); transformer notes without a detected notehead have no coordinates and are dropped. homr internally autocrops and downsizes to 1920 px width; the adapter maps results back to input pixels. Title OCR is skipped.
- Neither engine handles voice separation, ornament marking or transposing instruments (written pitch only).
- Recommended dpi is 150 for homr (it downsizes internally, so 150 and 300 give comparable accuracy).
- homr is sensitive to slight rotation; deskew before calling.
- `oemer` is not recommended: it was unreliable on multi-staff pages and does not support C clefs. `OMR_HANG_S=<seconds>` dumps all thread stacks and exits if oemer hangs (diagnostic).
