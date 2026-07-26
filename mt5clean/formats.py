"""Sniffing and parsing of MetaTrader 5 / MetaTrader 4 exports.

MT5 writes bar exports as tab-separated files with angle-bracket headers::

    <DATE>	<TIME>	<OPEN>	<HIGH>	<LOW>	<CLOSE>	<TICKVOL>	<VOL>	<SPREAD>
    2023.01.02	00:00:00	1.06975	1.07010	1.06952	1.06965	123	0	12

and tick exports as::

    <DATE>	<TIME>	<BID>	<ASK>	<LAST>	<VOLUME>	<FLAGS>
    2023.01.02	00:00:00.123	1.06975	1.06985	0	0	6

MT4 history-center exports are headerless comma-separated files. Some
platforms re-export with ``;`` separators and comma decimal marks. This
module works out which of those it is and turns the rows into records.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Iterable, Iterator, Sequence

DELIMITERS = ("\t", ",", ";", "|")

DATE_FORMATS = ("%Y.%m.%d", "%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y")
TIME_FORMATS = ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M")

# Header aliases, keyed by the canonical field name. Headers are normalised
# (angle brackets stripped, lowercased, non-alphanumerics collapsed) first.
HEADER_ALIASES = {
    "date": ("date",),
    "time": ("time",),
    "datetime": ("datetime", "timestamp", "date_time", "gmt_time", "local_time"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "tickvol": ("tickvol", "tick_volume", "tickvolume", "ticks"),
    "vol": ("vol", "volume", "real_volume", "realvolume"),
    "spread": ("spread", "spr"),
    "bid": ("bid",),
    "ask": ("ask",),
    "last": ("last",),
    "flags": ("flags", "flag"),
}


class ParseError(ValueError):
    """Raised when a file cannot be interpreted as an MT4/MT5 export."""


@dataclass
class Bar:
    """One OHLC bar."""

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    tickvol: float = 0.0
    vol: float = 0.0
    spread: float = 0.0
    line: int = 0
    synthetic: bool = False

    def key(self) -> tuple:
        """Value identity, ignoring provenance — used for duplicate grading."""
        return (
            self.ts,
            self.open,
            self.high,
            self.low,
            self.close,
            self.tickvol,
            self.vol,
            self.spread,
        )


@dataclass
class Tick:
    """One bid/ask tick."""

    ts: datetime
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    vol: float = 0.0
    flags: float = 0.0
    line: int = 0
    synthetic: bool = False

    def key(self) -> tuple:
        return (self.ts, self.bid, self.ask, self.last, self.vol, self.flags)


@dataclass
class Dialect:
    """How to read one particular file."""

    delimiter: str
    has_header: bool
    kind: str  # "bars" or "ticks"
    columns: dict[str, int]  # canonical field name -> column index
    decimal_comma: bool = False
    header_row: list[str] = field(default_factory=list)
    angle_headers: bool = False

    def describe(self) -> str:
        delim = {"\t": "tab", ",": "comma", ";": "semicolon", "|": "pipe"}.get(
            self.delimiter, repr(self.delimiter)
        )
        bits = [f"{self.kind}", f"{delim}-separated"]
        bits.append("with header" if self.has_header else "headerless")
        if self.decimal_comma:
            bits.append("comma decimals")
        return ", ".join(bits)


@dataclass
class BadRow:
    """A row that could not be turned into a record."""

    line: int
    raw: str
    reason: str


def _normalise_header(name: str) -> str:
    name = name.strip().strip("<>").strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return name


def _looks_like_date(value: str) -> bool:
    return _parse_date(value.strip()) is not None


def _looks_like_time(value: str) -> bool:
    return _parse_time(value.strip()) is not None


def _parse_date(value: str) -> datetime | None:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _parse_time(value: str) -> timedelta | None:
    for fmt in TIME_FORMATS:
        try:
            t = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return timedelta(
            hours=t.hour, minutes=t.minute, seconds=t.second, microseconds=t.microsecond
        )
    return None


def parse_timestamp(date_part: str, time_part: str | None = None) -> datetime | None:
    """Parse an MT4/MT5 timestamp from a date field plus optional time field."""
    date_part = date_part.strip()
    if time_part is None:
        # Combined column: "2023.01.02 00:00:00".
        chunks = date_part.replace("T", " ").split()
        if not chunks:
            return None
        date_part, time_part = chunks[0], (chunks[1] if len(chunks) > 1 else "")
    time_part = (time_part or "").strip()

    base = _parse_date(date_part)
    if base is None:
        return None
    if not time_part:
        return base
    offset = _parse_time(time_part)
    if offset is None:
        return None
    return base + offset


def _sniff_delimiter(sample_lines: Sequence[str]) -> str:
    best, best_score = ",", -1.0
    for delim in DELIMITERS:
        counts = [line.count(delim) for line in sample_lines if line.strip()]
        if not counts or max(counts) == 0:
            continue
        # Prefer the delimiter that appears often *and* consistently.
        modal = max(set(counts), key=counts.count)
        if modal == 0:
            continue
        consistency = counts.count(modal) / len(counts)
        score = modal * consistency
        if score > best_score:
            best, best_score = delim, score
    return best


def _headerless_columns(fields: Sequence[str], kind: str) -> dict[str, int]:
    """Map positional columns for files that ship without a header row."""
    n = len(fields)
    split_datetime = n >= 2 and _looks_like_time(fields[1])
    if kind == "ticks":
        names = ["bid", "ask", "last", "vol", "flags"]
    else:
        names = ["open", "high", "low", "close", "tickvol", "vol", "spread"]

    columns: dict[str, int] = {}
    if split_datetime:
        columns["date"], columns["time"] = 0, 1
        start = 2
    else:
        columns["datetime"] = 0
        start = 1
    for offset, name in enumerate(names):
        idx = start + offset
        if idx >= n:
            break
        columns[name] = idx
    return columns


def _guess_kind(columns: Iterable[str]) -> str:
    names = set(columns)
    if {"bid", "ask"} & names:
        return "ticks"
    return "bars"


def sniff(text: str, kind: str = "auto") -> Dialect:
    """Work out how to read ``text``.

    ``kind`` may be ``"auto"``, ``"bars"`` or ``"ticks"``; ``"auto"`` decides
    from the header, or from the column count for headerless files.
    """
    lines = [ln for ln in text.splitlines() if ln.strip()][:50]
    if not lines:
        raise ParseError("file is empty")

    delimiter = _sniff_delimiter(lines)
    first = next(csv.reader([lines[0]], delimiter=delimiter))
    first = [f.strip() for f in first]
    has_header = not _looks_like_date(first[0]) if first else False
    angle_headers = has_header and first[0].startswith("<")

    decimal_comma = False
    if delimiter != ",":
        body = lines[1:] if has_header else lines
        sample = "\n".join(body[:20])
        # A comma between two digits, in a file not separated by commas, is a
        # decimal mark.
        decimal_comma = bool(re.search(r"\d,\d", sample))

    if has_header:
        normalised = [_normalise_header(f) for f in first]
        columns: dict[str, int] = {}
        for canonical, aliases in HEADER_ALIASES.items():
            for idx, name in enumerate(normalised):
                if name in aliases and canonical not in columns:
                    columns[canonical] = idx
        # A bar file whose only volume column is <VOL> still means tick volume
        # in MT4-style exports; keep both, checks tolerate either being absent.
        detected = _guess_kind(columns)
        if kind == "auto":
            kind = detected
        if "date" not in columns and "datetime" not in columns:
            raise ParseError(
                "no date/time column found in header: " + delimiter.join(first)
            )
        header_row = first
    else:
        if kind == "auto":
            # Headerless tick exports are rare; bars are 5-9 columns.
            kind = "bars"
        columns = _headerless_columns(first, kind)
        header_row = []

    required = ("open", "high", "low", "close") if kind == "bars" else ("bid",)
    missing = [name for name in required if name not in columns]
    if missing:
        raise ParseError(f"missing required {kind} column(s): {', '.join(missing)}")

    return Dialect(
        delimiter=delimiter,
        has_header=has_header,
        kind=kind,
        columns=columns,
        decimal_comma=decimal_comma,
        header_row=header_row,
        angle_headers=angle_headers,
    )


def _to_float(value: str, decimal_comma: bool) -> float:
    value = value.strip()
    if not value:
        return 0.0
    if decimal_comma:
        value = value.replace(",", ".")
    return float(value)


def parse_rows(
    text: str, dialect: Dialect, shift_minutes: int = 0
) -> tuple[list, list[BadRow]]:
    """Parse ``text`` into records plus a list of rows that could not be read.

    Returns ``(records, bad_rows)`` where records are :class:`Bar` or
    :class:`Tick` depending on ``dialect.kind``.
    """
    reader = csv.reader(io.StringIO(text), delimiter=dialect.delimiter)
    cols = dialect.columns
    shift = timedelta(minutes=shift_minutes)
    records: list = []
    bad: list[BadRow] = []

    max_index = max(cols.values())
    for line_no, row in enumerate(reader, start=1):
        if line_no == 1 and dialect.has_header:
            continue
        if not row or not any(cell.strip() for cell in row):
            continue
        raw = dialect.delimiter.join(row)
        if len(row) <= max_index:
            bad.append(
                BadRow(line_no, raw, f"expected {max_index + 1} columns, got {len(row)}")
            )
            continue

        if "datetime" in cols:
            ts = parse_timestamp(row[cols["datetime"]])
        else:
            time_part = row[cols["time"]] if "time" in cols else None
            ts = parse_timestamp(row[cols["date"]], time_part)
        if ts is None:
            bad.append(BadRow(line_no, raw, "unparseable timestamp"))
            continue
        if shift_minutes:
            ts += shift

        def num(name: str) -> float:
            idx = cols.get(name)
            if idx is None:
                return 0.0
            return _to_float(row[idx], dialect.decimal_comma)

        try:
            if dialect.kind == "bars":
                record = Bar(
                    ts=ts,
                    open=num("open"),
                    high=num("high"),
                    low=num("low"),
                    close=num("close"),
                    tickvol=num("tickvol"),
                    vol=num("vol"),
                    spread=num("spread"),
                    line=line_no,
                )
            else:
                record = Tick(
                    ts=ts,
                    bid=num("bid"),
                    ask=num("ask"),
                    last=num("last"),
                    vol=num("vol"),
                    flags=num("flags"),
                    line=line_no,
                )
        except ValueError as exc:
            bad.append(BadRow(line_no, raw, f"non-numeric field ({exc})"))
            continue
        records.append(record)

    return records, bad


def read_file(
    path: str, kind: str = "auto", shift_minutes: int = 0
) -> tuple[list, list[BadRow], Dialect]:
    """Read and parse an export file from disk."""
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as handle:
        text = handle.read()
    dialect = sniff(text, kind=kind)
    records, bad = parse_rows(text, dialect, shift_minutes=shift_minutes)
    return records, bad, dialect


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

MT5_BAR_HEADER = [
    "<DATE>",
    "<TIME>",
    "<OPEN>",
    "<HIGH>",
    "<LOW>",
    "<CLOSE>",
    "<TICKVOL>",
    "<VOL>",
    "<SPREAD>",
]
MT5_TICK_HEADER = ["<DATE>", "<TIME>", "<BID>", "<ASK>", "<LAST>", "<VOLUME>", "<FLAGS>"]


def _fmt_number(value: float, digits: int) -> str:
    if value == int(value) and digits == 0:
        return str(int(value))
    return f"{value:.{digits}f}"


def infer_digits(records: Sequence, kind: str) -> int:
    """Infer price precision from the source data, so output round-trips."""
    fields = ("open", "high", "low", "close") if kind == "bars" else ("bid", "ask")
    digits = 0
    for record in records[:5000]:
        for name in fields:
            value = getattr(record, name, 0.0)
            text = repr(float(value))
            if "e" in text or "E" in text:
                continue
            if "." in text:
                digits = max(digits, len(text.split(".")[1]))
    return min(max(digits, 1), 10)


def write_file(
    path: str,
    records: Sequence,
    dialect: Dialect,
    digits: int | None = None,
    include_synthetic_flag: bool = False,
) -> None:
    """Write records back out in MT5 export layout."""
    kind = dialect.kind
    if digits is None:
        digits = infer_digits(records, kind)
    header = MT5_BAR_HEADER if kind == "bars" else MT5_TICK_HEADER
    if include_synthetic_flag:
        header = header + ["<SYNTHETIC>"]
    delimiter = dialect.delimiter

    time_fmt = "%H:%M:%S.%f" if kind == "ticks" else "%H:%M:%S"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, delimiter=delimiter, lineterminator="\n")
        writer.writerow(header)
        for record in records:
            stamp = record.ts.strftime(time_fmt)
            if kind == "ticks":
                stamp = stamp[:-3]  # microseconds -> milliseconds
            row = [record.ts.strftime("%Y.%m.%d"), stamp]
            if kind == "bars":
                row += [
                    _fmt_number(record.open, digits),
                    _fmt_number(record.high, digits),
                    _fmt_number(record.low, digits),
                    _fmt_number(record.close, digits),
                    _fmt_number(record.tickvol, 0),
                    _fmt_number(record.vol, 0),
                    _fmt_number(record.spread, 0),
                ]
            else:
                row += [
                    _fmt_number(record.bid, digits),
                    _fmt_number(record.ask, digits),
                    _fmt_number(record.last, digits),
                    _fmt_number(record.vol, 0),
                    _fmt_number(record.flags, 0),
                ]
            if include_synthetic_flag:
                row.append("1" if record.synthetic else "0")
            writer.writerow(row)


def copy_record(record, **changes):
    """Return a copy of ``record`` with the given fields replaced."""
    return replace(record, **changes)


def iter_bad_rows(bad: Iterable[BadRow]) -> Iterator[str]:
    for row in bad:
        yield f"line {row.line}: {row.reason}"
