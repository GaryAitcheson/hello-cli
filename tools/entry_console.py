"""Entry point for the console build of mt5clean.

PyInstaller wants a real script to start from, and importing the package here
(rather than relying on ``-m``) keeps the frozen app's argv handling identical
to the installed console script.
"""

import multiprocessing
import sys

from mt5clean.cli import main

if __name__ == "__main__":
    # Harmless on a single-process app, but required for frozen Windows builds
    # if anything ever spawns a worker: without it a child re-runs the whole
    # program instead of the worker function.
    multiprocessing.freeze_support()
    sys.exit(main())
