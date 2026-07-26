"""Amalgamate the mt5clean package into one runnable file.

The package is the real source; this exists so the tool can be handed to a
Windows machine next to MT5 as a single file, with no pip install, no PATH
entry and no directory to keep intact:

    python build_standalone.py -o mt5clean.py
    python mt5clean.py audit XAUUSD_M1.csv

Modules are concatenated in dependency order with their intra-package imports
stripped, since after flattening every name lives in one namespace.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

# Dependency order: each module may only use names defined above it.
MODULES = [
    "model",
    "timeframe",
    "reader",
    "sessions",
    "audit",
    "fixes",
    "writers",
    "report",
    "cli",
    # gui last: cli refers to run_gui lazily, resolved at call time.
    "gui",
]

RELATIVE_IMPORT = re.compile(
    r"""^(?P<indent>[ \t]*)from[ \t]+\.[\w.]*[ \t]+import[ \t]+"""
    r"""(?P<names>\([^)]*\)|[^\n]*)""",
    re.MULTILINE,
)

HEADER = '''"""mt5clean {version} - audit and repair MetaTrader 5 M1 bar exports.

Standalone build: the whole tool in one file, stdlib only.

    python mt5clean.py info   XAUUSD_M1.csv
    python mt5clean.py audit  XAUUSD_M1.csv
    python mt5clean.py clean  XAUUSD_M1.csv -o XAUUSD_sqx.csv --dedupe last --fix-ohlc

Generated from the mt5clean package by tools/build_standalone.py - edit the
package, not this file.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from array import array
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Dict, Iterator, List, NamedTuple, Optional, TextIO, Tuple
'''

FOOTER = '''

if __name__ == "__main__":
    sys.exit(main())
'''


def strip_module_source(text: str) -> str:
    """Remove the module docstring, imports, and relative-import lines."""
    # Drop the leading module docstring.
    text = re.sub(r'\A\s*(?:"""|\'\'\')(?:.|\n)*?(?:"""|\'\'\')\s*\n', "", text, count=1)

    # Relative imports become nothing: everything shares one namespace now.
    # Indented ones (inside functions) need a `pass` to keep the block valid.
    def replace(match: re.Match) -> str:
        indent = match.group("indent")
        return f"{indent}pass" if indent else ""

    text = RELATIVE_IMPORT.sub(replace, text)

    # A module's own `if __name__ == "__main__":` block must not survive: in the
    # flattened file it would fire partway through, before later sections have
    # been defined. Only the generated footer gets to be the entry point.
    text = re.sub(
        r"^if[ \t]+__name__[ \t]*==[ \t]*['\"]__main__['\"][ \t]*:[ \t]*\n"
        r"(?:(?:[ \t]+[^\n]*)?\n)*",
        "",
        text,
        flags=re.MULTILINE,
    )

    # Absolute stdlib imports are hoisted into the header instead.
    text = re.sub(
        r"^(?:from[ \t]+(?:__future__|argparse|csv|io|json|os|re|sys|array|collections"
        r"|dataclasses|datetime|typing)[ \t]+import[ \t]+[^\n]*"
        r"|import[ \t]+(?:argparse|csv|io|json|os|re|sys)[ \t]*)$\n?",
        "",
        text,
        flags=re.MULTILINE,
    )
    return text.strip("\n")


def build(package: Path, version: str) -> str:
    parts = [HEADER.format(version=version)]
    for name in MODULES:
        source = (package / f"{name}.py").read_text(encoding="utf-8")
        body = strip_module_source(source)
        parts.append(
            f"\n\n# {'=' * 70}\n# {name}.py\n# {'=' * 70}\n\n{body}\n"
        )
    parts.append(FOOTER)
    return "".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="mt5clean.py", help="file to write")
    parser.add_argument(
        "--package",
        default=str(Path(__file__).resolve().parent.parent / "mt5clean"),
        help="path to the mt5clean package directory",
    )
    args = parser.parse_args()

    package = Path(args.package)
    version_match = re.search(
        r'__version__\s*=\s*"([^"]+)"', (package / "__init__.py").read_text(encoding="utf-8")
    )
    version = version_match.group(1) if version_match else "0.0.0"

    text = build(package, version)
    # Not Path.write_text(newline=...): that keyword is 3.10+, and this script
    # has to run on the same interpreters the package supports.
    with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    lines = text.count("\n") + 1
    print(f"wrote {args.output}  ({lines:,} lines, {len(text) / 1024:,.0f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
