import sys
import os
import time
import uuid
import base64
import cv2
import numpy as np
import pandas as pd
import ddddocr  # must be imported BEFORE PyQt5 - the reverse order segfaults on Windows
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (ElementClickInterceptedException,
                                        InvalidSessionIdException, NoSuchWindowException,
                                        TimeoutException)

# Retrying cannot bring these back - the browser is gone. Abort instead of spinning.
SESSION_DEAD = (InvalidSessionIdException, NoSuchWindowException)
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
                             QFileDialog, QCheckBox, QLabel, QMessageBox, QTextEdit,
                             QProgressBar)

STYLESHEET = """
QWidget { background: #f7f8fa; font-family: 'Segoe UI'; font-size: 13px; color: #202124; }
QLabel#heading { font-size: 11px; font-weight: 600; color: #5f6368;
                 text-transform: uppercase; letter-spacing: 1px; padding-top: 6px; }
QPushButton { background: #ffffff; border: 1px solid #d0d3d8; border-radius: 5px; padding: 9px 14px; }
QPushButton:hover:enabled { border-color: #1a73e8; color: #1a73e8; }
QPushButton:disabled { color: #b0b3b8; background: #f1f2f4; }
QPushButton#primary:enabled { background: #1a73e8; border-color: #1a73e8; color: #ffffff; font-weight: 600; }
QPushButton#primary:hover:enabled { background: #1666d0; color: #ffffff; }
QProgressBar { border: 1px solid #d0d3d8; border-radius: 5px; background: #ffffff;
               height: 20px; text-align: center; }
QProgressBar::chunk { background: #1a73e8; border-radius: 4px; }
QTextEdit { background: #ffffff; border: 1px solid #d0d3d8; border-radius: 5px;
            font-family: Consolas, monospace; font-size: 12px; }
"""

SAMPLE_DIR = "captcha_samples"
# Measured across live runs, ddddocr lands about 1 read in 10 - worse than a small
# sample first suggested. A retry costs ~0.9s because a refused captcha refreshes in
# place, so retries are cheap and worth spending: 40 leaves roughly a 2% chance of losing
# a GSTIN, for ~10s each on average. A GSTIN that still fails is simply not recorded, so
# the next run picks it up again.
MAX_CAPTCHA_RETRIES = 40

# The running store. Append-only CSV rather than xlsx on purpose: appending a row is one
# line and survives a crash mid-run, where rewriting a whole workbook every scrape does not.
RESULTS_FILE = "gst_hsn_results.csv"

# The portal drops a loading overlay over the page; it silently eats clicks.
DIMMER = (By.CSS_SELECTOR, "div.dimmer-holder")

# An element screenshot comes out scaled by devicePixelRatio (228x63 at 1.25). Reading the
# img through a canvas gives its native 182x50, which is what a trained model will expect.
CAPTCHA_PNG_JS = """
const img = arguments[0];
const c = document.createElement('canvas');
c.width = img.naturalWidth; c.height = img.naturalHeight;
c.getContext('2d').drawImage(img, 0, 0);
return c.toDataURL('image/png').split(',')[1];
"""

# beta=True picks ddddocr's second bundled model. Measured on real GST captchas it lands
# ~20% against the default model's ~10%, including 3/20 of the ones the default misses
# outright. Costs nothing - same call, same speed.
# ponytail: still a generic model. A net trained on this font would clear 95%; see the
# training-data note in README before trusting captcha_samples/labeled for that.
_ocr = ddddocr.DdddOcr(beta=True, show_ad=False)


def solve_captcha(png_bytes):
    """Read a GST captcha. Always returns exactly 6 digits.

    A short or long read is wrong either way, and there is no reason to skip submitting
    it: the portal rejects a wrong guess in place, swaps in a fresh image and keeps the
    GSTIN filled, which is one round trip. Reloading the page to get a new captcha costs
    three. So pad the read out to six and always take the shot.
    """
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return "000000"

    # Inpaint out the red strikethrough line; measurably better than feeding the raw image.
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red_mask = (cv2.inRange(hsv, np.array([0, 50, 50]), np.array([10, 255, 255]))
                + cv2.inRange(hsv, np.array([170, 50, 50]), np.array([180, 255, 255])))
    cleaned = cv2.inpaint(img, red_mask, 3, cv2.INPAINT_TELEA)

    text = _ocr.classification(cv2.imencode('.png', cleaned)[1].tobytes())
    digits = "".join(c for c in text if c.isdigit())
    return (digits + "000000")[:6]


