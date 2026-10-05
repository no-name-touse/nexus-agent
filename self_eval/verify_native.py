"""Free local Runtime smoke check with the real Windows Broker and audit policy."""

from __future__ import annotations

import argparse
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from backend.configuration import atomic_write_text

from .run import load_model, preflight, run_attempt
from .suite import Check, Task, safe_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, default=Path(r"D:\PraxisData"))
    args = parser.parse_args()
    root = args.output.absolute()
    log = safe_path(root, "console.log")
    receipt = safe_path(root, "exit.json")
    root.mkdir(parents=True, exist_ok=True)
    if receipt.exists():
        raise ValueError("Use a new verification directory")
    code = 2
    with log.open("x", encoding="utf-8", buffering=1) as handle, redirect_stdout(handle), redirect_stderr(handle):
        try:
            # Configuration is read only for the normal terminal/sandbox settings.
            # The real provider is never called: all model requests go to loopback.
            configured, config = load_model(args.data_root)
            preflight(configured, config)
            from tests.benchmark_local_support import local_model

            task = Task(
                "native-command-proof",
                "command",
                "通过命令工具创建native-proof.txt，内容为SANDBOX_NATIVE_OK。",
                {},
                (Check("text", "native-proof.txt", "SANDBOX_NATIVE_OK"),),
                require_command=True,
            )
            with local_model(
                tool_name="run_command", tool_arguments={"cmd": "echo SANDBOX_NATIVE_OK>native-proof.txt"}
            ) as (model, calls):
                result = run_attempt(task, root / "attempt", model, config, timeout_seconds=60)
            code = 0 if result["passed"] else 2
            print(
                json.dumps(
                    {
                        "passed": result["passed"],
                        "failure_kind": result["failure_kind"],
                        "local_model_requests": len(calls),
                        "paid_model_requests": 0,
                    },
                    ensure_ascii=False,
                )
            )
        except BaseException as exc:
            print(f"Native verification failed: {type(exc).__name__}")
        finally:
            atomic_write_text(receipt, json.dumps({"exit_code": code, "paid_model_requests": 0}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
