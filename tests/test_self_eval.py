"""Only temporary files and a loopback simulated model; never paid Agent scores."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from backend.providers import ModelConfig
from backend.runtime.core.contracts import InterruptRequest
from backend.runtime.core.events import RuntimeEvent
from self_eval import run
from self_eval.suite import (
    SUITE_VERSION,
    TASKS,
    Check,
    Task,
    grade_task,
    json_equal,
    safe_path,
    seed_task,
    select_tasks,
    suite_digest,
)
from tests.benchmark_local_support import local_model


def reference_outputs(task: Task, workspace: Path) -> None:
    """Test-only reference materialization. Never imported by production runner."""
    for check in task.checks:
        path = safe_path(workspace, check.path)
        if check.kind == "absent":
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if check.kind == "json":
            text = json.dumps(check.expected, ensure_ascii=False)
        elif check.kind == "csv":
            buffer = io.StringIO()
            csv.writer(buffer).writerows(check.expected)
            text = buffer.getvalue()
        else:
            text = check.expected
        path.write_bytes(text.encode("utf-8"))


def test_suite_shape_and_selection():
    assert len(TASKS) == 20
    assert len({task.id for task in TASKS}) == 20
    assert Counter(task.category for task in TASKS) == dict.fromkeys(("command", "file", "data", "workflow"), 5)
    assert len(select_tasks(split="dev")) == 8
    assert len(select_tasks(split="holdout")) == 12
    assert select_tasks(["cmd-01"])[0].id == "cmd-01"
    assert suite_digest() == suite_digest()
    with pytest.raises(ValueError):
        select_tasks(["unknown"])
    with pytest.raises(ValueError):
        select_tasks(["cmd-01"], "holdout")


@pytest.mark.parametrize("task", TASKS, ids=lambda task: task.id)
def test_all_reference_outputs_pass_and_wrong_output_fails(task, tmp_path):
    workspace = tmp_path / "workspace"
    seed_task(task, workspace)
    reference_outputs(task, workspace)
    tools = {"run_command", "file_operation"}
    assert all(check["passed"] for check in grade_task(task, workspace, tools))
    first = next(check for check in task.checks if check.kind != "absent")
    safe_path(workspace, first.path).write_text("WRONG", encoding="utf-8")
    assert not all(check["passed"] for check in grade_task(task, workspace, tools))


@pytest.mark.parametrize(
    "task", [t for t in TASKS if any(p not in t.mutable for p in t.files)], ids=lambda task: task.id
)
def test_protected_input_mutation_fails(task, tmp_path):
    workspace = tmp_path / "workspace"
    seed_task(task, workspace)
    reference_outputs(task, workspace)
    protected = next(path for path in task.files if path not in task.mutable)
    safe_path(workspace, protected).write_bytes(b"CORRUPTED")
    verdicts = grade_task(task, workspace, {"run_command", "file_operation"})
    assert any(item["check"] == f"preserved:{protected}" and not item["passed"] for item in verdicts)


def test_commands_require_real_success_and_missing_outputs_fail(tmp_path):
    task = TASKS[0]
    workspace = tmp_path / "workspace"
    seed_task(task, workspace)
    assert not all(c["passed"] for c in grade_task(task, workspace, {"run_command"}))
    reference_outputs(task, workspace)
    assert not all(c["passed"] for c in grade_task(task, workspace, {"file_operation"}))
    assert not all(c["passed"] for c in grade_task(task, workspace, set()))


@pytest.mark.parametrize(
    "path", ["../escape", r"C:\outside.txt", "/outside", r"..\escape", "a/../b", "file:stream", "C:relative"]
)
def test_path_boundaries(tmp_path, path):
    with pytest.raises(ValueError):
        safe_path(tmp_path, path)


def test_existing_workspace_is_never_overwritten(tmp_path):
    path = tmp_path / "existing"
    path.mkdir()
    marker = path / "user.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        seed_task(TASKS[0], path)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_json_numeric_and_format_policy():
    assert json_equal({"a": 39.80000001}, {"a": 39.8})
    assert not json_equal({"a": "39.8"}, {"a": 39.8})
    assert not json_equal({"a": True}, {"a": 1})
    assert not json_equal({"a": float("nan")}, {"a": 1})
    assert not json_equal({"a": 1, "extra": 2}, {"a": 1})
    assert not json_equal([2, 1], [1, 2])


def test_bom_json_and_crlf_supported(tmp_path):
    task = TASKS[0]
    seed_task(task, tmp_path / "workspace")
    (tmp_path / "workspace" / "result.json").write_bytes(b'\xef\xbb\xbf{"sum":5050}\r\n')
    assert all(c["passed"] for c in grade_task(task, tmp_path / "workspace", {"run_command"}))


def test_approval_never_allows_escalation_or_external_tools():
    assert run.approve_local(InterruptRequest("tool", "", {"tool": "run_command"})).choice == "continue"
    assert (
        run.approve_local(
            InterruptRequest("tool", "", {"tool": "run_command", "approval_kind": "sandbox_escalation"})
        ).choice
        == "deny"
    )
    assert run.approve_local(InterruptRequest("tool", "", {"tool": "web_fetch"})).choice == "deny"
    assert run.approve_local(InterruptRequest("skill", "", {})).choice == "skip"
    assert run.approve_local(InterruptRequest("question", "", {})).choice == "cancel"


def test_telemetry_distinguishes_missing_usage_and_failed_commands():
    model = ModelConfig("secret-for-test", "http://127.0.0.1/v1", "test")
    telemetry = run.Telemetry(model)
    telemetry(RuntimeEvent("model_response", "", {"usage": {"total_tokens": 12}}))
    telemetry(RuntimeEvent("model_response", "", {}))
    telemetry(RuntimeEvent("tool_call", "run_command", {"call_id": "a"}))
    telemetry(RuntimeEvent("tool_call", "run_command", {"call_id": "a"}))
    telemetry(RuntimeEvent("tool_result", '{"exit_code":1}', {"tool": "run_command"}))
    assert "run_command" not in telemetry.successful_tools
    telemetry(RuntimeEvent("tool_result", '{"exit_code":0}', {"tool": "write_stdin"}))
    assert "run_command" in telemetry.successful_tools
    telemetry(RuntimeEvent("tool_failed", "SandboxAuditUnavailable secret-for-test", {"tool": "run_command"}))
    assert telemetry.environment_failed
    metrics = telemetry.metrics(None)
    assert metrics["total_tokens"] is None
    assert metrics["reported_token_subtotal"] == 12
    assert metrics["tool_calls"] == 1
    assert metrics["agent_duration_ms"] is None
    assert "secret-for-test" not in json.dumps(telemetry.events)


def _result(*, passed=True, seconds=10, tokens=30, failure=None):
    return {
        "task_id": "cmd-01",
        "category": "command",
        "attempt": 1,
        "passed": passed,
        "failure_kind": failure,
        "metrics": {
            "agent_duration_ms": seconds * 1000 if seconds is not None else None,
            "tool_calls": 2,
            "total_tokens": tokens,
        },
    }


def test_summary_retains_failures_and_excludes_missing_measurements():
    results = [_result(), _result(passed=False, seconds=None, tokens=None, failure="environment_error")]
    summary = run.summarize(results, 3)
    assert summary["success_rate"] == 0.5
    assert not summary["complete"]
    assert summary["successful_agent_seconds_mean"] == 10
    assert summary["tokens_mean_when_reported"] == 30
    assert summary["token_coverage"] == 0.5
    assert summary["failures_by_kind"] == {"environment_error": 1}


def test_report_never_generates_final_resume_for_incomplete_run(tmp_path):
    meta = {"suite_version": SUITE_VERSION, "model": "test", "temperature": 0}
    run.write_report(tmp_path, meta, [_result()], 60)
    text = (tmp_path / "resume_metrics.md").read_text(encoding="utf-8")
    assert "尚未完成" in text
    assert "成功率为" not in text


def test_selected_task_resume_does_not_claim_four_categories(tmp_path):
    meta = {"suite_version": SUITE_VERSION, "model": "test", "temperature": 0}
    run.write_report(tmp_path, meta, [_result()], 1)
    text = (tmp_path / "resume_metrics.md").read_text(encoding="utf-8")
    assert "1项" in text and "1次" in text
    assert "数据处理" not in text


def test_real_runtime_with_local_simulated_model_is_not_a_paid_score(tmp_path, monkeypatch):
    # Mock just the system broker factory to avoid modifying real audit/resource policy.
    monkeypatch.setattr("backend.runtime.application.factory._sandbox_runtime", lambda *a, **k: (None, {}))
    from backend.runtime.conversation.service import ConversationService

    original_run = ConversationService.run_task

    def check_workspace_permission(self, *args, **kwargs):
        assert self.runtime.state.permission_mode == "workspace_write"
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(ConversationService, "run_task", check_workspace_permission)
    task = Task("local-test", "file", "创建result.json", {}, (Check("json", "result.json", {"ok": True}),))
    with local_model(
        tool_name="file_operation",
        tool_arguments={"operation": "create", "path": "result.json", "type": "file", "content": '{"ok":true}'},
    ) as (model, requests):
        result = run.run_attempt(task, tmp_path / "attempt", model, {}, timeout_seconds=30)
    assert result["passed"], result
    assert result["metrics"]["model_calls"] == 2
    assert result["metrics"]["total_tokens"] == 30
    assert result["metrics"]["tool_calls"] == 1
    assert result["metrics"]["agent_duration_ms"] > 0
    assert len(requests) == 2
    evidence = json.loads((tmp_path / "attempt" / "events.json").read_text(encoding="utf-8"))
    assert any(event["kind"] == "tool_result" for event in evidence)
    assert model.api_key not in json.dumps(result) and model.api_key not in json.dumps(evidence)


def test_initialization_failure_has_null_duration_and_preserves_seed(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("secret-for-test")

    monkeypatch.setattr(run, "build_application", fail)
    model = ModelConfig("secret-for-test", "http://127.0.0.1/v1", "test")
    result = run.run_attempt(TASKS[5], tmp_path / "attempt", model, {})
    assert not result["passed"]
    assert result["metrics"]["agent_duration_ms"] is None
    assert result["failure_kind"] == "application_error"
    assert model.api_key not in json.dumps(result)
    assert (tmp_path / "attempt" / "workspace" / "config" / "app.json").exists()


def test_unavailable_sandbox_is_failure_without_host_command_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.runtime.application.factory._sandbox_runtime", lambda *a, **k: (None, {}))
    with local_model(tool_name="run_command", tool_arguments={"cmd": "echo SELF_EVAL_CHECK"}) as (model, _):
        result = run.run_attempt(TASKS[0], tmp_path / "attempt", model, {}, timeout_seconds=30)
    assert not result["passed"]
    assert result["failure_kind"] == "environment_error", result
    assert "run_command" not in result["successful_tools"]
    assert result["tool_failures"]
    evidence = json.loads((tmp_path / "attempt" / "events.json").read_text(encoding="utf-8"))
    assert any(event["kind"] == "tool_failed_snapshot" for event in evidence)


def test_cli_does_not_touch_model_for_prepare_list_or_missing_consent(tmp_path, monkeypatch):
    def fail(*args):
        pytest.fail("No model configuration should be accessed")

    monkeypatch.setattr(run, "load_model", fail)
    assert run.main(["--list"]) == 0
    assert run.main(["--run"]) == 2
    assert run.main(["--prepare", "--output", str(tmp_path)]) == 0
    roots = list(tmp_path.glob("run-*"))
    assert len(roots) == 1
    assert len(list((roots[0] / "practice").iterdir())) == 20
    assert (roots[0] / "questions.md").exists()
    assert not (roots[0] / "report.json").exists()


def test_existing_model_settings_read_only(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = tmp_path / "config.toml"
    config.write_text('[runtime]\nterminal_type="cmd"\n', encoding="utf-8")
    db = runtime / "state.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE provider_settings (id INTEGER, provider_configs_json TEXT)")
        connection.execute(
            "INSERT INTO provider_settings VALUES(1,?)",
            (
                json.dumps(
                    [
                        {
                            "id": "test",
                            "is_active": True,
                            "api_key_ciphertext": "cipher",
                            "base_url": "http://127.0.0.1/v1",
                            "model": "test",
                        }
                    ]
                ),
            ),
        )
    old_db = db.read_bytes()
    old_config = config.read_bytes()
    monkeypatch.setattr(run, "decrypt_secret", lambda cipher: "not-a-real-key")
    model, values = run.load_model(tmp_path)
    assert model.model == "test" and values["runtime"]["terminal_type"] == "cmd"
    assert db.read_bytes() == old_db and config.read_bytes() == old_config


@pytest.mark.parametrize("selected", [None, "cmd-01", "full"])
def test_smoke_launcher_runs_only_selected_tasks(tmp_path, monkeypatch, selected):
    import sys

    from self_eval import launch_smoke

    captured = []
    monkeypatch.setattr(launch_smoke, "evaluate", lambda args: captured.extend(args) or 0)
    args = ["launch_smoke", "--receipt-root", str(tmp_path / "receipt"), "--data-root", str(tmp_path / "data")]
    if selected == "full":
        args += ["--full-suite"]
    elif selected:
        args += ["--task", selected]
    monkeypatch.setattr(sys, "argv", args)
    assert launch_smoke.main() == 0
    tasks = [captured[i + 1] for i, value in enumerate(captured) if value == "--task"]
    assert tasks == ([] if selected == "full" else [selected] if selected else ["cmd-01", "file-01", "data-01"])
    assert captured[captured.index("--repeat") + 1] == ("3" if selected == "full" else "1")
