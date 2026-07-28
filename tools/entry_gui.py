"""Entry point for the windowed build of mt5clean.

This is the double-click target: no console appears, the file picker opens
straight away. A path may still be passed (Windows passes one when you drag a
file onto the .exe), in which case it is preloaded.

Errors cannot go to a console in a windowed build, so anything escaping
``run_gui`` is shown in a message box instead of vanishing.
"""

import multiprocessing
import sys


def main() -> int:
    from mt5clean.gui import run_gui

    initial = sys.argv[1] if len(sys.argv) > 1 else None
    return run_gui(initial)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - last resort for a GUI build
        try:
            import tkinter.messagebox as messagebox

            messagebox.showerror("mt5clean", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass
        sys.exit(1)
