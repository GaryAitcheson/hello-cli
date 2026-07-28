"""A small tkinter front end: pick a file, audit it, repair it.

tkinter ships with the python.org Windows installer, which is the machine this
is aimed at — the one with MT5 on it. Nothing here is required for the CLI, and
the import is guarded so a headless box without Tk can still use everything
else.

Audits of a decade of M1 take tens of seconds, so the work runs on a worker
thread and results come back through a queue that the UI polls. Tk is not
thread-safe; no widget is touched from the worker.
"""

from __future__ import annotations

import os
import queue
import threading
import traceback
from typing import Callable, Optional

try:
    import tkinter as tk
    from tkinter import filedialog, font, messagebox, ttk

    TK_AVAILABLE = True
except ImportError:  # headless Linux, or a python built without Tk
    TK_AVAILABLE = False

from .audit import Thresholds, audit
from .fixes import CleanOptions, CleanStats, clean, describe_plan, suggest_options
from .reader import sniff
from .report import render_text, write_gaps_csv
from .writers import FORMATS, BarWriter, import_hint, open_output

TK_HINT = (
    "This build of Python has no tkinter, so the GUI cannot start.\n"
    "On Debian/Ubuntu: sudo apt install python3-tk\n"
    "On Windows and macOS the python.org installer includes it.\n"
    "The command line works either way: mt5clean audit <file>"
)


class _Task:
    """Runs one job on a worker thread and reports back through a queue."""

    def __init__(self, widget, on_done: Callable, on_error: Callable) -> None:
        self.widget = widget
        self.on_done = on_done
        self.on_error = on_error
        self.queue: queue.Queue = queue.Queue()

    def start(self, work: Callable) -> None:
        def run() -> None:
            try:
                self.queue.put(("ok", work()))
            except Exception as exc:  # surfaced in the UI, not swallowed
                self.queue.put(("error", (exc, traceback.format_exc())))

        threading.Thread(target=run, daemon=True).start()
        self._poll()

    def _poll(self) -> None:
        try:
            status, payload = self.queue.get_nowait()
        except queue.Empty:
            self.widget.after(80, self._poll)
            return
        if status == "ok":
            self.on_done(payload)
        else:
            self.on_error(*payload)


