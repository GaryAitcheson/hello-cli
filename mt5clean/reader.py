"""Format sniffing and streaming parser for MT5 bar exports.

MT5 has never settled on one export shape, so this module figures the file out
rather than demanding a fixed layout. Handled variants:

    <DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>
    Date,Time,Open,High,Low,Close,Volume
    2024.01.02,00:00,1.10432,1.10445,1.10430,1.10441,52        (headerless, MT4 style)
    2024-01-02 00:00:00;1,10432;...                            (EU locale)

Parsing is a generator so a 4M-row M1 file never has to fit in memory.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

from .model import EPOCH_ORD, Bar, Issue, Severity, Tick

DELIMITERS = ("\t", ";", ",", "|", " ")

# Normalised header name -> canonical field.
_HEADER_ALIASES = {
    "date": "date",
    "time": "time",
    "datetime": "datetime",
    "timestamp": "datetime",
    "date_time": "datetime",
    "open": "open",
    "o": "open",
    "high": "high",
    "h": "high",
    "low": "low",
    "l": "low",
    "close": "close",
    "c": "close",
    "tickvol": "tick_volume",
    "tick_volume": "tick_volume",
    "tickvolume": "tick_volume",
    "volume": "tick_volume",
    "vol": "real_volume",
    "real_volume": "real_volume",
    "realvolume": "real_volume",
    "spread": "spread",
    "bid": "bid",
    "ask": "ask",
    "last": "last",
    "flags": "flags",
    "flag": "flags",
    "volume_real": "real_volume",
}

_NUMERIC_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?$")


class SniffError(ValueError):
    """Raised when the file cannot be recognised as an MT5 bar export."""


DATE_ORDERS = {"ymd": "YYYY.MM.DD", "dmy": "DD.MM.YYYY", "mdy": "MM.DD.YYYY"}


@dataclass
class Dialect:
    """Everything the parser needs to read a specific file quickly."""

    delimiter: str
    has_header: bool
    columns: Dict[str, int]
    kind: str = "bars"
    """Either "bars" or "ticks"."""

    date_order: str = "ymd"
    date_order_ambiguous: bool = False
    """True when the sample never contained a day above 12, so DD/MM could
    not be told apart from MM/DD. Pass --date-order to settle it."""

    decimal_comma: bool = False
    encoding: str = "utf-8-sig"
    header_line: Optional[str] = None
    sample_rows: List[List[str]] = field(default_factory=list)

    def describe(self) -> str:
        pretty = {"\t": "tab", " ": "space", ";": "semicolon", ",": "comma", "|": "pipe"}
        cols = ", ".join(f"{name}={idx}" for name, idx in sorted(self.columns.items(), key=lambda kv: kv[1]))
        order = DATE_ORDERS[self.date_order]
        if self.date_order_ambiguous:
            order += " (assumed - file never shows a day above 12)"
        return (
            f"delimiter={pretty.get(self.delimiter, repr(self.delimiter))}, "
            f"header={'yes' if self.has_header else 'no'}, "
            f"date_order={order}, "
            f"decimal={'comma' if self.decimal_comma else 'point'}\n"
            f"  columns: {cols}"
        )


def _normalise_header(cell: str) -> str:
    cell = cell.strip().strip("<>").strip().lower()
    cell = re.sub(r"[^a-z_]+", "_", cell).strip("_")
    return cell


def _looks_like_header(row: List[str]) -> bool:
    if not row:
        return False
    joined = "".join(row)
    if "<" in joined and ">" in joined:
        return True
    alpha = sum(1 for cell in row if re.search(r"[A-Za-z]", cell))
    return alpha >= max(2, len(row) // 2)


def _split(line: str, delimiter: str) -> List[str]:
    if delimiter == " ":
        return line.split()
    return next(csv.reader([line], delimiter=delimiter))


def _score_delimiter(lines: List[str], delimiter: str) -> Tuple[int, int]:
    """Return (consistency score, field count) for a candidate delimiter."""
    counts = [len(_split(line, delimiter)) for line in lines]
    counts = [c for c in counts if c > 1]
    if not counts:
        return (0, 0)
    modal = max(set(counts), key=counts.count)
    if modal < 4:  # date + time + at least two price columns
        return (0, modal)
    return (counts.count(modal), modal)


def _read_sample(path: str, limit: int = 60) -> Tuple[List[str], str]:
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            with io.open(path, "r", encoding=encoding, newline="") as fh:
                lines = []
                for line in fh:
                    line = line.rstrip("\r\n")
                    if line.strip():
                        lines.append(line)
                    if len(lines) >= limit:
                        break
            if lines:
                return lines, encoding
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise SniffError(f"{path}: could not decode as text (utf-8, utf-16 or latin-1)")


def _map_positional(n_fields: int, first_row: List[str]) -> Dict[str, int]:
    """Best-effort column map for a headerless file, based on field count."""
    combined_stamp = " " in first_row[0].strip() or "T" in first_row[0].strip()[10:11]
    base = 1 if combined_stamp else 2
    keys = ["datetime"] if combined_stamp else ["date", "time"]

    remaining = n_fields - base
    if remaining < 4:
        raise SniffError(
            f"headerless file has {n_fields} columns; expected at least "
            f"{base + 4} (date/time + OHLC)"
        )
    keys += ["open", "high", "low", "close"]
    tail = ["tick_volume", "real_volume", "spread"]
    keys += tail[: max(0, remaining - 4)]
    return {name: idx for idx, name in enumerate(keys)}


def _detect_date_order(rows: List[List[str]], date_idx: int) -> Tuple[str, bool]:
    """Return (order, ambiguous) for the date column.

    MT5's own exports are year-first and unambiguous. A year-last file has been
    through a spreadsheet or a European locale somewhere, and DD.MM vs MM.DD can
    only be settled by seeing a component above 12.
    """
    saw_year_last = False
    for row in rows:
        if date_idx >= len(row):
            continue
        parts = re.split(r"[./\-]", row[date_idx].strip().split(" ")[0])
        if len(parts) != 3:
            continue
        try:
            first, middle, _last = (int(p) for p in parts)
        except ValueError:
            continue
        if len(parts[0]) == 4 or first > 31:
            return "ymd", False
        saw_year_last = True
        if first > 12:
            return "dmy", False
        if middle > 12:
            return "mdy", False
    if saw_year_last:
        # Day-first is the safer read: MT5 never writes MM.DD.YYYY, and the
        # tools that produce year-last dates are usually EU-locale ones.
        return "dmy", True
    return "ymd", False


def sniff(
    path: str,
    delimiter: Optional[str] = None,
    date_order: Optional[str] = None,
) -> Dialect:
    """Inspect the head of `path` and work out how to parse it."""
    lines, encoding = _read_sample(path)
    if not lines:
        raise SniffError(f"{path}: file is empty")

    if delimiter:
        chosen = delimiter
    else:
        scored = sorted(
            ((_score_delimiter(lines, d), d) for d in DELIMITERS),
            key=lambda item: (item[0][0], item[0][1]),
            reverse=True,
        )
        (score, _fields), chosen = scored[0]
        if score == 0:
            raise SniffError(
                f"{path}: no delimiter produced consistent rows of 5+ columns. "
                f"First line was: {lines[0][:120]!r}"
            )

    rows = [_split(line, chosen) for line in lines]
    has_header = _looks_like_header(rows[0])
    header_line = lines[0] if has_header else None
    data_rows = rows[1:] if has_header else rows
    if not data_rows:
        raise SniffError(f"{path}: header found but no data rows")

    kind = "bars"
    if has_header:
        columns: Dict[str, int] = {}
        for idx, cell in enumerate(rows[0]):
            canonical = _HEADER_ALIASES.get(_normalise_header(cell))
            if canonical and canonical not in columns:
                columns[canonical] = idx
        missing = {"open", "high", "low", "close"} - set(columns)
        has_stamp = bool({"date", "datetime"} & set(columns))
        if {"bid", "ask"} & set(columns) and has_stamp:
            # A tick export: bid/ask instead of OHLC, and no fixed grid.
            kind = "ticks"
        elif missing or not has_stamp:
            # Header exists but is unrecognised - fall back to position.
            columns = _map_positional(len(data_rows[0]), data_rows[0])
    else:
        # Headerless tick exports are not a thing MT5 produces, so position
        # mapping only ever has to cover bars.
        columns = _map_positional(len(data_rows[0]), data_rows[0])

    date_idx = columns.get("date", columns.get("datetime", 0))
    if date_order:
        if date_order not in DATE_ORDERS:
            raise SniffError(f"unknown date order {date_order!r}; choose from {', '.join(DATE_ORDERS)}")
        detected, ambiguous = date_order, False
    else:
        detected, ambiguous = _detect_date_order(data_rows, date_idx)

    price_idx = columns["bid"] if kind == "ticks" else columns["close"]
    decimal_comma = chosen != "," and any(
        "," in row[price_idx] for row in data_rows[:20] if price_idx < len(row)
    )

    return Dialect(
        delimiter=chosen,
        has_header=has_header,
        columns=columns,
        kind=kind,
        date_order=detected,
        date_order_ambiguous=ambiguous,
        decimal_comma=decimal_comma,
        encoding=encoding,
        header_line=header_line,
        sample_rows=data_rows[:5],
    )


class _StampParser:
    """Date/time -> integer minutes, with a cache on the date component.

    An M1 file repeats each date up to 1440 times, so caching the date-to-day
    conversion removes almost all of the parsing cost.
    """

    def __init__(self, date_order: str) -> None:
        self.date_order = date_order
        self._cache: Dict[str, int] = {}

    def days(self, text: str) -> int:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        parts = re.split(r"[./\-]", text)
        if len(parts) != 3:
            raise ValueError(f"unrecognised date {text!r}")
        a, b, c = (int(p) for p in parts)
        if len(parts[0]) == 4 or a > 31:
            year, month, day = a, b, c
        elif self.date_order == "dmy":
            day, month, year = a, b, c
        else:
            month, day, year = a, b, c
            if month > 12:
                raise ValueError(
                    f"date {text!r} is not MM.DD.YYYY - re-run with --date-order dmy"
                )
        if year < 100:
            year += 2000 if year < 70 else 1900
        from datetime import date as _date

        value = _date(year, month, day).toordinal() - EPOCH_ORD
        self._cache[text] = value
        return value

    @staticmethod
    def clock(text: str) -> Tuple[int, int]:
        """Return (minutes into the day, seconds)."""
        if not text:
            return 0, 0
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        second = int(float(parts[2])) if len(parts) > 2 else 0
        return hour * 60 + minute, second

    @staticmethod
    def clock_ms(text: str) -> Tuple[int, int]:
        """Return (minutes into the day, milliseconds into that minute).

        Tick exports carry a fractional-second component MT5 uses to order
        ticks that land in the same second; bars never need this precision.
        """
        if not text:
            return 0, 0
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        seconds = float(parts[2]) if len(parts) > 2 else 0.0
        return hour * 60 + minute, int(round(seconds * 1000))


def _to_float(text: str, decimal_comma: bool) -> float:
    if decimal_comma:
        text = text.replace(".", "").replace(",", ".")
    return float(text)


def count_decimals(text: str, decimal_comma: bool) -> int:
    sep = "," if decimal_comma else "."
    _, _, frac = text.partition(sep)
    return len(frac.strip())


def read_bars(
    path: str,
    dialect: Dialect,
    issues: Optional[List[Issue]] = None,
    max_parse_errors: int = 50,
    stats: Optional[Dict[str, int]] = None,
) -> Iterator[Bar]:
    """Stream `path` as Bar records, appending parse failures to `issues`.

    `stats`, if given, receives the true error total even when the example
    list is capped.
    """
    cols = dialect.columns
    stamps = _StampParser(dialect.date_order)
    dc = dialect.decimal_comma

    date_i = cols.get("date")
    time_i = cols.get("time")
    dt_i = cols.get("datetime")
    o_i, h_i, l_i, c_i = cols["open"], cols["high"], cols["low"], cols["close"]
    tv_i = cols.get("tick_volume")
    rv_i = cols.get("real_volume")
    sp_i = cols.get("spread")
    needed = max(v for v in cols.values()) + 1

    errors = 0
    with io.open(path, "r", encoding=dialect.encoding, newline="") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            if line_no == 1 and dialect.has_header:
                continue
            row = _split(line, dialect.delimiter)
            try:
                if len(row) < needed:
                    raise ValueError(f"expected {needed} columns, found {len(row)}")
                if dt_i is not None:
                    stamp = row[dt_i].strip().replace("T", " ")
                    date_part, _, time_part = stamp.partition(" ")
                else:
                    date_part = row[date_i].strip()
                    time_part = row[time_i].strip() if time_i is not None else ""
                    if " " in date_part and not time_part:
                        date_part, _, time_part = date_part.partition(" ")

                minutes, second = stamps.clock(time_part)
                ts = stamps.days(date_part) * 1440 + minutes

                yield Bar(
                    ts=ts,
                    sec=second,
                    open=_to_float(row[o_i], dc),
                    high=_to_float(row[h_i], dc),
                    low=_to_float(row[l_i], dc),
                    close=_to_float(row[c_i], dc),
                    tick_volume=_to_float(row[tv_i], dc) if tv_i is not None and row[tv_i].strip() else 0.0,
                    real_volume=_to_float(row[rv_i], dc) if rv_i is not None and row[rv_i].strip() else 0.0,
                    spread=_to_float(row[sp_i], dc) if sp_i is not None and row[sp_i].strip() else 0.0,
                    line_no=line_no,
                )
            except (ValueError, IndexError, OverflowError) as exc:
                errors += 1
                if issues is not None and errors <= max_parse_errors:
                    issues.append(
                        Issue(
                            code="unparseable_row",
                            severity=Severity.ERROR,
                            line_no=line_no,
                            ts=None,
                            message=f"{exc} | {line[:100]}",
                        )
                    )
                continue

    if issues is not None and errors > max_parse_errors:
        issues.append(
            Issue(
                code="unparseable_row",
                severity=Severity.ERROR,
                line_no=None,
                ts=None,
                message=f"... and {errors - max_parse_errors} further unparseable rows",
            )
        )
    if stats is not None:
        stats["unparseable_row"] = errors


def read_ticks(
    path: str,
    dialect: Dialect,
    issues: Optional[List[Issue]] = None,
    max_parse_errors: int = 50,
    stats: Optional[Dict[str, int]] = None,
) -> Iterator[Tick]:
    """Stream `path` as Tick records. Mirrors `read_bars`.

    Ticks have no fixed grid, sub-second stamps, and bid/ask instead of OHLC,
    so this is a parallel path rather than a variant of `read_bars` - trying
    to unify them would leave both harder to follow for no shared benefit.
    """
    cols = dialect.columns
    stamps = _StampParser(dialect.date_order)
    dc = dialect.decimal_comma

    date_i = cols.get("date")
    time_i = cols.get("time")
    dt_i = cols.get("datetime")
    bid_i = cols.get("bid")
    ask_i = cols.get("ask")
    last_i = cols.get("last")
    vol_i = cols.get("tick_volume", cols.get("real_volume"))
    flags_i = cols.get("flags")
    needed = max(v for v in cols.values()) + 1

    errors = 0
    with io.open(path, "r", encoding=dialect.encoding, newline="") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.rstrip("\r\n")
            if not line.strip():
                continue
            if line_no == 1 and dialect.has_header:
                continue
            row = _split(line, dialect.delimiter)
            try:
                if len(row) < needed:
                    raise ValueError(f"expected {needed} columns, found {len(row)}")
                if dt_i is not None:
                    stamp = row[dt_i].strip().replace("T", " ")
                    date_part, _, time_part = stamp.partition(" ")
                else:
                    date_part = row[date_i].strip()
                    time_part = row[time_i].strip() if time_i is not None else ""
                    if " " in date_part and not time_part:
                        date_part, _, time_part = date_part.partition(" ")

                minutes, ms = stamps.clock_ms(time_part)
                ts = stamps.days(date_part) * 1440 + minutes

                yield Tick(
                    ts=ts,
                    ms=ms,
                    bid=_to_float(row[bid_i], dc) if bid_i is not None and row[bid_i].strip() else 0.0,
                    ask=_to_float(row[ask_i], dc) if ask_i is not None and row[ask_i].strip() else 0.0,
                    last=_to_float(row[last_i], dc) if last_i is not None and row[last_i].strip() else 0.0,
                    volume=_to_float(row[vol_i], dc) if vol_i is not None and row[vol_i].strip() else 0.0,
                    flags=_to_float(row[flags_i], dc) if flags_i is not None and row[flags_i].strip() else 0.0,
                    line_no=line_no,
                )
            except (ValueError, IndexError, OverflowError) as exc:
                errors += 1
                if issues is not None and errors <= max_parse_errors:
                    issues.append(
                        Issue(
                            code="unparseable_row",
                            severity=Severity.ERROR,
                            line_no=line_no,
                            ts=None,
                            message=f"{exc} | {line[:100]}",
                        )
                    )
                continue

    if issues is not None and errors > max_parse_errors:
        issues.append(
            Issue(
                code="unparseable_row",
                severity=Severity.ERROR,
                line_no=None,
                ts=None,
                message=f"... and {errors - max_parse_errors} further unparseable rows",
            )
        )
    if stats is not None:
        stats["unparseable_row"] = errors


def detect_digits(path: str, dialect: Dialect, limit: int = 5000) -> int:
    """Infer the symbol's price precision from the raw text (5 for most FX)."""
    price_i = dialect.columns["bid"] if dialect.kind == "ticks" else dialect.columns["close"]
    best = 0
    seen = 0
    with io.open(path, "r", encoding=dialect.encoding, newline="") as fh:
        for line_no, line in enumerate(fh, start=1):
            if line_no == 1 and dialect.has_header:
                continue
            if not line.strip():
                continue
            row = _split(line.rstrip("\r\n"), dialect.delimiter)
            if price_i < len(row) and _NUMERIC_RE.match(row[price_i].strip()):
                best = max(best, count_decimals(row[price_i].strip(), dialect.decimal_comma))
                seen += 1
            if seen >= limit:
                break
    return best
