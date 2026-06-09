# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`hello-cli` is a minimal Python command-line greeter. It has a single entry point (`hello.py`) with no external dependencies.

## Running the CLI

```bash
python hello.py
python hello.py --name Alice
```

## Code Structure

All logic lives in `hello.py`:
- `greet(name: str) -> str` — pure function that returns the greeting string
- `main()` — parses `--name` (default: `"World"`) via `argparse` and prints the result
