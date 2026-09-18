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

## What gets stored

Results append to `gst_hsn_results.csv`, **one row per HSN/SAC code**, so the sheet
pivots and filters directly. The full taxpayer profile repeats on every row of a given
GSTIN, meaning each row stands alone:

| Group | Columns |
| --- | --- |
| Run | `Timestamp`, `GSTIN`, `Company` (from your input sheet) |
| Taxpayer | `Legal Name of Business`, `Trade Name`, `Additional Trade Name`, `Effective Date of registration`, `Constitution of Business`, `GSTIN / UIN Status`, `Taxpayer Type`, `Administrative Office`, `Other Office`, `Principal Place of Business`, `Whether Aadhaar Authenticated?`, `Whether e-KYC Verified?` |
| Catch-all | `Other Details` — any panel label not named above, so new portal fields are kept rather than silently dropped |
| Code | `Type` (Goods or Services), `HSN`, `Description` |

A taxpayer with no goods or services listed still gets one row, with the code columns
blank — the profile is worth keeping on its own.

Jurisdiction fields are multi-line on the portal and are joined with ` | `, e.g.
`(JURISDICTION - CENTER) | State - CBIC | Zone - MUMBAI | Commissionerate - RAIGARH`.

**Export Everything to Excel** dumps the entire store to a workbook, including repeat
scrapes of the same GSTIN — nothing is filtered or deduplicated.

> Read the CSV back with `dtype=str`. SAC codes like `00440177` have meaningful leading
> zeros, and pandas will happily turn them into `440177` otherwise. `export_to_excel`
> already does this.

## How the captcha is handled

The portal reveals the captcha only *after* the GSTIN is submitted once, so each lookup
is a two-stage form. The captcha is always **6 digits**, 182×50 px, over a fixed 6px
lattice with a red strikethrough line.

`solve_captcha` inpaints the red line out, runs `ddddocr`, and **always returns six
digits**, padding a short read rather than giving up. That is deliberate: a refused
captcha does not navigate away. The portal keeps the GSTIN filled, clears the captcha box
and swaps in a fresh image, so a wrong guess is one round trip and doubles as the cheapest
way to get a new captcha. Reloading the page instead would cost three.

So the page loads **once per GSTIN**, and all `MAX_CAPTCHA_RETRIES` (25) attempts happen
in place at roughly 1.3s each.

**The generic model is weak here: roughly 1 in 6 reads is exact.** Expect a run of
`captcha 'NNNNNN' rejected` lines before each success — that is normal, not a failure.
25 attempts puts the odds of losing a GSTIN near 1 in 100, for about 8s per GSTIN on
average. Guesses ending in several zeros are padded partial reads.

Success and refusal are detected by watching for the taxpayer panel and the portal's
`.err` message at the same time, so a refusal is noticed the moment it appears instead of
costing a full timeout on every retry.

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
  clicks. It *fades* rather than vanishing and can reappear between the check and the
  click, so `click_search` retries on interception instead of trusting one wait.
- `send_keys` into the captcha box is sometimes dropped when Angular re-renders around
  it, leaving the field empty. `type_captcha` reads the value back and retypes.
- The taxpayer panel renders **before** the goods/services section, so the HSN table is
  waited for separately — reading immediately after the panel appears finds nothing.
- Captcha images are read through a canvas rather than a Selenium element screenshot,
  which comes out scaled by `devicePixelRatio` (228×63 at 1.25) and would not match what
  a trained model expects.
- The HSN table sits behind `ng-if="!goodServErrMsg"` and is **absent entirely** for a
  taxpayer with no goods or services. Success is therefore detected on the taxpayer panel
  (`div.tbl-format`), which always renders, not on the table.
- `Additional Trade Name` records only the word `View`. The names themselves load from a
  `getaddltrdnm()` click into a modal, which is not scraped yet.