# Taxpayer panel fields, in the order the portal lays them out. Fixed rather than
# "whatever labels turn up", because this is an append-only store and the header has to
# stay stable across runs. Conditional fields simply come back empty.
PROFILE_FIELDS = [
    "Legal Name of Business",
    "Trade Name",
    "Additional Trade Name",
    "Effective Date of registration",
    "Constitution of Business",
    "GSTIN / UIN Status",
    "Taxpayer Type",
    "Administrative Office",
    "Other Office",
    "Principal Place of Business",
    "Whether Aadhaar Authenticated?",
    "Whether e-KYC Verified?",
]

RESULT_COLUMNS = (["Timestamp", "GSTIN", "Company"] + PROFILE_FIELDS
                  + ["Other Details", "Type", "HSN", "Description"])

# The taxpayer panel is always rendered on a successful search. The HSN table is not -
# it sits behind ng-if="!goodServErrMsg" and vanishes when a taxpayer lists no goods or
# services - so the panel, not the table, is what tells an accepted captcha from a refused one.
PROFILE_PANEL = (By.CSS_SELECTOR, "div.tbl-format")

# Shown as "Enter valid letters shown in the image below" when a captcha is refused.
CAPTCHA_ERROR = (By.CSS_SELECTOR, ".err")

HSN_TABLE = (By.XPATH, "//table[contains(@class, 'table-bordered')]")
HSN_ROWS = (By.XPATH, "//table[contains(@class, 'table-bordered')]//tr")


def search_outcome(driver):
    """'found', 'captcha', or None while neither has appeared yet.

    Watching for both means a refused captcha is noticed the moment the portal says so,
    instead of costing a full timeout on the success condition every single retry.
    """
    if driver.find_elements(*PROFILE_PANEL):
        return "found"
    for element in driver.find_elements(*CAPTCHA_ERROR):
        if element.is_displayed() and element.text.strip():
            return "captcha"
    return None

# The results table is [Goods HSN | Goods Desc | Services HSN | Services Desc], and its
# header row is <td> inside <tbody>, so position alone cannot tell data from heading.
HSN_LAYOUT = (("Goods", 0, 1), ("Services", 2, 3))


def _tidy(text):
    """Collapse the portal's stray whitespace so labels match and cells stay readable."""
    return " ".join(text.split())


def extract_hsn_rows(rows):
    """[(kind, code, description)] for every HSN/SAC in the results table.

    Codes are numeric, which is what separates them from the 'HSN' header cell and from
    'NA' placeholders. Order is preserved and repeats dropped.
    """
    found = []
    seen = set()
    for row in rows:
        cells = row.find_elements(By.XPATH, './td')
        for kind, code_index, desc_index in HSN_LAYOUT:
            if code_index >= len(cells):
                continue
            code = _tidy(cells[code_index].text)
            if not code.isdigit() or (kind, code) in seen:
                continue
            seen.add((kind, code))
            description = _tidy(cells[desc_index].text) if desc_index < len(cells) else ""
            found.append((kind, code, description))
    return found


def click_search(driver, wait, attempts=5):
    """Press SEARCH, working around the loading overlay that swallows clicks.

    The overlay fades out rather than vanishing, and can come back between the check and
    the click, so a single invisibility wait is not enough - retry on interception.
    """
    for _ in range(attempts):
        try:
            wait.until(EC.invisibility_of_element_located(DIMMER))
            driver.find_element(By.ID, "lotsearch").click()
            return True
        except ElementClickInterceptedException:
            time.sleep(0.5)
    return False


def type_captcha(element, guess, attempts=3):
    """Type the guess and confirm it actually landed.

    Angular re-renders the form around this input, and a send_keys that lands mid-render
    is silently dropped - the field stays empty and the submit is wasted. Reading the
    value back is the only way to know it stuck.
    """
    for _ in range(attempts):
        element.clear()
        element.send_keys(guess)
        if element.get_attribute("value") == guess:
            return True
        time.sleep(0.3)
    return False


