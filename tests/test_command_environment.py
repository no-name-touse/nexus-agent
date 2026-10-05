"""Command facts reach the model before generation without changing tool policy."""

import json
import os
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
import requests

from backend.domain import AssistantMessage, SystemMessage, ToolSpec
from backend.planning import LLMPlanner
from backend.planning.llm.requests import RequestMixin
from backend.providers import LLMClient, ModelConfig
from backend.runtime import AgentRunner, PreparedResponse
from backend.tools import ToolError, ToolRegistry, WorkspaceCommand, build_tool_registry
from backend.tools.default_tools.command import command_tool
from tests.test_tools import FakeProcess


@pytest.mark.parametrize("terminal", ["cmd", "powershell", "pwsh", "git_bash", "wsl"])
def test_windows_description_metadata_and_launch_agree(tmp_path, monkeypatch, terminal):
    executable = f"{terminal}-test.exe"
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: executable)
    command = WorkspaceCommand(tmp_path, is_windows=True, terminal_type=terminal)
    spec = command_tool(command).spec
    facts = spec.provider_options["praxis"]["execution_environment"]
    assert facts["terminal_type"] == terminal
    assert facts["shell"] == {"git_bash": "bash", "wsl": "sh"}.get(terminal, terminal)
    assert facts["host_os"] == "windows"
    assert facts["cwd"] == str(tmp_path.resolve())
    assert facts["executable"] == executable
    assert facts["executable_resolution"] == "unverified"
    assert terminal in spec.description and executable in spec.description
    assert spec.parameters["properties"]["cmd"]["description"] == facts["syntax_guidance"]
    if terminal == "wsl" and os.name != "nt":
        assert facts["shell_cwd"] == "(workspace cannot be mapped to WSL)"
    else:
        assert command._command_line("echo hi")[0] == executable


def test_posix_reports_actual_bash_instead_of_configured_cmd(tmp_path):
    command = WorkspaceCommand(tmp_path, is_windows=False, terminal_type="cmd")
    facts = command.execution_environment()
    assert facts["host_os"] == "posix"
    assert facts["terminal_type"] == facts["shell"] == "bash"
    assert facts["executable"] == command._command_line("echo hi")[0] == "bash"
    assert "Terminal: bash" in command_tool(command).description


def test_executable_is_frozen_for_description_and_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: "first.exe")
    first = WorkspaceCommand(tmp_path, is_windows=True)
    spec = command_tool(first).spec
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: "second.exe")
    assert first._command_line("echo hi")[0] == "first.exe"
    assert spec.provider_options["praxis"]["execution_environment"]["executable"] == "first.exe"
    second = WorkspaceCommand(tmp_path, is_windows=True, terminal_type="pwsh")
    assert second.execution_environment()["terminal_type"] == "pwsh"
    assert second._command_line("echo hi")[0] == "second.exe"


def test_unavailable_executable_is_not_advertised_as_available(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: None)
    command = WorkspaceCommand(tmp_path, is_windows=True, terminal_type="git_bash")
    assert command.execution_environment()["executable_resolution"] == "unavailable"
    assert "(unavailable)" in command_tool(command).description
    with pytest.raises(ToolError, match="not available"):
        command._command_line("echo hi")


def test_existing_executable_is_reported_without_process_probe(tmp_path, monkeypatch):
    executable = Path(__file__).resolve()
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: str(executable))
    command = WorkspaceCommand(tmp_path, is_windows=True)
    # Existence is only a resolution fact, not a claim that this file launches.
    assert command.execution_environment()["executable_resolution"] == "resolved_file"


def test_metadata_is_sanitized_and_preserves_trace_origin_and_policy(tmp_path):
    command = WorkspaceCommand(tmp_path, environment={"API_KEY": "secret-test-marker", "OTHER": "private-value"})
    tool = command_tool(command)
    spec = replace(tool, trace_origin={"source": "local"}).spec
    assert spec.provider_options["praxis"]["trace_origin"] == {"source": "local"}
    assert "secret-test-marker" not in json.dumps(spec.provider_options)
    assert "private-value" not in json.dumps(spec.provider_options)
    assert tool.requires_confirmation and not tool.read_only and tool.workspace_confined


def test_project_cwd_is_the_command_cwd(tmp_path):
    project = tmp_path / "project"
    registry = build_tool_registry(tmp_path, project_workspace=project)
    spec = next(spec for spec in registry.specs() if spec.name == "run_command")
    assert spec.provider_options["praxis"]["execution_environment"]["cwd"] == str(project.resolve())


