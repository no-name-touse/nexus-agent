"""Run authorized smoke or full-suite evaluation with a log and exit receipt."""

from __future__ import annotations

import argparse
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.configuration import atomic_write_text

from .run import main as evaluate
from .suite import safe_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--task", action="append", choices=("cmd-01", "file-01", "data-01"))
    selection.add_argument("--full-suite", action="store_true", help="Run all 20 tasks three times (paid model calls)")
    args = parser.parse_args()
    root = args.receipt_root.absolute()
    log_path = safe_path(root, "console.log")
    receipt = safe_path(root, "exit.json")
    root.mkdir(parents=True, exist_ok=True)
    if receipt.exists():
        raise ValueError("An existing invocation receipt must not be overwritten")
    code = 2
    with log_path.open("x", encoding="utf-8", buffering=1) as log, redirect_stdout(log), redirect_stderr(log):
        try:
            code = evaluate(
                [
                    "--run",
                    "--allow-model-api",
                    "--data-root",
                    str(args.data_root),
                    "--output",
                    str(root),
                    "--repeat",
                    "3" if args.full_suite else "1",
                    *[
                        value
                        for task in ([] if args.full_suite else args.task or ["cmd-01", "file-01", "data-01"])
                        for value in ("--task", task)
                    ],
                ]
            )
        except BaseException as exc:
            # Do not write exception strings or tracebacks that could contain secrets.
            log.write(f"Invocation failed: {type(exc).__name__}\n")
        finally:
            atomic_write_text(receipt, json.dumps({"exit_code": code}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
