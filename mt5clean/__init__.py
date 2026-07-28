"""mt5clean - audit and repair MetaTrader 5 exports (bars and ticks).

Typical use from Python::

    from mt5clean import audit, render_text

    result = audit("EURUSD_M1.csv")
    print(render_text(result))
    print(result.coverage, "% of session minutes present")

Tick exports are read-only for now::

    from mt5clean import audit_ticks, render_tick_text

    result = audit_ticks("EURUSD_ticks.csv")
    print(render_tick_text(result))

Everything is stdlib-only, so it runs wherever MT5 put the file.
"""

from .audit import AuditResult, Thresholds, audit
from .fixes import CleanOptions, CleanStats, clean
from .model import Bar, Gap, Issue, Severity, Tick
from .reader import Dialect, SniffError, read_bars, read_ticks, sniff
from .report import render_json, render_text, write_gaps_csv
from .sessions import SessionMask
from .tick_audit import TickAuditResult, TickThresholds, audit_ticks
from .tick_report import render_json as render_tick_json
from .tick_report import render_text as render_tick_text
from .timeframe import parse_timeframe
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
    "Tick",
    "TickAuditResult",
    "TickThresholds",
    "audit",
    "audit_ticks",
    "clean",
    "parse_timeframe",
    "read_bars",
    "read_ticks",
    "render_json",
    "render_text",
    "render_tick_json",
    "render_tick_text",
    "sniff",
    "write_gaps_csv",
    "__version__",
]
