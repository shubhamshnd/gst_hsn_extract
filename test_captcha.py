"""Contract checks for the captcha read/collect path and the results store.
Run: python test_captcha.py"""
import glob
import os
import shutil
import tempfile

import cv2
import numpy as np
import pandas as pd

import app


def test_solve_rejects_garbage():
    blank = cv2.imencode('.png', np.full((40, 150, 3), 255, np.uint8))[1].tobytes()
    assert app.solve_captcha(blank) == ""
    assert app.solve_captcha(b"not a png") == ""


def test_solve_returns_six_digits_or_nothing():
    samples = glob.glob(os.path.join(app.SAMPLE_DIR, "unlabeled", "*.png"))
    assert samples, "no captcha samples to check against"
    for path in samples:
        guess = app.solve_captcha(open(path, "rb").read())
        assert guess == "" or (len(guess) == 6 and guess.isdigit()), f"{path} -> {guess!r}"


def test_save_sample_round_trips():
    tmp = tempfile.mkdtemp()
    original, app.SAMPLE_DIR = app.SAMPLE_DIR, tmp
    try:
        blob = b"\x89PNG-pretend"
        unlabeled = app.save_sample(blob)
        labeled = app.save_sample(blob, "123456")
        assert open(unlabeled, "rb").read() == blob
        assert open(labeled, "rb").read() == blob
        assert os.path.basename(os.path.dirname(unlabeled)) == "unlabeled"
        assert os.path.basename(labeled).startswith("123456_")
    finally:
        app.SAMPLE_DIR = original
        shutil.rmtree(tmp)


class _FakeCell:
    def __init__(self, text):
        self.text = text


class _FakeRow:
    """Stands in for a selenium <tr> so the parsing rule can be checked without a browser."""
    def __init__(self, *texts):
        self._cells = [_FakeCell(t) for t in texts]

    def find_elements(self, by, path):
        return self._cells


def test_extract_hsns_skips_headers_and_placeholders():
    rows = [
        _FakeRow(),                                              # 'Goods | Services' spanning row
        _FakeRow("HSN", "Description", "HSN", "Description"),     # header, and it is <td>
        _FakeRow("", "", "00440177", "PORT SERVICES"),
        _FakeRow("1001", "WHEAT", "00440048", "RENT A CAB"),
        _FakeRow("NA", "", "", ""),
        _FakeRow("1001", "WHEAT AGAIN", "", ""),                  # duplicate
    ]
    assert app.extract_hsns(rows) == ["00440177", "1001", "00440048"]


def test_store_appends_and_exports_everything():
    tmp = tempfile.mkdtemp()
    original, app.RESULTS_FILE = app.RESULTS_FILE, os.path.join(tmp, "results.csv")
    try:
        app.append_result("27AAAAA0000A1Z5", "First Co", "1001, 1002")
        app.append_result("29BBBBB1111B2Z6", "Second Co", "3004")
        app.append_result("27AAAAA0000A1Z5", "First Co", "1001, 1002, 9999")  # rescrape

        out = os.path.join(tmp, "export.xlsx")
        assert app.export_to_excel(out) == 3, "export must dump every row, including rescrapes"

        df = pd.read_excel(out)
        assert list(df.columns) == ["Timestamp", "GSTIN", "Company", "HSNs"]
        assert df["HSNs"].iloc[2] == "1001, 1002, 9999"
        # Header written once, not once per append.
        assert open(app.RESULTS_FILE).read().count("GSTIN") == 1
    finally:
        app.RESULTS_FILE = original
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
