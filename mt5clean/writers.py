"""Output formats.

`mt5` is the default because it reproduces a genuine MetaTrader 5 bar export
byte for byte - tab separated, `<DATE>`/`<TIME>` header, `yyyy.MM.dd` dates,
`HH:mm:ss` times, CRLF line endings. Anything that already imports MT5 files,
StrategyQuant X included, then takes the cleaned file on the same settings
with nothing to re-map.

`sqx` and `csv` remain for tools that want a single-file comma layout, but
they are hand-rolled shapes: you have to describe them to the importer.
"""

from __future__ import annotations

import io
from typing import Iterable, List, TextIO

from .model import Bar, ts_to_datetime

FORMATS = ("mt5", "sqx", "csv")

_SQX_HEADER = "Date,Time,Open,High,Low,Close,Volume,Spread"
_MT5_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"
_CSV_HEADER = "datetime,open,high,low,close,volume,spread"

# MT5 writes CRLF; the other two are ours, so they get plain LF.
_LINE_ENDINGS = {"mt5": "\r\n", "sqx": "\n", "csv": "\n"}


class BarWriter:
    def __init__(self, handle: TextIO, fmt: str = "mt5", digits: int = 5) -> None:
        if fmt not in FORMATS:
            raise ValueError(f"unknown output format {fmt!r}; choose from {', '.join(FORMATS)}")
        self.handle = handle
        self.fmt = fmt
        self.eol = _LINE_ENDINGS[fmt]
        self.price = f"{{:.{digits}f}}".format
        self._write_header()

    def _write_header(self) -> None:
        header = {"sqx": _SQX_HEADER, "mt5": _MT5_HEADER, "csv": _CSV_HEADER}[self.fmt]
        self.handle.write(header + self.eol)

    def write(self, bar: Bar) -> None:
        dt = ts_to_datetime(bar.ts)
        p = self.price
        volume = int(bar.tick_volume)
        spread = int(bar.spread)

        if self.fmt == "sqx":
            line = (
                f"{dt:%Y.%m.%d},{dt:%H:%M}:{bar.sec:02d},"
                f"{p(bar.open)},{p(bar.high)},{p(bar.low)},{p(bar.close)},"
                f"{volume},{spread}"
            )
        elif self.fmt == "mt5":
            line = (
                f"{dt:%Y.%m.%d}\t{dt:%H:%M}:{bar.sec:02d}\t"
                f"{p(bar.open)}\t{p(bar.high)}\t{p(bar.low)}\t{p(bar.close)}\t"
                f"{volume}\t{int(bar.real_volume)}\t{spread}"
            )
        else:
            line = (
                f"{dt:%Y-%m-%d %H:%M}:{bar.sec:02d},"
                f"{p(bar.open)},{p(bar.high)},{p(bar.low)},{p(bar.close)},"
                f"{volume},{spread}"
            )
        self.handle.write(line + self.eol)

    def write_all(self, bars: Iterable[Bar]) -> int:
        count = 0
        for bar in bars:
            self.write(bar)
            count += 1
        return count


def open_output(path: str) -> TextIO:
    # newline="" so the writer's own line terminator reaches the file untouched.
    return io.open(path, "w", encoding="utf-8", newline="")


def import_hint(fmt: str, digits: int) -> List[str]:
    """The settings to type into the destination tool's import dialog."""
    if fmt == "mt5":
        return [
            "This file is byte-shaped like a MetaTrader 5 bar export, so import it",
            "exactly the way you import MT5 files.",
            "",
            "StrategyQuant X  ->  Data -> Import data -> from CSV/text file:",
            "  Separator        : Tab",
            "  Skip rows        : 1   (the <DATE> header line)",
            "  Date format      : yyyy.MM.dd HH:mm:ss",
            "  Columns          : Date, Time, Open, High, Low, Close, Volume,",
            "                     Unused, Unused",
            "                     (col 8 is <VOL>/real volume, col 9 is <SPREAD>)",
            f"  Price decimals   : {digits}  (set the symbol's point value to match)",
            "  Bar timestamp    : start of bar",
            "  Timezone         : whatever your MT5 server used - the file is not converted",
        ]
    if fmt == "sqx":
        return [
            "StrategyQuant X  ->  Data -> Import data -> from CSV file:",
            "  Separator        : Comma (,)",
            "  Skip rows        : 1   (the header line)",
            "  Date format      : yyyy.MM.dd HH:mm:ss",
            "  Columns          : Date, Time, Open, High, Low, Close, Volume, Unused",
            f"  Price decimals   : {digits}  (set the symbol's point value to match)",
            "  Timezone         : whatever your MT5 server used - the file is not converted",
            "",
            "Note: 'Date format' in SQX covers the Date and Time columns together,",
            "so it stays yyyy.MM.dd HH:mm:ss even though they are two columns.",
            "If your saved format has a single 'Date & Time' column, use --format csv",
            "or pick Date + Time separately here - a lone yyyy.MM.dd in column 1 is",
            "what produces the datetime parse error.",
        ]
    return [
        "Generic CSV: ISO-8601 datetime in column 1, then OHLC, volume, spread.",
        "In StrategyQuant X: separator Comma, skip 1 row,",
        "  Date format : yyyy-MM-dd HH:mm:ss   (dashes, no milliseconds)",
        "  Columns     : Date & Time, Open, High, Low, Close, Volume, Unused",
    ]
