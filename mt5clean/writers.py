"""Output formats.

`sqx` is the default because it is what StrategyQuant X's data import wizard
reads with the least fiddling: comma separated, `yyyy.MM.dd` dates, `HH:mm:ss`
times, one header row.
"""

from __future__ import annotations

import io
from typing import Iterable, List, TextIO

from .model import Bar, ts_to_datetime

FORMATS = ("sqx", "mt5", "csv")

_SQX_HEADER = "Date,Time,Open,High,Low,Close,Volume,Spread"
_MT5_HEADER = "<DATE>\t<TIME>\t<OPEN>\t<HIGH>\t<LOW>\t<CLOSE>\t<TICKVOL>\t<VOL>\t<SPREAD>"
_CSV_HEADER = "datetime,open,high,low,close,volume,spread"


class BarWriter:
    def __init__(self, handle: TextIO, fmt: str = "sqx", digits: int = 5) -> None:
        if fmt not in FORMATS:
            raise ValueError(f"unknown output format {fmt!r}; choose from {', '.join(FORMATS)}")
        self.handle = handle
        self.fmt = fmt
        self.price = f"{{:.{digits}f}}".format
        self._write_header()

    def _write_header(self) -> None:
        header = {"sqx": _SQX_HEADER, "mt5": _MT5_HEADER, "csv": _CSV_HEADER}[self.fmt]
        self.handle.write(header + "\n")

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
        self.handle.write(line + "\n")

    def write_all(self, bars: Iterable[Bar]) -> int:
        count = 0
        for bar in bars:
            self.write(bar)
            count += 1
        return count


def open_output(path: str) -> TextIO:
    return io.open(path, "w", encoding="utf-8", newline="\n")


def import_hint(fmt: str, digits: int) -> List[str]:
    """The settings to type into the destination tool's import dialog."""
    if fmt == "sqx":
        return [
            "StrategyQuant X  ->  Data -> Import data -> from CSV file:",
            "  Column separator : Comma (,)",
            "  Date format      : yyyy.MM.dd",
            "  Time format      : HH:mm:ss",
            "  First row        : header (skip 1 line)",
            "  Columns          : Date, Time, Open, High, Low, Close, Volume, Spread",
            f"  Price decimals   : {digits}  (set the symbol's point value to match)",
            "  Timezone         : whatever your MT5 server used - the file is not converted",
        ]
    if fmt == "mt5":
        return ["Tab-separated, same layout MT5 writes from Tools -> History Center export."]
    return ["Generic CSV: ISO-8601 datetime in column 1, then OHLC, volume, spread."]