class App:
    def __init__(self, root, initial_file: Optional[str] = None) -> None:
        self.root = root
        self.result = None
        self.path = tk.StringVar(value=initial_file or "")
        self.status = tk.StringVar(value="Choose an MT5 export to begin.")

        root.title("mt5clean - MT5 export auditor")
        root.geometry("980x740")
        root.minsize(760, 560)

        self.mono = font.nametofont("TkFixedFont").copy()
        self.mono.configure(size=10)

        self._build_file_row()
        self._build_summary()
        self._build_report()
        self._build_status()

        if initial_file:
            self._describe_format()

    # ---------------------------------------------------------------- layout

    def _build_file_row(self) -> None:
        frame = ttk.Frame(self.root, padding=(12, 12, 12, 6))
        frame.pack(fill="x")

        ttk.Label(frame, text="MT5 export").grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=self.path)
        entry.grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(frame, text="Browse...", command=self.choose_file).grid(row=0, column=2)

        self.format_label = ttk.Label(frame, text="", foreground="#666")
        self.format_label.grid(row=1, column=1, sticky="w", padx=8, pady=(4, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self.audit_button = ttk.Button(buttons, text="Audit", command=self.run_audit)
        self.audit_button.pack(side="left")
        self.clean_button = ttk.Button(
            buttons, text="Repair...", command=self.open_clean_dialog, state="disabled"
        )
        self.clean_button.pack(side="left", padx=6)
        self.save_button = ttk.Button(
            buttons, text="Save report...", command=self.save_report, state="disabled"
        )
        self.save_button.pack(side="left")
        self.gaps_button = ttk.Button(
            buttons, text="Export gaps CSV...", command=self.save_gaps, state="disabled"
        )
        self.gaps_button.pack(side="left", padx=6)

        frame.columnconfigure(1, weight=1)

    def _build_summary(self) -> None:
        self.summary = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        self.summary.pack(fill="x")
        self.tiles = {}
        for column, (key, label) in enumerate(
            [
                ("bars", "Bars"),
                ("coverage", "Session coverage"),
                ("errors", "Errors"),
                ("warnings", "Warnings"),
                ("gaps", "Gaps in session"),
            ]
        ):
            tile = ttk.Frame(self.summary, relief="groove", padding=8)
            tile.grid(row=0, column=column, sticky="ew", padx=(0, 6))
            ttk.Label(tile, text=label, foreground="#666").pack(anchor="w")
            value = ttk.Label(tile, text="-", font=("TkDefaultFont", 14, "bold"))
            value.pack(anchor="w")
            self.tiles[key] = value
            self.summary.columnconfigure(column, weight=1)

    def _build_report(self) -> None:
        frame = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        frame.pack(fill="both", expand=True)

        self.text = tk.Text(frame, wrap="none", font=self.mono, height=20)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)

        self.text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        self.text.tag_configure("error", foreground="#b00020")
        self.text.tag_configure("warn", foreground="#a15c00")
        self.text.tag_configure("ok", foreground="#1a7f37")
        self._set_text("Pick a file and press Audit.\n\nNothing is ever written unless "
                       "you use Repair, and then only the repairs you tick.")

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, padding=(12, 0, 12, 10))
        bar.pack(fill="x")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=160)
        self.progress.pack(side="right")
        ttk.Label(bar, textvariable=self.status).pack(side="left")

    # --------------------------------------------------------------- helpers

    def _set_text(self, body: str) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", body)
        self._highlight()
        self.text.configure(state="disabled")

    def _highlight(self) -> None:
        """Colour the severity markers and the verdict line."""
        for tag, needle in (("error", "[ERROR]"), ("warn", "[WARN "), ("ok", "VERDICT: clean")):
            start = "1.0"
            while True:
                index = self.text.search(needle, start, stopindex="end")
                if not index:
                    break
                end = f"{index} lineend"
                self.text.tag_add(tag, index, end)
                start = end

    def _busy(self, busy: bool, message: str = "") -> None:
        state = "disabled" if busy else "normal"
        self.audit_button.configure(state=state)
        for button in (self.clean_button, self.save_button, self.gaps_button):
            if busy:
                button.configure(state="disabled")
            elif self.result is not None:
                button.configure(state="normal")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if message:
            self.status.set(message)

    def _failed(self, exc: Exception, detail: str) -> None:
        self._busy(False, f"Failed: {exc}")
        self._set_text(detail)
        messagebox.showerror("mt5clean", str(exc))

    # --------------------------------------------------------------- actions

    def choose_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Choose an MT5 export",
            filetypes=[
                ("MT5 exports", "*.csv *.txt *.tsv"),
                ("CSV files", "*.csv"),
                ("All files", "*.*"),
            ],
        )
        if chosen:
            self.path.set(chosen)
            self.result = None
            for button in (self.clean_button, self.save_button, self.gaps_button):
                button.configure(state="disabled")
            self._describe_format()

    def _describe_format(self) -> None:
        path = self.path.get().strip()
        if not path:
            return
        try:
            dialect = sniff(path)
        except Exception as exc:
            self.format_label.configure(text=f"Could not read the format: {exc}", foreground="#b00020")
            self.status.set("Unrecognised file.")
            return
        # Only the headline facts: the full column map is wide enough to clip,
        # and it is spelled out in the report body anyway.
        headline = dialect.describe().split("\n")[0].rstrip(", ")
        self.format_label.configure(
            text=f"{headline} | {len(dialect.columns)} columns mapped", foreground="#666"
        )
        size = os.path.getsize(path) / 1_048_576
        self.status.set(f"Ready. {size:,.1f} MiB. Press Audit.")

    def run_audit(self) -> None:
        path = self.path.get().strip()
        if not path:
            messagebox.showinfo("mt5clean", "Choose a file first.")
            return
        if not os.path.exists(path):
            messagebox.showerror("mt5clean", f"No such file:\n{path}")
            return

        self._busy(True, "Auditing... large files take a moment.")

        def work():
            result = audit(path, Thresholds())
            return result, render_text(result)

        def done(payload):
            self.result, body = payload
            self._set_text(body)
            self._update_tiles()
            self._busy(False, self._verdict_line())

        _Task(self.root, done, self._failed).start(work)

    def _update_tiles(self) -> None:
        result = self.result
        real_gaps = sum(1 for gap in result.gaps if gap.kind in ("intraday", "holiday"))
        self.tiles["bars"].configure(text=f"{result.bars:,}")
        self.tiles["coverage"].configure(text=f"{result.coverage:.2f}%")
        self.tiles["errors"].configure(
            text=f"{result.error_count:,}",
            foreground="#b00020" if result.error_count else "#1a7f37",
        )
        self.tiles["warnings"].configure(
            text=f"{result.warn_count:,}",
            foreground="#a15c00" if result.warn_count else "#1a7f37",
        )
        self.tiles["gaps"].configure(text=f"{real_gaps:,}")

    def _verdict_line(self) -> str:
        result = self.result
        if result.error_count:
            return f"{result.error_count:,} error-level problem(s). Repair before backtesting."
        if result.warn_count:
            return f"Usable, with {result.warn_count:,} warning(s)."
        return "Clean."

    def save_report(self) -> None:
        if self.result is None:
            return
        target = filedialog.asksaveasfilename(
            title="Save report", defaultextension=".txt",
            initialfile="mt5clean_report.txt",
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not target:
            return
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(render_text(self.result))
        self.status.set(f"Report saved to {target}")

    def save_gaps(self) -> None:
        if self.result is None:
            return
        target = filedialog.asksaveasfilename(
            title="Export gaps", defaultextension=".csv",
            initialfile="gaps.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        count = write_gaps_csv(self.result, target)
        self.status.set(f"{count:,} gaps written to {target}")

    def open_clean_dialog(self) -> None:
        if self.result is not None:
            CleanDialog(self)


class CleanDialog:
    """Repair options, pre-ticked from what the audit actually found."""

    def __init__(self, app: App) -> None:
        self.app = app
        self.result = app.result
        suggested = suggest_options(self.result)

        self.window = tk.Toplevel(app.root)
        self.window.title("Repair")
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.resizable(False, False)

        body = ttk.Frame(self.window, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(
            body,
            text="Ticked automatically from this file's findings. Untick anything\n"
                 "you would rather leave alone - nothing is applied to the original.",
            foreground="#666",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.duplicates = tk.StringVar(value=suggested.duplicates)
        self.sort = tk.BooleanVar(value=suggested.sort)
        self.fix_ohlc = tk.BooleanVar(value=suggested.fix_ohlc)
        self.drop_invalid = tk.BooleanVar(value=False)
        self.off_grid = tk.StringVar(value=suggested.off_grid)
        self.fill_gaps = tk.BooleanVar(value=suggested.fill_gaps)
        self.max_fill = tk.IntVar(value=suggested.max_fill)
        self.drop_spikes = tk.BooleanVar(value=suggested.drop_spikes)
        self.session_only = tk.BooleanVar(value=False)
        self.out_format = tk.StringVar(value="mt5")

        row = 1
        row = self._combo(body, row, "Duplicate timestamps", self.duplicates,
                          ("none", "first", "last"), self._count_hint("duplicate"))
        row = self._check(body, row, "Sort by timestamp", self.sort,
                          self._count_hint("out_of_order"))
        row = self._check(body, row, "Repair OHLC (clamp high/low)", self.fix_ohlc,
                          self._count_hint("ohlc_invalid"))
        row = self._check(body, row, "Drop invalid bars instead of repairing", self.drop_invalid, "")
        row = self._combo(body, row, "Off-grid timestamps", self.off_grid,
                          ("keep", "snap", "drop"), self._count_hint("off_grid"))
        row = self._check(body, row, "Fill short in-session gaps", self.fill_gaps,
                          self._count_hint("gap_intraday"))

        ttk.Label(body, text="Longest gap to fill (minutes)").grid(row=row, column=0, sticky="w")
        ttk.Spinbox(body, from_=1, to=1440, textvariable=self.max_fill, width=8).grid(
            row=row, column=1, sticky="w", padx=8
        )
        row += 1

        row = self._check(body, row, "Drop price spikes", self.drop_spikes,
                          self._count_hint("price_spike"))
        row = self._check(body, row, "Drop bars outside the trading session", self.session_only, "")

        ttk.Separator(body, orient="horizontal").grid(
            row=row, column=0, columnspan=3, sticky="ew", pady=10
        )
        row += 1

        ttk.Label(body, text="Output format").grid(row=row, column=0, sticky="w")
        ttk.Combobox(body, textvariable=self.out_format, values=list(FORMATS),
                     state="readonly", width=10).grid(row=row, column=1, sticky="w", padx=8)
        ttk.Label(body, text="mt5 = identical to an MT5 export (import it the same way)",
                  foreground="#666").grid(
            row=row, column=2, sticky="w"
        )
        row += 1

        actions = ttk.Frame(body)
        actions.grid(row=row, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(actions, text="Cancel", command=self.window.destroy).pack(side="right")
        ttk.Button(actions, text="Write repaired file...", command=self.run).pack(
            side="right", padx=6
        )

    def _count_hint(self, code: str) -> str:
        if code == "duplicate":
            total = (self.result.counts.get("duplicate_identical", 0)
                     + self.result.counts.get("duplicate_conflicting", 0))
        else:
            total = self.result.counts.get(code, 0)
        return f"{total:,} found" if total else "none found"

    def _check(self, parent, row: int, label: str, variable, hint: str) -> int:
        ttk.Checkbutton(parent, text=label, variable=variable).grid(
            row=row, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(parent, text=hint, foreground="#666").grid(row=row, column=2, sticky="w")
        return row + 1

    def _combo(self, parent, row: int, label: str, variable, values, hint: str) -> int:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w")
        ttk.Combobox(parent, textvariable=variable, values=list(values),
                     state="readonly", width=10).grid(row=row, column=1, sticky="w", padx=8)
        ttk.Label(parent, text=hint, foreground="#666").grid(row=row, column=2, sticky="w")
        return row + 1

    def _options(self) -> CleanOptions:
        return CleanOptions(
            duplicates=self.duplicates.get(),
            sort=self.sort.get(),
            fix_ohlc=self.fix_ohlc.get(),
            drop_invalid=self.drop_invalid.get(),
            off_grid=self.off_grid.get(),
            fill_gaps=self.fill_gaps.get(),
            max_fill=self.max_fill.get(),
            drop_spikes=self.drop_spikes.get(),
            spike_limit=self.result.median_range * self.result.thresholds.spike_range_mult,
            session_only=self.session_only.get(),
        )

    def run(self) -> None:
        source = self.app.path.get().strip()
        stem = os.path.splitext(os.path.basename(source))[0]
        target = filedialog.asksaveasfilename(
            parent=self.window,
            title="Write repaired file",
            defaultextension=".csv",
            initialfile=f"{stem}_clean.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        if os.path.abspath(target) == os.path.abspath(source):
            messagebox.showerror(
                "mt5clean",
                "Refusing to overwrite the original export.\nChoose a different name.",
                parent=self.window,
            )
            return

        options = self._options()
        fmt = self.out_format.get()
        digits = self.result.digits
        dialect = self.result.dialect
        mask = self.result.mask
        stats = CleanStats()
        self.window.destroy()
        self.app._busy(True, "Writing repaired file...")

        def work():
            with open_output(target) as handle:
                writer = BarWriter(handle, fmt, digits)
                for bar in clean(source, dialect, options, mask, stats):
                    writer.write(bar)
            return stats

        def done(finished: CleanStats):
            lines = [
                f"Wrote {finished.written:,} bars to {target}",
                "",
                "What was done:",
            ]
            lines += [f"  - {step}" for step in describe_plan(options)]
            lines += ["", "Counts:"]
            lines += [f"  {line}" for line in finished.summary()]
            lines += ["", *import_hint(fmt, digits)]
            self.app._set_text("\n".join(lines))
            self.app._busy(False, f"Repaired file written to {target}")

        _Task(self.app.root, done, self.app._failed).start(work)


def run_gui(initial_file: Optional[str] = None) -> int:
    """Open the window. Returns a process exit code."""
    if not TK_AVAILABLE:
        print(TK_HINT)
        return 2
    root = tk.Tk()
    App(root, initial_file)
    root.mainloop()
    return 0
