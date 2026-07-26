"""mt5clean - audit and repair MetaTrader 5 M1 bar exports.

Typical use from Python::

    from mt5clean import audit, render_text

    result = audit("EURUSD_M1.csv")
    print(render_text(result))
    print(result.coverage, "% of session minutes present")

Everything is stdlib-only, so it runs wherever MT5 put the file.
"""

from .audit import AuditResult, Thresholds, audit
from .fixes import CleanOptions, CleanStats, clean
from .model import Bar, Gap, Issue, Severity
from .reader import Dialect, SniffError, read_bars, sniff
from .report import render_json, render_text, write_gaps_csv
from .sessions import SessionMask
from .writers import BarWriter

__version__ = "0.1.0"

__all__ = [
    "AuditResult",
    "Bar",
    "BarWriter",
    "CleanOptions",
    "CleanStats",
    "Dialect",
    "Gap",
    "Issue",
    "SessionMask",
    "Severity",
    "SniffError",
    "Thresholds",
    "audit",
    "clean",
    "read_bars",
    "render_json",
    "render_text",
    "sniff",
    "write_gaps_csv",
    "__version__",
]
