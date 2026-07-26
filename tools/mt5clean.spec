# PyInstaller spec for mt5clean. Build with:
#
#     pyinstaller tools/mt5clean.spec --noconfirm
#
# Produces two one-file executables in dist/:
#
#   mt5clean.exe      console build - the CLI (info / audit / clean)
#   mt5clean-gui.exe  windowed build - double-click to open the file picker
#
# Two binaries rather than one because the choice is mutually exclusive on
# Windows: a console build flashes a terminal when double-clicked, and a
# windowed build has nowhere to print a CLI report to.

import os
import sys

block_cipher = None

# The spec runs from the repo root when invoked as `pyinstaller tools/...`,
# but SPECPATH is the tools/ directory either way.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# cli.py imports the GUI lazily, inside cmd_gui(), so PyInstaller's static
# analysis never sees it. Without this the console build raises
# ModuleNotFoundError the moment someone runs `mt5clean` with no arguments.
HIDDEN = [
    "mt5clean.gui",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.font",
    "tkinter.messagebox",
    "tkinter.ttk",
]

# Nothing here needs numpy/pandas/etc; excluding the usual suspects keeps the
# binary near 10 MB instead of dragging in whatever else is in the build env.
EXCLUDES = [
    "numpy",
    "pandas",
    "matplotlib",
    "scipy",
    "PIL",
    "pytest",
    "setuptools",
    "pip",
]


def analysis(entry):
    return Analysis(
        [os.path.join(ROOT, "tools", entry)],
        pathex=[ROOT],
        binaries=[],
        datas=[],
        hiddenimports=HIDDEN,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=EXCLUDES,
        win_no_prefer_redirects=False,
        win_private_assemblies=False,
        cipher=block_cipher,
        noarchive=False,
    )


console_a = analysis("entry_console.py")
console_pyz = PYZ(console_a.pure, console_a.zipped_data, cipher=block_cipher)
console_exe = EXE(
    console_pyz,
    console_a.scripts,
    console_a.binaries,
    console_a.zipfiles,
    console_a.datas,
    [],
    name="mt5clean",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
)

gui_a = analysis("entry_gui.py")
gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data, cipher=block_cipher)
gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    gui_a.binaries,
    gui_a.zipfiles,
    gui_a.datas,
    [],
    name="mt5clean-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
)
