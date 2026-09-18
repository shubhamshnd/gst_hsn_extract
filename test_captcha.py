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


def _is_submittable(guess):
    """The portal only accepts six digits, so that is all solve_captcha may ever return."""
    return len(guess) == 6 and guess.isdigit()


def test_solve_always_returns_something_submittable():
    # Even unreadable input must produce a guess: a rejected guess is how the loop gets a
    # fresh captcha, and returning nothing would strand it with no way to move on.
    blank = cv2.imencode('.png', np.full((40, 150, 3), 255, np.uint8))[1].tobytes()
    assert _is_submittable(app.solve_captcha(blank))
    assert _is_submittable(app.solve_captcha(b"not a png"))


def test_solve_pads_real_samples_to_six_digits():
    samples = glob.glob(os.path.join(app.SAMPLE_DIR, "unlabeled", "*.png"))
    assert samples, "no captcha samples to check against"
    for path in samples:
        guess = app.solve_captcha(open(path, "rb").read())
        assert _is_submittable(guess), f"{path} -> {guess!r}"


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


def test_extract_hsn_rows_keeps_descriptions_and_skips_headers():
    rows = [
        _FakeRow(),                                              # 'Goods | Services' spanning row
        _FakeRow("HSN", "Description", "HSN", "Description"),     # header, and it is <td>
        _FakeRow("", "", "00440177", "PORT SERVICES"),
        _FakeRow("1001", "WHEAT", "00440048", "RENT A CAB"),
        _FakeRow("NA", "", "", ""),
        _FakeRow("1001", "WHEAT AGAIN", "", ""),                  # duplicate
    ]
    assert app.extract_hsn_rows(rows) == [
        ("Services", "00440177", "PORT SERVICES"),
        ("Goods", "1001", "WHEAT"),
        ("Services", "00440048", "RENT A CAB"),
    ]


class _FakeDriver:
    def __init__(self, panels=(), errors=()):
        self._found = {app.PROFILE_PANEL: list(panels), app.CAPTCHA_ERROR: list(errors)}

    def find_elements(self, by, value):
        return self._found.get((by, value), [])


class _ErrEl:
    def __init__(self, text, displayed=True):
        self.text = text
        self._displayed = displayed

    def is_displayed(self):
        return self._displayed


def test_search_outcome_tells_success_from_refusal():
    assert app.search_outcome(_FakeDriver()) is None, "neither yet -> keep polling"
    assert app.search_outcome(_FakeDriver(panels=[object()])) == "found"
    assert app.search_outcome(
        _FakeDriver(errors=[_ErrEl("Enter valid letters shown in the image below")])) == "captcha"
    # An empty or hidden .err sits in the DOM before submit; it must not read as a refusal.
    assert app.search_outcome(_FakeDriver(errors=[_ErrEl("")])) is None
    assert app.search_outcome(_FakeDriver(errors=[_ErrEl("boom", displayed=False)])) is None


class _FlakyInput:
    """An input that swallows the first N send_keys, the way Angular does mid-render."""
    def __init__(self, swallow):
        self.swallow = swallow
        self.value = ""

    def clear(self):
        self.value = ""

    def send_keys(self, text):
        if self.swallow > 0:
            self.swallow -= 1
            return
        self.value = text

    def get_attribute(self, name):
        return self.value


def test_type_captcha_retries_until_the_value_sticks():
    good = _FlakyInput(swallow=0)
    assert app.type_captcha(good, "123456") is True
    assert good.value == "123456"

    flaky = _FlakyInput(swallow=2)
    assert app.type_captcha(flaky, "123456") is True, "must retry, not give up on one drop"
    assert flaky.value == "123456"

    dead = _FlakyInput(swallow=99)
    assert app.type_captcha(dead, "123456") is False, "must report failure, not submit blank"


def test_store_writes_one_row_per_hsn_and_exports_everything():
    profile = {
        "Legal Name of Business": "JSW DHARAMTAR PORT PRIVATE LIMITED",
        "Taxpayer Type": "Regular",
        "Some Future Field": "kept anyway",     # not in PROFILE_FIELDS
    }
    tmp = tempfile.mkdtemp()
    original, app.RESULTS_FILE = app.RESULTS_FILE, os.path.join(tmp, "results.csv")
    try:
        assert app.append_results("27AACCJ9361Q1ZS", "JSW DPPL", profile, [
            ("Services", "00440177", "PORT SERVICES"),
            ("Services", "00440048", "RENT A CAB OPERATORS"),
        ]) == 2
        # A taxpayer with no codes still gets a row, so the profile is not lost.
        assert app.append_results("29BBBBB1111B2Z6", "No Codes Co", profile, []) == 1

        out = os.path.join(tmp, "export.xlsx")
        assert app.export_to_excel(out) == 3

        # dtype=str on read too: pandas re-infers 00440177 as a number on the way back in,
        # even though the cell itself is stored as text.
        df = pd.read_excel(out, dtype=str)
        assert list(df.columns) == app.RESULT_COLUMNS
        # Leading zeros are part of the SAC code and must survive the round trip.
        assert list(df["HSN"][:2]) == ["00440177", "00440048"]
        assert df["Description"].iloc[0] == "PORT SERVICES"
        assert df["Legal Name of Business"].iloc[0] == "JSW DHARAMTAR PORT PRIVATE LIMITED"
        assert "Some Future Field: kept anyway" in df["Other Details"].iloc[0]
        assert pd.isna(df["HSN"].iloc[2])          # the no-codes taxpayer
        # Header written once, not once per append.
        assert open(app.RESULTS_FILE).read().count("Timestamp") == 1
    finally:
        app.RESULTS_FILE = original
        shutil.rmtree(tmp)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
