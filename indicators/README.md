# Inside Bar Indicator

An **Inside Bar** is a bar whose entire range sits inside the previous bar's
range: `High[current] < High[previous]` and `Low[current] > Low[previous]`.
It's a common price-action contraction/consolidation pattern, often traded
as a breakout of the "mother" bar's range.

Two implementations are provided here:

## `InsideBar_MT5.mq5`

A standalone MetaTrader 5 custom indicator. Drop it in
`MQL5/Indicators/`, compile in MetaEditor, and attach to a chart. It plots
an arrow below inside bars that follow a bullish "mother" bar, and above
inside bars that follow a bearish "mother" bar.

## `InsideBar_SQX.xml`

A StrategyQuant X / AlgoWizard custom block XML with three condition
blocks (`InsideBar_Long`, `InsideBar_Short`, `InsideBar_Any`), importable
via **AlgoWizard -> Custom Blocks -> Import**. It only compares raw price
bars (no custom indicators required).

This file was hand-authored without access to a live SQX install for
validation, so verify it imports cleanly in AlgoWizard before relying on
it — report back any import errors and they'll be fixed.

**Local convention:** copy generated custom block XML files to
`D:\SQX\Downloaded Indicators\Custom Blocks` before importing into
AlgoWizard.
