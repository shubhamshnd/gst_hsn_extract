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
from selenium.common.exceptions import TimeoutException
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
MAX_CAPTCHA_RETRIES = 8

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

# ponytail: ddddocr's generic model scores ~1/6 exact on real GST captchas. The retry
# loop absorbs that. Swap this for a model trained on captcha_samples/labeled once
# enough of those have piled up.
_ocr = ddddocr.DdddOcr(show_ad=False)


def solve_captcha(png_bytes):
    """Read a GST captcha. Returns 6 digits, or '' if the read is obviously bad."""
    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return ""

    # Inpaint out the red strikethrough line; measurably better than feeding the raw image.
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    red_mask = (cv2.inRange(hsv, np.array([0, 50, 50]), np.array([10, 255, 255]))
                + cv2.inRange(hsv, np.array([170, 50, 50]), np.array([180, 255, 255])))
    cleaned = cv2.inpaint(img, red_mask, 3, cv2.INPAINT_TELEA)

    text = _ocr.classification(cv2.imencode('.png', cleaned)[1].tobytes())
    digits = "".join(c for c in text if c.isdigit())
    # The captcha is always 6 digits, so anything else is a misread - don't spend a submit on it.
    return digits if len(digits) == 6 else ""


RESULT_COLUMNS = ["Timestamp", "GSTIN", "Company", "HSNs"]

# The results table is [Goods HSN | Goods Desc | Services HSN | Services Desc], and its
# header row is <td> inside <tbody>, so position alone cannot tell data from heading.
HSN_COLUMNS = (0, 2)


def extract_hsns(rows):
    """Pull HSN/SAC codes out of the results table, goods and services alike.

    Codes are numeric, which is what separates them from the 'HSN' header cell and
    from 'NA' placeholders. Order is preserved and duplicates dropped.
    """
    # ponytail: goods and services are merged into one list, matching the single HSNs
    # column. Split into two columns if the distinction ever matters.
    found = []
    for row in rows:
        cells = row.find_elements(By.XPATH, './td')
        for index in HSN_COLUMNS:
            if index < len(cells):
                text = cells[index].text.strip()
                if text.isdigit() and text not in found:
                    found.append(text)
    return found


def append_result(gstin, company, hsns):
    """One scraped row onto the end of the store. Written immediately, not batched."""
    # A store written by an older version has a different header. Appending under it makes
    # a CSV pandas cannot parse, so roll it aside instead of corrupting either one.
    if os.path.exists(RESULTS_FILE):
        with open(RESULTS_FILE) as f:
            header = f.readline().strip().split(",")
        if header != RESULT_COLUMNS:
            os.rename(RESULTS_FILE, f"{RESULTS_FILE}.{time.strftime('%Y%m%d%H%M%S')}.old")

    row = pd.DataFrame({
        "Timestamp": [time.strftime("%Y-%m-%d %H:%M:%S")],
        "GSTIN": [gstin],
        "Company": [company],
        "HSNs": [hsns],
    })
    row.to_csv(RESULTS_FILE, mode='a', header=not os.path.exists(RESULTS_FILE), index=False)


def export_to_excel(path):
    """Dump the whole store to a workbook. Returns how many rows went out."""
    df = pd.read_csv(RESULTS_FILE)
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
        
        total = int(df["GSTIN"].notna().sum()) if "GSTIN" in df.columns else 0
        done = 0
        self.count_signal.emit(0, total)

        for index, row in df.iterrows():
            if not self._is_running:
                self.progress_signal.emit("Process stopped by user.")
                break

            gstin = row.get("GSTIN")
            if pd.isna(gstin):
                continue

            for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
                if not self._is_running:
                    break
                try:
                    # A fresh page load is also a fresh captcha.
                    driver.get("https://services.gst.gov.in/services/searchtp")

                    gstin_input = wait.until(EC.presence_of_element_located((By.ID, "for_gstin")))
                    gstin_input.clear()
                    gstin_input.send_keys(str(gstin))

                    # The captcha is not in the DOM until the GSTIN is submitted once.
                    wait.until(EC.invisibility_of_element_located(DIMMER))
                    driver.find_element(By.ID, "lotsearch").click()
                    captcha_img = wait.until(EC.visibility_of_element_located((By.ID, "imgCaptcha")))
                    # Reading a half-loaded img poisons both the guess and the sample.
                    wait.until(lambda d: d.execute_script(
                        "return arguments[0].complete && arguments[0].naturalWidth > 0", captcha_img))
                    png = base64.b64decode(driver.execute_script(CAPTCHA_PNG_JS, captcha_img))

                    guess = solve_captcha(png)
                    if not guess:
                        save_sample(png)
                        self.progress_signal.emit(f"{gstin}: unreadable captcha ({attempt}/{MAX_CAPTCHA_RETRIES})")
                        continue

                    captcha_input = driver.find_element(By.ID, "fo-captcha")
                    captcha_input.clear()
                    captcha_input.send_keys(guess)
                    wait.until(EC.invisibility_of_element_located(DIMMER))
                    driver.find_element(By.ID, "lotsearch").click()

                    # No results table at all means the captcha was refused - the portal
                    # re-renders the form instead. A valid GSTIN with no codes still gets a
                    # table, so this distinguishes the two cases rather than guessing.
                    try:
                        rows = result_wait.until(EC.presence_of_all_elements_located(
                            (By.XPATH, "//table[contains(@class, 'table-bordered')]//tr")
                        ))
                    except TimeoutException:
                        save_sample(png)
                        self.progress_signal.emit(f"{gstin}: captcha '{guess}' rejected ({attempt}/{MAX_CAPTCHA_RETRIES})")
                        continue

                    # The portal accepted it, so the guess is ground truth - free training label.
                    save_sample(png, guess)

                    hsn_string = ", ".join(extract_hsns(rows))

                    # Save immediately to prevent data loss
                    append_result(gstin, row.get("Company"), hsn_string)

                    self.progress_signal.emit(f"Successfully scraped: {gstin} (attempt {attempt})")
                    break

                except Exception as e:
                    self.progress_signal.emit(f"Error on {gstin}: {str(e)}")
                    break
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