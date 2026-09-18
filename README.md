# GST HSN Extractor

A PyQt5 desktop utility that pulls HSN/SAC codes from the GST taxpayer search portal.
Feed it an Excel list of GSTINs; it works through them, handles the captcha on each
lookup, and appends every result to a local CSV as it goes so a crash never costs you
the run.

## Prerequisites

1. **Python 3.9+** (developed and tested on 3.14).
2. **Google Chrome** installed (tested against Chrome 153).
3. That's it — no captcha model to source. Captcha reading uses `ddddocr`, which ships
   its own bundled ONNX model and installs from `requirements.txt`.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt
```

On **Python 3.12+** you need `setuptools` in the environment even though nothing imports
it directly: `distutils` was dropped from the standard library, and
`undetected-chromedriver` still imports it. It is pinned in `requirements.txt`, so a
plain `pip install -r requirements.txt` covers it. Without it you get:

```
ModuleNotFoundError: No module named 'distutils'
```

`undetected-chromedriver` downloads a matching chromedriver on first run, so the first
launch is slower than later ones.

## Running

```bash
python app.py
```

The window walks through three steps:

1. **Generate Excel Template** — writes an `.xlsx` with `GSTIN` and `Company` columns.
2. **Upload Filled Excel** — the sheet is read immediately, so you get either
   `yourfile.xlsx — 12 GSTIN(s) ready` in green or the actual error in red. **Start**
   stays disabled until a valid sheet is loaded.
3. **Start Processing** — leave *headless* unchecked the first time so you can watch the
   browser. The progress bar tracks GSTINs completed; the log shows per-attempt detail.

Results append to `gst_hsn_results.csv` with columns
`Timestamp, GSTIN, Company, HSNs`. **Export Everything to Excel** dumps that entire
store to a workbook, including repeat scrapes of the same GSTIN — nothing is filtered or
deduplicated.

## How the captcha is handled

The portal reveals the captcha only *after* the GSTIN is submitted once, so each lookup
is a two-stage form. The captcha is always **6 digits**, 182×50 px, over a fixed 6px
lattice with a red strikethrough line.

`solve_captcha` inpaints the red line out and runs `ddddocr`. If the read is not exactly
six digits it is discarded without spending a submit, and the page is reloaded for a
fresh captcha — up to `MAX_CAPTCHA_RETRIES` (8) per GSTIN.

**The generic model is weak here: roughly 1 in 6 reads is exact.** The retry loop absorbs
that, so expect several `captcha 'NNNNNN' rejected` lines per GSTIN in the log. That is
normal, not a failure.

### Collecting training data

Every captcha seen is saved under `captcha_samples/`:

- `unlabeled/` — reads that failed or were rejected. Local only.
- `labeled/` — **captchas the portal accepted.** The accepted guess *is* the ground
  truth, so the filename is the label (`636165_9ebfdc87.png`). These accumulate for free
  as you scrape, with no hand-labelling, and are the training set for replacing `ddddocr`
  with a model that actually fits this captcha.

There is no geometric warp to correct — the apparent bulge in the middle of these images
is per-glyph size jitter plus a left-dark/right-light background split. That was measured
and ruled out; the background lattice is perfectly straight.

## Tests

```bash
python test_captcha.py
```

Five contract checks covering captcha reading, sample storage, HSN table parsing, and the
results store. No framework needed.

## Gotchas

- **`ddddocr` must be imported before `PyQt5`.** The reverse order segfaults the
  interpreter on Windows. The import order in `app.py` is deliberate — don't let an
  autoformatter sort it.
- The portal drops a `div.dimmer-holder` overlay while loading that silently swallows
  clicks. Every click waits for it to clear.
- Captcha images are read through a canvas rather than a Selenium element screenshot,
  which comes out scaled by `devicePixelRatio` (228×63 at 1.25) and would not match what
  a trained model expects.
- Goods and services HSNs are merged into one `HSNs` column. Split them if the
  distinction matters.
