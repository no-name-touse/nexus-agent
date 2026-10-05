"""Prepare, execute and report the small local suite without Docker or downloads."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sqlite3
import time
import tomllib
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean, median
from uuid import uuid4

from backend.configuration import ClientPaths, atomic_write_text
from backend.domain import safe_error_message
from backend.providers import ModelConfig
from backend.runtime import RunnerSettings, build_application
from backend.runtime.core.contracts import InterruptDecision, InterruptRequest
from backend.sandbox import WindowsBrokerClient
from backend.storage.settings.contract import normalize_sandbox_config
from backend.storage.settings.crypto import decrypt_secret
from backend.tools import ToolError, ToolRegistry
from backend.tools.workspace_tools import build_workspace_tools

from .suite import SUITE_VERSION, Task, grade_task, safe_path, seed_task, select_tasks, suite_digest

LOCAL_TOOLS = frozenset({"run_command", "write_stdin", "read_file", "file_operation", "glob", "grep"})
DISABLED = {name: {"enabled": False} for name in ("memory", "skills", "mcp", "subagents")}


def load_model(data_root: Path) -> tuple[ModelConfig, dict]:
    """Read existing settings with SQLite mode=ro. Never initialize or mutate them."""
    paths = ClientPaths(data_root.resolve())
    if not paths.state_db.is_file() or not paths.config_file.is_file():
        raise ValueError("此data-root没有现有配置和runtime/state.db，请指定网页后端实际使用的目录。")
    with sqlite3.connect(paths.state_db.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT provider_configs_json FROM provider_settings WHERE id=1").fetchone()
    records = json.loads(row[0]) if row else []
    record = next((item for item in records if item.get("is_active")), None)
    if not record:
        raise ValueError("没有已启用的模型配置，请先在网页中配置模型。")
    key = decrypt_secret(record["api_key_ciphertext"]) if record.get("api_key_ciphertext") else ""
    if not key:
        raise ValueError("已启用的模型没有配置API Key。")
    with paths.config_file.open("rb") as handle:
        config = tomllib.load(handle)
    return ModelConfig.from_mapping({**record, "api_key": key}), config


def redact(value: object, model: ModelConfig | None = None) -> str:
    text = str(value)
    if model is not None:
        for secret in (model.api_key, model.base_url):
            if secret:
                text = text.replace(secret, "[redacted]")
    return safe_error_message(RuntimeError(text))


def preflight(model: ModelConfig, config: dict) -> dict:
    """No model requests, process launches or sandbox policy changes."""
    if os.name != "nt":
        raise ValueError("此本地评测沿用项目Windows命令沙箱，请在Windows运行。")
    from ctypes import windll

    if not windll.shell32.IsUserAnAdmin():
        raise ValueError("请使用管理员PowerShell运行，Windows权限审计需要管理员令牌。")
    sandbox = normalize_sandbox_config(config.get("sandbox"))
    broker = WindowsBrokerClient.from_system(expected_proxy_port=int(sandbox["proxy_port"]))
    status = broker.status()
    if not status.installed or not status.healthy:
        raise ValueError("命令沙箱Broker尚未就绪；不会降级为宿主机直接执行。")
    return {
        "model": redact(model.model, model),
        "protocol": model.protocol,
        "temperature": model.temperature,
        "configured_max_tokens": model.max_tokens,
        "terminal": config.get("runtime", {}).get("terminal_type", "cmd"),
        "sandbox": "healthy",
        "model_requests": 0,
    }


def approve_local(request: InterruptRequest) -> InterruptDecision:
    """Approve local task operations, never host escalation or external trust."""
    if request.kind == "tool":
        if request.data.get("approval_kind") or request.data.get("tool") not in LOCAL_TOOLS:
            return InterruptDecision("deny")
        return InterruptDecision("continue")
    if request.kind == "plan":
        return InterruptDecision("implement")
    if request.kind == "skill":
        return InterruptDecision("skip")
    return InterruptDecision("cancel")


class Telemetry:
    def __init__(self, model: ModelConfig):
        self.model = model
        self.started = time.perf_counter()
        self.responses = 0
        self.requests = 0
        self.retries = 0
        self.tokens = 0
        self.usage_complete = True
        self.successful_tools: set[str] = set()
        self.calls: set[str] = set()
        self.finished: dict = {}
        self.events: list[dict] = []
        self.tool_failures: list[str] = []
        self.model_failed = False
        self.environment_failed = False

    def __call__(self, event) -> None:
        kind = event.kind
        tool = str(event.data.get("tool") or "")
        if kind == "model_request":
            self.requests += 1
        elif kind == "model_retry":
            self.retries += 1
        elif kind == "model_error":
            self.model_failed = True
        elif kind == "model_response":
            self.responses += 1
            usage = event.data.get("usage")
            value = usage.get("total_tokens") if isinstance(usage, dict) else getattr(usage, "total_tokens", None)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.tokens += value
            else:
                self.usage_complete = False
        elif kind == "tool_call":
            tool = event.message
            self.calls.add(str(event.data.get("call_id") or f"uncounted-{len(self.calls)}"))
        elif kind == "run_finished":
            self.finished = event.data
        elif kind == "tool_result":
            if tool in {"run_command", "write_stdin"}:
                try:
                    output = json.loads(event.message)
                except (TypeError, ValueError):
                    output = {}
                if output.get("exit_code") == 0:
                    self.successful_tools.add("run_command")
            elif tool:
                self.successful_tools.add(tool)
        elif kind == "tool_failed":
            code = str(event.data.get("failure_code") or "tool_error")
            self.tool_failures.append(code)
            diagnostic = str(event.message) + str(event.data.get("error_report", {}))
            self.environment_failed |= any(
                token in diagnostic for token in ("Sandbox", "WinError", "machine disk reserve", "BrokerUnavailable")
            )
        if (
            kind in {"tool_call", "tool_result", "tool_failed", "model_error", "model_retry", "run_finished"}
            and len(self.events) < 300
        ):
            self.events.append(
                {
                    "kind": kind,
                    "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 2),
                    "tool": tool,
                    "message": redact(event.message, self.model)[:1500] if kind != "tool_call" else tool,
                }
            )

    def metrics(self, duration_ms: float | None) -> dict:
        return {
            "agent_duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
            "model_calls": int(self.finished.get("model_calls", self.requests)),
            "tool_calls": len(self.calls),
            "tool_call_events": int(self.finished.get("tool_calls", len(self.calls))),
            "retries": int(self.finished.get("retries", self.retries)),
            "total_tokens": self.tokens if self.responses and self.usage_complete else None,
            "reported_token_subtotal": self.tokens,
            "usage_complete": bool(self.responses and self.usage_complete),
        }

    def capture_tool_messages(self, messages: list) -> None:
        # The normal publisher deliberately hides recoverable tool_failed events
        # from on_event. Read canonical final ToolMessages instead of losing them.
        for message in messages:
            for tool in getattr(message, "tool_messages", ()):
                if tool.call_id:
                    self.calls.add(tool.call_id)
                if tool.status != "failed":
                    continue
                code = str(tool.failure_code or "tool_error")
                self.tool_failures.append(code)
                diagnostic = str(tool.content or "")
                self.environment_failed |= any(
                    token in diagnostic.lower()
                    for token in ("sandbox", "winerror", "openprocess", "machine disk reserve", "brokerunavailable")
                )
                if len(self.events) < 300:
                    self.events.append(
                        {
                            "kind": "tool_failed_snapshot",
                            "tool": tool.name,
                            "call_id": tool.call_id,
                            "failure_code": code,
                            "message": redact(diagnostic, self.model)[:1500],
                        }
                    )


def run_attempt(
    task: Task,
    directory: Path,
    model: ModelConfig,
    config: dict,
    *,
    attempt: int = 1,
    max_tool_calls: int = 20,
    max_model_calls: int = 16,
    timeout_seconds: int = 300,
) -> dict:
    """Run normal Runtime with normal Windows command sandbox, not tools_override."""
    workspace = directory / "workspace"
    seed_task(task, workspace)
    paths = ClientPaths(directory / "client")
    telemetry = Telemetry(model)
    app = None
    conversation = None
    state = None
    started = None
    duration = None
    failure = None
    error = None
    stopped_by_budget = None
    verdicts: list[dict] = []
    phase = "application"
    wall_started = time.perf_counter()

    def cancelled() -> bool:
        nonlocal stopped_by_budget
        if started is not None and time.perf_counter() - started >= timeout_seconds:
            stopped_by_budget = "timeout"
        elif telemetry.requests >= max_model_calls:
            stopped_by_budget = "model_call_limit"
        return stopped_by_budget is not None

    try:
        overrides = {
            **DISABLED,
            "runtime": {"terminal_type": config.get("runtime", {}).get("terminal_type", "cmd")},
        }
        if isinstance(config.get("sandbox"), dict):
            overrides["sandbox"] = config["sandbox"]
        app = build_application(
            workspace,
            planner_name="llm",
            paths=paths,
            model_config=model,
            config_override=overrides,
            settings=RunnerSettings(
                max_tool_calls=max_tool_calls,
                max_tool_parellel=1,
                max_transport_retries=1,
                log_full_messages=False,
            ),
        )
        # Keep the runner's SandboxLauncher; restrict only the advertised tools.
        terminal = app.runner.tools
        local_tools = build_workspace_tools(workspace, terminal_type=overrides["runtime"]["terminal_type"])
        registry = ToolRegistry([tool for tool in local_tools if tool.name in LOCAL_TOOLS])
        if not set(registry.names()).issubset(set(terminal.names())):
            raise ToolError("Local tools differ from the normal Runtime")
        app.runner.tools = registry
        conversation = app.open_conversation()
        # All suite outputs belong to this fresh attempt workspace. Approval
        # alone does not change the default read-only command sandbox policy.
        conversation.ensure_session()
        if conversation.runtime is None:
            raise RuntimeError("Evaluation conversation runtime is unavailable")
        conversation.runtime.state.permission_mode = "workspace_write"
        conversation.runtime.save()
        phase = "agent"
        started = time.perf_counter()
        state = conversation.run_task(
            task.prompt,
            mode="agent",
            on_event=telemetry,
            interrupt=approve_local,
            cancel_requested=cancelled,
        )
        duration = (time.perf_counter() - started) * 1000
        telemetry.capture_tool_messages(conversation.runtime.state.messages)
        phase = "grading"
        verdicts = grade_task(task, workspace, telemetry.successful_tools)
        if state.status != "completed" or stopped_by_budget:
            failure = stopped_by_budget or (
                "environment_error"
                if telemetry.environment_failed
                else "model_error"
                if telemetry.model_failed
                else "runtime_error"
            )
        elif not all(item["passed"] for item in verdicts):
            failure = "environment_error" if telemetry.environment_failed else "acceptance_failed"
    except KeyboardInterrupt:
        failure = "cancelled"
        error = "用户中止；保留已有结果。"
    except Exception as exc:
        failure = f"{phase}_error"
        error = redact(exc, model)[:1500]
    finally:
        if state is None and conversation is not None and conversation.runtime is not None:
            telemetry.capture_tool_messages(conversation.runtime.state.messages)
        if started is not None and duration is None:
            duration = (time.perf_counter() - started) * 1000
        if app is not None:
            try:
                app.close()
            except Exception as exc:
                failure = "cleanup_error"
                error = redact(exc, model)[:1500]
    result = {
        "task_id": task.id,
        "category": task.category,
        "split": task.split,
        "attempt": attempt,
        "passed": failure is None and bool(verdicts) and all(item["passed"] for item in verdicts),
        "runtime_status": state.status if state is not None else "error",
        "failure_kind": failure,
        "error": error,
        "metrics": telemetry.metrics(duration),
        "wall_duration_ms": round((time.perf_counter() - wall_started) * 1000, 2),
        "verdicts": verdicts,
        "successful_tools": sorted(telemetry.successful_tools),
        "tool_failures": telemetry.tool_failures,
        "final_answer": redact(state.final_answer or "", model)[:1500] if state is not None else "",
        "evidence_file": str(directory / "events.json"),
        "workspace": str(workspace),
    }
    atomic_write_text(directory / "events.json", json.dumps(telemetry.events, ensure_ascii=False, indent=2))
    atomic_write_text(directory / "result.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result


def summarize(results: list[dict], planned: int) -> dict:
    successes = [result for result in results if result["passed"]]
    times = [
        r["metrics"]["agent_duration_ms"] / 1000 for r in successes if r["metrics"]["agent_duration_ms"] is not None
    ]
    tokens = [r["metrics"]["total_tokens"] for r in results if r["metrics"]["total_tokens"] is not None]
    categories = sorted({result["category"] for result in results})
    return {
        "planned_attempts": planned,
        "completed_attempts": len(results),
        "complete": len(results) == planned,
        "unique_tasks": len({r["task_id"] for r in results}),
        "passed_attempts": len(successes),
        "success_rate": len(successes) / len(results) if results else None,
        "failures_by_kind": dict(Counter(r["failure_kind"] for r in results if not r["passed"])),
        "successful_agent_seconds_mean": mean(times) if times else None,
        "successful_agent_seconds_median": median(times) if times else None,
        "successful_tool_calls_mean": mean(r["metrics"]["tool_calls"] for r in successes) if successes else None,
        "token_coverage": len(tokens) / len(results) if results else None,
        "reported_tokens_sum": sum(tokens),
        "tokens_mean_when_reported": mean(tokens) if tokens else None,
        "by_category": {
            category: {
                "attempts": sum(r["category"] == category for r in results),
                "passes": sum(r["category"] == category and r["passed"] for r in results),
            }
            for category in categories
        },
    }


def write_report(root: Path, meta: dict, results: list[dict], planned: int) -> dict:
    summary = summarize(results, planned)
    report = {"meta": meta, "summary": summary, "results": results}
    atomic_write_text(root / "report.json", json.dumps(report, ensure_ascii=False, indent=2))
    rate = f"{summary['success_rate']:.1%}" if summary["success_rate"] is not None else "尚未执行"
    lines = [
        "# Praxis 自建任务评测结果",
        "",
        "真实模型执行结果；不是公开benchmark、官方排行榜或单元测试成绩。",
        f"任务版本：{meta['suite_version']}；模型：{meta['model']}；温度：{meta['temperature']}。",
        f"已执行 {len(results)}/{planned} 次；通过 {summary['passed_attempts']} 次；端到端成功率 {rate}。",
        "成功率分母包含已执行的环境、模型与验收失败；未执行任务不进入分母。",
        "成功耗时仅统计Agent阶段；不含环境创建、评分、清理。Token缺失以null记录。",
        "",
        "|任务|类别|次数|通过|失败原因|",
        "|---|---|---:|---|---|",
    ]
    for result in results:
        lines.append(
            f"|{result['task_id']}|{result['category']}|{result['attempt']}|{'是' if result['passed'] else '否'}|{result['failure_kind'] or '-'}|"
        )
    atomic_write_text(root / "report.md", "\n".join(lines) + "\n")
    resume = "# 简历指标\n\n"
    if not summary["complete"]:
        resume += "评测尚未完成，不生成可直接用于简历的最终指标。\n"
    else:
        labels = {"command": "命令执行", "file": "文件操作", "data": "数据处理", "workflow": "多步任务"}
        coverage = "、".join(labels.get(category, category) for category in sorted(summary["by_category"]))
        resume += (
            f"构建覆盖{coverage}的{summary['unique_tasks']}项自建评测集，"
            f"通过独立会话、测试文件重置与程序化验收完成{len(results)}次评测，"
            f"在固定模型配置下端到端任务成功率为{rate}。\n"
        )
        if summary["successful_agent_seconds_mean"] is not None:
            resume += f"成功任务平均Agent执行耗时{summary['successful_agent_seconds_mean']:.1f}s，平均工具调用{summary['successful_tool_calls_mean']:.1f}次。\n"
        resume += "\n仅代表这个自建基础任务集的结果；不证明MCP、多Agent或真实生产场景能力。\n"
    atomic_write_text(root / "resume_metrics.md", resume)
    return summary


def _new_run_root(output: Path) -> Path:
    if output.is_symlink():
        raise ValueError("输出目录不能是符号链接")
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    root = safe_path(output, f"run-{stamp}-{uuid4().hex[:8]}")
    root.mkdir(parents=True, exist_ok=False)
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="20题本地自建Agent评测，无Docker；默认只列题，不调用模型。")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--list", action="store_true")
    actions.add_argument("--prepare", action="store_true", help="生成练习输入，不调用模型或生成成绩")
    actions.add_argument("--preflight", action="store_true", help="检查已配置模型及沙箱，不调用模型")
    actions.add_argument("--run", action="store_true", help="调用真实模型与正常Agent")
    parser.add_argument("--allow-model-api", action="store_true", help="明确同意模型调用可能产生费用")
    parser.add_argument("--task", action="append", help="任务ID，可重复")
    parser.add_argument("--split", choices=("all", "dev", "holdout"), default="all")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--data-root", type=Path, default=Path(r"D:\PraxisData"))
    parser.add_argument("--output", type=Path, default=Path(r"D:\PraxisSelfEval"))
    parser.add_argument("--max-tool-calls", type=int, default=20)
    parser.add_argument("--max-model-calls", type=int, default=16)
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    args = parser.parse_args(argv)
    model: ModelConfig | None = None
    try:
        if not 1 <= args.repeat <= 10 or not 1 <= args.max_tool_calls <= 100:
            raise ValueError("repeat须为1到10，max-tool-calls须为1到100")
        if not 2 <= args.max_model_calls <= 100 or not 30 <= args.timeout_seconds <= 1800:
            raise ValueError("max-model-calls须为2到100，timeout-seconds须为30到1800")
        if not 256 <= args.max_output_tokens <= 8192:
            raise ValueError("max-output-tokens须为256到8192")
        tasks = select_tasks(args.task, args.split)
        if args.run and not args.allow_model_api:
            raise ValueError("实际运行会调用模型，请确认费用后添加--allow-model-api。")
        if args.prepare:
            root = _new_run_root(args.output)
            for task in tasks:
                seed_task(task, root / "practice" / task.id)
            atomic_write_text(
                root / "questions.md", "\n\n".join(f"## {t.id} [{t.category}/{t.split}]\n\n{t.prompt}" for t in tasks)
            )
            print(f"已准备{len(tasks)}题：{root}；未调用模型，未生成成绩。")
            return 0
        if not args.run and not args.preflight:
            print(f"{SUITE_VERSION}：{len(tasks)}题，默认{len(tasks) * args.repeat}次；没有模型调用。")
            for task in tasks:
                print(f"{task.id:10} {task.category:8} {task.split:7} {task.question}")
            return 0
        model, config = load_model(args.data_root)
        diagnostic = preflight(model, config)
        if args.preflight:
            print(json.dumps(diagnostic, ensure_ascii=False, indent=2))
            return 0
        model = replace(model, max_tokens=min(model.max_tokens, args.max_output_tokens))
        root = _new_run_root(args.output)
        meta = {
            "suite_version": SUITE_VERSION,
            "suite_sha256": suite_digest(),
            "selected_tasks": [task.id for task in tasks],
            "split": args.split,
            "repeat": args.repeat,
            "model": redact(model.model, model),
            "protocol": model.protocol,
            "temperature": model.temperature,
            "max_output_tokens": model.max_tokens,
            "terminal": diagnostic["terminal"],
            "max_tool_calls": args.max_tool_calls,
            "max_model_calls": args.max_model_calls,
            "timeout_seconds": args.timeout_seconds,
            "transport_retries": 1,
            "local_tools_only": True,
            "permission_mode": "workspace_write",
            "memory_mcp_skills_subagents": "disabled",
            "grading_version": SUITE_VERSION,
            "evaluation_code_sha256": hashlib_code(),
            "runtime_code_sha256": hashlib_runtime(),
            "python_version": platform.python_version(),
            "os": platform.system(),
            "model_request_timeout_seconds": model.timeout_seconds,
        }
        atomic_write_text(root / "questions.md", "\n\n".join(f"## {t.id}\n\n{t.prompt}" for t in tasks))
        results: list[dict] = []
        planned = len(tasks) * args.repeat
        write_report(root, meta, results, planned)
        print(f"真实模型评测：{len(tasks)}题×{args.repeat}次；报告目录：{root}", flush=True)
        for attempt in range(1, args.repeat + 1):
            for task in tasks:
                print(f"[{len(results) + 1}/{planned}] {task.id} 第{attempt}次", flush=True)
                result = run_attempt(
                    task,
                    root / "attempts" / f"{task.id}-{attempt}",
                    model,
                    config,
                    attempt=attempt,
                    max_tool_calls=args.max_tool_calls,
                    max_model_calls=args.max_model_calls,
                    timeout_seconds=args.timeout_seconds,
                )
                results.append(result)
                summary = write_report(root, meta, results, planned)
                print(f"  {'通过' if result['passed'] else result['failure_kind']}", flush=True)
                if result["failure_kind"] == "cancelled":
                    return 130
                # Do not pay for dozens of doomed runs if the environment/model is broken.
                last = results[-2:]
                if len(last) == 2 and all(
                    r["failure_kind"] in {"environment_error", "model_error", "application_error", "agent_error"}
                    for r in last
                ):
                    print("连续两次运行环境/模型错误，已停止；部分结果保留，不生成最终简历指标。")
                    return 2
        print(f"通过 {summary['passed_attempts']}/{planned}，成功率 {summary['success_rate']:.1%}。报告：{root}")
        return 0
    except KeyboardInterrupt:
        print("已中止；若已开始执行，现有逐题结果和部分报告仍保留。")
        return 130
    except Exception as exc:
        print(f"评测未能启动或继续：{redact(exc, model)}")
        return 2


def hashlib_code() -> str:
    import hashlib

    from . import suite

    return hashlib.sha256(Path(__file__).read_bytes() + Path(suite.__file__).read_bytes()).hexdigest()


def hashlib_runtime() -> str:
    import hashlib

    root = Path(__file__).resolve().parents[1] / "backend" / "src"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