def extract_profile(driver):
    """Label -> value for every field in the taxpayer detail panel.

    Each field is a column div holding a <strong> label followed by either value
    paragraphs, a <ul> of jurisdiction lines, or a link. All three shapes are read the
    same way so nothing in the panel is skipped.
    """
    profile = {}
    for col in driver.find_elements(By.CSS_SELECTOR, "div.tbl-format div.col-sm-4"):
        labels = col.find_elements(By.TAG_NAME, "strong")
        if not labels:
            continue
        # The label shares its <p> with the strong; value paragraphs never contain one.
        values = [_tidy(p.text) for p in col.find_elements(By.XPATH, './p')
                  if not p.find_elements(By.TAG_NAME, 'strong') and p.text.strip()]
        values += [_tidy(li.text) for li in col.find_elements(By.XPATH, './ul/li')
                   if li.text.strip()]
        # ponytail: Additional Trade Name is only a "View" link - the names themselves sit
        # behind a click. Recording the link at least says the data exists.
        values += [_tidy(a.text) for a in col.find_elements(By.XPATH, './a') if a.text.strip()]
        profile[_tidy(labels[0].text)] = " | ".join(values)
    return profile


def append_results(gstin, company, profile, hsn_rows):
    """Append one row per HSN. Profile fields repeat so every row stands alone in Excel."""
    # A store written by an older version has a different header. Appending under it makes
    # a CSV pandas cannot parse, so roll it aside instead of corrupting either one.
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            header = f.readline().strip().split(",")
        if header != RESULT_COLUMNS:
            os.rename(RESULTS_FILE, f"{RESULTS_FILE}.{time.strftime('%Y%m%d%H%M%S')}.old")

    base = {"Timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "GSTIN": gstin, "Company": company}
    base.update({field: profile.get(field, "") for field in PROFILE_FIELDS})
    # Any label the portal shows that PROFILE_FIELDS does not name is kept, not dropped.
    extras = {k: v for k, v in profile.items() if k not in PROFILE_FIELDS}
    base["Other Details"] = " ; ".join(f"{k}: {v}" for k, v in extras.items())

    # A taxpayer with no codes listed still deserves a row - the profile is the find.
    records = [dict(base, Type=kind, HSN=code, Description=description)
               for kind, code, description in (hsn_rows or [("", "", "")])]

    frame = pd.DataFrame(records, columns=RESULT_COLUMNS)
    frame.to_csv(RESULTS_FILE, mode='a', header=not os.path.exists(RESULTS_FILE), index=False)
    return len(records)


def normalise_gstin(value):
    """GSTINs are compared across an input sheet and the store, so spelling must match."""
    return str(value).strip().upper()


def scraped_today(when=None):
    """GSTINs already in the store bearing today's date.

    Restarting a run should resume, not start over. Scoped to the day rather than to all
    time so a GSTIN can still be refreshed tomorrow.
    """
    if not os.path.exists(RESULTS_FILE):
        return set()
    day = when or time.strftime("%Y-%m-%d")
    try:
        df = pd.read_csv(RESULTS_FILE, dtype=str)
    except Exception:
        return set()          # an unreadable store must not block a run
    if "Timestamp" not in df.columns or "GSTIN" not in df.columns:
        return set()
    today = df[df["Timestamp"].fillna("").str.startswith(day)]
    return {normalise_gstin(g) for g in today["GSTIN"].dropna()}


def export_to_excel(path):
    """Dump the whole store to a workbook. Returns how many rows went out."""
    # dtype=str or pandas reads 00440177 as the number 440177 and the leading zeros,
    # which are part of the SAC code, are gone. Every column here is text anyway.
    df = pd.read_csv(RESULTS_FILE, dtype=str)
    df.to_excel(path, index=False)
    return len(df)


def save_sample(png_bytes, label=None):
    """Bank the captcha for training. A label means the portal confirmed that guess."""
    folder = os.path.join(SAMPLE_DIR, "labeled" if label else "unlabeled")
    os.makedirs(folder, exist_ok=True)
    name = f"{label}_{uuid.uuid4().hex[:8]}.png" if label else f"{uuid.uuid4().hex}.png"
    path = os.path.join(folder, name)
    with open(path, "wb") as f:
        f.write(png_bytes)
    return path

class ScraperThread(QThread):
    progress_signal = pyqtSignal(str)
    count_signal = pyqtSignal(int, int)   # (done, total)
    finished_signal = pyqtSignal()

    def __init__(self, excel_path, headless=False):
        super().__init__()
        self.excel_path = excel_path
        self.headless = headless
        self._is_running = True
        self.output_file = RESULTS_FILE

    def run(self):
        try:
            df = pd.read_excel(self.excel_path)
        except Exception as e:
            self.progress_signal.emit(f"Failed to read Excel: {str(e)}")
            self.finished_signal.emit()
            return
            
        options = uc.ChromeOptions()
        if self.headless:
            options.add_argument('--headless')
        
        driver = uc.Chrome(options=options)
        wait = WebDriverWait(driver, 10)
        # Shorter: a wrong captcha means waiting this out on every retry.
        result_wait = WebDriverWait(driver, 6)
        # Only paid once per successful scrape, so it can afford to be patient.
        table_wait = WebDriverWait(driver, 8)
        
        total = int(df["GSTIN"].notna().sum()) if "GSTIN" in df.columns else 0
        done = 0
        self.count_signal.emit(0, total)

        # Resume rather than restart. Anything already scraped today is skipped, so
        # stopping and starting again picks up where the last run left off.
        done_today = scraped_today()
        if done_today:
            self.progress_signal.emit(
                f"Resuming - {len(done_today)} GSTIN(s) already scraped today will be skipped.")

        for index, row in df.iterrows():
            if not self._is_running:
                self.progress_signal.emit("Process stopped by user.")
                break

            gstin = row.get("GSTIN")
            if pd.isna(gstin):
                continue
            gstin = normalise_gstin(gstin)

            if gstin in done_today:
                done += 1
                self.count_signal.emit(done, total)
                self.progress_signal.emit(f"Skipping {gstin} - already scraped today")
                continue

            try:
                # Loaded once per GSTIN, not once per attempt: a rejected captcha leaves us
                # on this page with the GSTIN still filled and a fresh captcha already
                # loaded, so retrying costs one round trip instead of three.
                driver.get("https://services.gst.gov.in/services/searchtp")

                gstin_input = wait.until(EC.presence_of_element_located((By.ID, "for_gstin")))
                gstin_input.clear()
                gstin_input.send_keys(str(gstin))

                # The captcha is not in the DOM until the GSTIN is submitted once.
                if not click_search(driver, wait):
                    raise TimeoutException("SEARCH stayed covered by the loading overlay")
            except SESSION_DEAD:
                self.progress_signal.emit(
                    "Browser window closed or crashed - stopping. "
                    "Everything scraped so far is saved; press Start to resume.")
                break
            except Exception as e:
                # Some selenium errors carry an empty message; the class name is the clue.
                self.progress_signal.emit(
                    f"Error on {gstin}: {type(e).__name__} {str(e).strip() or '(no detail)'}")
                done += 1
                self.count_signal.emit(done, total)
                continue

            previous_src = None
            for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
                if not self._is_running:
                    break
                try:
                    captcha_img = wait.until(EC.visibility_of_element_located((By.ID, "imgCaptcha")))
                    # After a rejection the portal swaps in a new image. Waiting for the src
                    # to change stops us re-reading - and re-banking - the stale one.
                    if previous_src:
                        wait.until(lambda d: d.find_element(
                            By.ID, "imgCaptcha").get_attribute("src") != previous_src)
                        captcha_img = driver.find_element(By.ID, "imgCaptcha")
                    previous_src = captcha_img.get_attribute("src")

                    # Reading a half-loaded img poisons both the guess and the sample.
                    wait.until(lambda d: d.execute_script(
                        "return arguments[0].complete && arguments[0].naturalWidth > 0", captcha_img))
                    png = base64.b64decode(driver.execute_script(CAPTCHA_PNG_JS, captcha_img))

                    guess = solve_captcha(png)

                    captcha_input = wait.until(EC.element_to_be_clickable((By.ID, "fo-captcha")))
                    if not type_captcha(captcha_input, guess):
                        save_sample(png)
                        self.progress_signal.emit(
                            f"{gstin}: captcha box would not accept input ({attempt}/{MAX_CAPTCHA_RETRIES})")
                        continue

                    if not click_search(driver, wait):
                        self.progress_signal.emit(
                            f"{gstin}: SEARCH stayed covered ({attempt}/{MAX_CAPTCHA_RETRIES})")
                        continue

                    # Watch for either outcome instead of timing out on the good one. A
                    # rejection is announced immediately, so this no longer costs a full wait.
                    try:
                        outcome = result_wait.until(search_outcome)
                    except TimeoutException:
                        outcome = None

                    if outcome != "found":
                        save_sample(png)
                        self.progress_signal.emit(
                            f"{gstin}: captcha '{guess}' rejected ({attempt}/{MAX_CAPTCHA_RETRIES})")
                        continue

                    # The portal accepted it, so the guess is ground truth - free training label.
                    save_sample(png, guess)

                    profile = extract_profile(driver)

                    # The panel renders before the goods/services section, so reading
                    # straight away finds an empty page. Give the table a moment to arrive;
                    # a taxpayer that genuinely has none just times out and yields zero rows.
                    try:
                        table_wait.until(EC.presence_of_element_located(HSN_TABLE))
                    except TimeoutException:
                        pass
                    hsn_rows = extract_hsn_rows(driver.find_elements(*HSN_ROWS))

                    # Save immediately to prevent data loss
                    written = append_results(gstin, row.get("Company"), profile, hsn_rows)
                    # Covers a GSTIN listed twice in the same sheet, not just a restart.
                    done_today.add(gstin)

                    name = profile.get("Legal Name of Business", "")
                    self.progress_signal.emit(
                        f"Scraped {gstin} {('- ' + name) if name else ''} "
                        f"({len(hsn_rows)} HSN, {written} row(s), attempt {attempt})")
                    break

                except SESSION_DEAD:
                    self.progress_signal.emit(
                        "Browser window closed or crashed - stopping. "
                        "Everything scraped so far is saved; press Start to resume.")
                    self._is_running = False
                    break

                except Exception as e:
                    # Angular swapping elements mid-attempt is transient; the next pass
                    # re-finds everything. MAX_CAPTCHA_RETRIES bounds it either way.
                    self.progress_signal.emit(
                        f"{gstin}: retrying after {type(e).__name__} ({attempt}/{MAX_CAPTCHA_RETRIES})")
                    continue
            else:
                self.progress_signal.emit(f"Gave up on {gstin} after {MAX_CAPTCHA_RETRIES} captcha attempts")

            done += 1
            self.count_signal.emit(done, total)
            time.sleep(2)
            
        driver.quit()
        self.finished_signal.emit()

    def stop(self):
        self._is_running = False

class GSTScraperUI(QWidget):
    def __init__(self):
        super().__init__()
        self.initUI()
        self.excel_path = None
        self.thread = None

    def initUI(self):
        self.setWindowTitle("GST HSN Extractor")
        self.setGeometry(300, 300, 560, 620)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(18, 18, 18, 18)

        layout.addWidget(self._heading("Step 1 - Prepare your list"))

        self.btn_template = QPushButton("Generate Excel Template")
        self.btn_template.clicked.connect(self.generate_template)
        layout.addWidget(self.btn_template)

        self.btn_upload = QPushButton("Upload Filled Excel")
        self.btn_upload.clicked.connect(self.upload_excel)
        layout.addWidget(self.btn_upload)

        self.lbl_file = QLabel("No file selected")
        self.lbl_file.setWordWrap(True)
        self.set_file_status("No file selected", ok=None)
        layout.addWidget(self.lbl_file)

        layout.addWidget(self._heading("Step 2 - Run"))

        self.chk_headless = QCheckBox("Run in headless mode (hides the browser)")
        layout.addWidget(self.chk_headless)

        row = QHBoxLayout()
        self.btn_start = QPushButton("Start Processing")
        self.btn_start.setObjectName("primary")
        self.btn_start.setEnabled(False)  # nothing to start until a valid sheet is loaded
        self.btn_start.clicked.connect(self.start_scraping)
        row.addWidget(self.btn_start, 2)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.clicked.connect(self.stop_scraping)
        self.btn_stop.setEnabled(False)
        row.addWidget(self.btn_stop, 1)
        layout.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v of %m done")
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        layout.addWidget(self._heading("Step 3 - Collect"))

        self.btn_export = QPushButton("Export Everything to Excel")
        self.btn_export.clicked.connect(self.export_results)
        layout.addWidget(self.btn_export)

        self.log_window = QTextEdit()
        self.log_window.setReadOnly(True)
        # A long run emits up to MAX_CAPTCHA_RETRIES lines per GSTIN. Unbounded, the
        # widget slows to a crawl after a few thousand; old lines are not worth that.
        self.log_window.document().setMaximumBlockCount(500)
        layout.addWidget(self.log_window, 1)

        self.setLayout(layout)

    def _heading(self, text):
        label = QLabel(text)
        label.setObjectName("heading")
        return label

    def generate_template(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Template", "GST_Template.xlsx", "Excel Files (*.xlsx)")
        if path:
            df = pd.DataFrame(columns=["GSTIN", "Company"])
            df.to_excel(path, index=False)
            QMessageBox.information(self, "Success", f"Template saved to {path}")

    def export_results(self):
        """Dump the whole store to a workbook. Everything ever scraped, nothing dropped."""
        if not os.path.exists(RESULTS_FILE):
            QMessageBox.warning(self, "Nothing to export", f"No {RESULTS_FILE} yet - run a scrape first.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self, "Export All Results", "GST_HSN_Export.xlsx", "Excel Files (*.xlsx)")
        if not path:
            return

        rows = export_to_excel(path)
        self.update_log(f"Exported {rows} rows to {path}")
        QMessageBox.information(self, "Exported", f"{rows} rows written to {path}")

    def upload_excel(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Excel File", "", "Excel Files (*.xlsx *.xls)")
        if not path:
            return

        # Read it now rather than at Start, so a bad sheet is obvious while it can still be fixed.
        try:
            df = pd.read_excel(path)
        except Exception as e:
            self.excel_path = None
            self.set_file_status(f"Could not read that file: {e}", ok=False)
            self.btn_start.setEnabled(False)
            return

        if "GSTIN" not in df.columns:
            self.excel_path = None
            self.set_file_status(
                f"No 'GSTIN' column - found: {', '.join(map(str, df.columns)) or 'nothing'}", ok=False)
            self.btn_start.setEnabled(False)
            return

        count = int(df["GSTIN"].notna().sum())
        self.excel_path = path
        self.set_file_status(f"{os.path.basename(path)} - {count} GSTIN(s) ready", ok=True)
        self.btn_start.setEnabled(count > 0)

    def set_file_status(self, message, ok):
        """ok True/False/None -> green, red, or neutral."""
        colours = {True: ("#e6f4ea", "#1e7e34"), False: ("#fdecea", "#b3261e"),
                   None: ("#eceff1", "#5f6368")}
        background, foreground = colours[ok]
        self.lbl_file.setText(message)
        self.lbl_file.setStyleSheet(
            f"padding:9px; border-radius:5px; background:{background}; color:{foreground};")

    def start_scraping(self):
        if not self.excel_path:
            QMessageBox.warning(self, "Error", "Please upload an Excel file first.")
            return

        headless = self.chk_headless.isChecked()
        self.thread = ScraperThread(self.excel_path, headless)
        self.thread.progress_signal.connect(self.update_log)
        self.thread.count_signal.connect(self.update_progress)
        self.thread.finished_signal.connect(self.scraping_finished)

        self.log_window.clear()
        self.update_log("Starting Chrome...")
        self.btn_start.setEnabled(False)
        self.btn_upload.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.thread.start()

    def update_progress(self, done, total):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

    def stop_scraping(self):
        if self.thread:
            self.thread.stop()
            self.update_log("Stopping after current item finishes...")
            self.btn_stop.setEnabled(False)

    def update_log(self, message):
        self.log_window.append(message)

    def scraping_finished(self):
        self.btn_start.setEnabled(True)
        self.btn_upload.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.update_log(f"Processing complete. Stored in '{RESULTS_FILE}' - "
                        f"use Export to pull everything into Excel.")

if __name__ == '__main__':
    app = QApplication(sys.argv)
    ex = GSTScraperUI()
    ex.show()
    sys.exit(app.exec_())