@pytest.mark.parametrize("specs", [None, [], [ToolSpec("run_command", "legacy")], [ToolSpec("read_file", "read")]])
def test_no_command_metadata_does_not_fabricate_environment(specs):
    system = SystemMessage(content="original", provider_options={"chat_completions": {"test": 1}})
    assert RequestMixin._with_command_environment(system, specs) is system


def test_prompt_preserves_system_options_and_whitelists_metadata(tmp_path):
    tool = command_tool(WorkspaceCommand(tmp_path, is_windows=True, environment={}))
    spec = tool.spec
    spec.provider_options["praxis"]["execution_environment"]["API_KEY"] = "secret-test-marker"
    system = SystemMessage(name="policy", content="rules", provider_options={"chat_completions": {"test": 1}})
    result = RequestMixin._with_command_environment(system, [spec])
    assert result.name == system.name and result.provider_options == system.provider_options
    assert "secret-test-marker" not in result.content
    assert result.content.startswith("rules")


def test_runtime_serialization_retains_command_environment(tmp_path):
    tool = command_tool(WorkspaceCommand(tmp_path, is_windows=True, environment={}))
    registry = ToolRegistry([tool])
    planner = LLMPlanner(RecordingClient(), registry.specs(), registry.read_only_specs())
    runtime = AgentRunner(planner, registry).new_runtime(task="inspect")
    runtime.state.tool_specs = registry.specs()
    restored = type(runtime.state).from_dict(runtime.state.to_dict())
    assert restored.tool_specs[0].provider_options == tool.spec.provider_options


class RecordingClient:
    def __init__(self):
        self.requests = []

    def run(self, runtime):
        self.requests.append(list(runtime.exchange.messages))
        return PreparedResponse(AssistantMessage(content="done"))


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_context_reaches_decision_before_generation_only_with_allowed_command(tmp_path, mode):
    tool = command_tool(WorkspaceCommand(tmp_path, is_windows=True, terminal_type="cmd", environment={}))
    registry = ToolRegistry([tool])
    client = RecordingClient()
    planner = LLMPlanner(client, registry.specs(), registry.read_only_specs())
    runtime = AgentRunner(planner, registry).new_runtime(task="calculate", mode=mode)
    planner.decide(runtime)
    system = client.requests[0][0].content
    assert ("## Command execution environment" in system) is (mode == "agent")
    if mode == "agent":
        assert '"terminal_type": "cmd"' in system
        assert "not PowerShell or Bash" in system
        assert "do not" in system.lower()
        assert runtime.exchange.context["trace_system_message"] == system


def test_executor_starts_the_same_executable_as_model_description(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("backend.tools.command.terminal_executable", lambda *_args, **_kwargs: "cmd.exe")

    def factory(args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess(stdout="5050\n")

    command = WorkspaceCommand(tmp_path, is_windows=True, terminal_type="cmd", popen_factory=factory)
    spec = command_tool(command).spec
    output = json.loads(command.run('python -c "print(sum(range(1,101)))"'))
    assert output["output"].strip() == "5050"
    assert calls[0][0][0] == spec.provider_options["praxis"]["execution_environment"]["executable"]
    assert calls[0][1]["cwd"] == spec.provider_options["praxis"]["execution_environment"]["cwd"]


def test_real_loopback_provider_receives_context_without_internal_wire_options(tmp_path):
    captured_requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured_requests.append(payload)
            response = {
                "id": "local-test",
                "model": "local-test",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
            if payload.get("stream"):
                chunk = {
                    "id": "local-test",
                    "model": "local-test",
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}
                    ],
                }
                body = (f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                body = json.dumps(response).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    session = requests.Session()
    session.trust_env = False
    client = LLMClient(
        ModelConfig("local-dummy", f"http://127.0.0.1:{server.server_port}/v1", "local-test"), session=session
    )
    registry = ToolRegistry([command_tool(WorkspaceCommand(tmp_path, is_windows=True, environment={}))])
    planner = LLMPlanner(client, registry.specs(), registry.read_only_specs())
    runtime = AgentRunner(planner, registry).new_runtime(task="inspect command environment")
    try:
        assert planner.decide(runtime).content == "done"
        assert len(captured_requests) == 1
        system = captured_requests[0]["messages"][0]["content"]
        assert '"terminal_type": "cmd"' in system
        assert "not PowerShell or Bash" in system
        function = captured_requests[0]["tools"][0]["function"]
        assert "Terminal: cmd" in function["description"]
        assert "praxis" not in function and "execution_environment" not in function
        assert set(function) == {"name", "description", "parameters"}
    finally:
        session.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
