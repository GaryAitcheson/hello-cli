"""mt5clean — check and repair MetaTrader 5 data exports.

Typical use from Python::

    from mt5clean import read_file, analyze

    records, bad_rows, dialect = read_file("EURUSD_M1.csv")
    result = analyze(records, dialect.kind)
    print(result.session_coverage, len(result.gaps))
"""

__version__ = "0.1.0"

from .analyze import Analysis, Gap, Issue, analyze, infer_timeframe
from .clean import CleanResult, clean
from .formats import Bar, Dialect, ParseError, Tick, read_file, sniff, write_file

__all__ = [
    "Analysis",
    "Bar",
    "CleanResult",
    "Dialect",
    "Gap",
    "Issue",
    "ParseError",
    "Tick",
    "analyze",
    "clean",
    "infer_timeframe",
    "read_file",
    "sniff",
    "write_file",
    "__version__",
]
