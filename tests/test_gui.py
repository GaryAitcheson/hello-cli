"""Tests for the tkinter front end.

These drive the real widgets rather than mocking them, because the bugs that
actually bite in a Tk app are wiring bugs: a button that never re-enables, a
worker thread whose result never reaches the UI, a dialog that reads the wrong
variable. Mocks would happily pass through all of those.

Skipped when there is no Tk or no display, so a headless box still runs the
rest of the suite. CI runs this job under xvfb so it does not silently skip
everywhere.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mt5clean.gui import TK_AVAILABLE  # noqa: E402

from make_sample import generate, inject, write  # noqa: E402

def _tk_usable() -> bool:
    """Whether Tk can actually start, not merely whether the module imports.

    These are different things, and the difference is not academic: the hosted
    Windows Python images have shipped a tkinter that imports cleanly while its
    Tcl library files are missing, so `Tk()` dies with "Can't find a usable
    tk.tcl". Guarding on the import alone turns that into two errors in a
    fixture rather than an honest skip.
    """
    if not TK_AVAILABLE:
        return False
    if not (os.environ.get("DISPLAY") or sys.platform in ("win32", "darwin")):
        return False
    try:
        import tkinter as tk

        root = tk.Tk()
        root.destroy()
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _tk_usable(),
    reason="needs tkinter and a working Tcl/Tk install with a display",
)

PUMP_TIMEOUT = 120


@pytest.fixture(scope="module")
def dirty_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("gui") / "dirty.csv"
    rows, _notes = inject(generate(datetime(2024, 1, 1), weeks=8))
    write(rows, str(path), "mt5")
    return str(path)


@pytest.fixture
def app(dirty_file):
    import tkinter as tk

    from mt5clean.gui import App

    root = tk.Tk()
    instance = App(root, dirty_file)
    yield instance
    try:
        root.destroy()
    except tk.TclError:
        pass


def pump_until(root, predicate, timeout: float = PUMP_TIMEOUT):
    """Run the Tk event loop until `predicate` holds, so _Task can poll."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_window_builds_and_reads_the_format(app, dirty_file):
    assert "delimiter=tab" in app.format_label.cget("text")
    assert "columns mapped" in app.format_label.cget("text")
    # The headline must fit on one line - it used to be clipped mid-sentence.
    assert "\n" not in app.format_label.cget("text")


def test_audit_runs_on_a_worker_and_reaches_the_widgets(app):
    app.run_audit()
    assert pump_until(app.root, lambda: app.result is not None), "audit never finished"

    assert app.result.bars > 0
    assert "VERDICT" in app.text.get("1.0", "end")
    assert app.tiles["errors"].cget("text") == f"{app.result.error_count:,}"
    assert app.tiles["coverage"].cget("text").endswith("%")


def test_action_buttons_enable_only_after_an_audit(app):
    # ttk state is a flag set, not a string: cget("state") returns a Tcl object.
    assert not app.clean_button.instate(["!disabled"])
    app.run_audit()
    assert pump_until(app.root, lambda: app.result is not None)
    for button in (app.clean_button, app.save_button, app.gaps_button):
        assert button.instate(["!disabled"])


def test_repair_dialog_is_preticked_from_the_findings(app):
    from mt5clean.fixes import suggest_options
    from mt5clean.gui import CleanDialog

    app.run_audit()
    assert pump_until(app.root, lambda: app.result is not None)

    dialog = CleanDialog(app)
    app.root.update()
    suggested = suggest_options(app.result)

    assert dialog.duplicates.get() == suggested.duplicates == "last"
    assert dialog.sort.get() is suggested.sort is True
    assert dialog.fix_ohlc.get() is suggested.fix_ohlc is True
    assert dialog.off_grid.get() == suggested.off_grid == "snap"
    assert dialog.drop_spikes.get() is suggested.drop_spikes is True

    # And the dialog must hand those choices back unchanged.
    options = dialog._options()
    assert options.duplicates == "last"
    assert options.off_grid == "snap"
    assert options.spike_limit > 0
    dialog.window.destroy()


def test_repair_writes_a_file_that_reparses(app, tmp_path, monkeypatch):
    from mt5clean import audit, sniff
    from mt5clean.gui import CleanDialog

    app.run_audit()
    assert pump_until(app.root, lambda: app.result is not None)
    assert app.result.error_count > 0

    target = tmp_path / "repaired.csv"
    dialog = CleanDialog(app)
    monkeypatch.setattr(
        "mt5clean.gui.filedialog.asksaveasfilename", lambda **kwargs: str(target)
    )
    dialog.run()
    assert pump_until(app.root, lambda: target.exists() and "Wrote" in app.text.get("1.0", "end"))

    # GUI default output is now "mt5" (tab separated), matching a real MT5 export.
    assert sniff(str(target)).delimiter == "\t"
    assert audit(str(target)).error_count == 0


def test_repair_refuses_to_overwrite_the_original(app, monkeypatch):
    from mt5clean.gui import CleanDialog

    app.run_audit()
    assert pump_until(app.root, lambda: app.result is not None)

    source = app.path.get()
    before = os.path.getmtime(source)
    dialog = CleanDialog(app)
    monkeypatch.setattr("mt5clean.gui.filedialog.asksaveasfilename", lambda **kwargs: source)

    errors = []
    monkeypatch.setattr(
        "mt5clean.gui.messagebox.showerror", lambda *a, **k: errors.append(a)
    )
    dialog.run()

    assert errors, "should have refused to overwrite the source export"
    assert os.path.getmtime(source) == before
    dialog.window.destroy()


def test_missing_file_is_reported_not_crashed(app, monkeypatch):
    shown = []
    monkeypatch.setattr("mt5clean.gui.messagebox.showerror", lambda *a, **k: shown.append(a))
    app.path.set("/nonexistent/nope.csv")
    app.run_audit()
    assert shown
    assert app.result is None
