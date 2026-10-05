"""Broker process audit identity regression; no system policy changes or model API."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest

from backend.sandbox.control.broker import WindowsBrokerClient
from backend.sandbox.errors import SandboxInitializationError
from backend.sandbox.native_broker_adapter import process as native
from backend.sandbox.native_windows.audit_policy import SandboxAuditUnavailable
from backend.sandbox.native_windows.permission_audit import CommandAudit

ACCOUNT = "S-1-5-21-1-2-3-1001"
LOGON = "S-1-5-5-10-20"
IDENTITY = {"pid": 4321, "account_sid": ACCOUNT, "logon_sid": LOGON, "logon_id": 0x100000002}


class Handle:
    def __init__(self):
        self.closed = False

    def Close(self):
        self.closed = True


class Security:
    TOKEN_QUERY = 8
    TokenUser = 1
    TokenGroups = 2
    TokenStatistics = 3

    def __init__(self, *, logon_sid=LOGON, logon_id=IDENTITY["logon_id"]):
        self.token = Handle()
        self.logon_sid = logon_sid
        self.logon_id = logon_id
        self.opened = []

    def OpenProcessToken(self, process, access):
        self.opened.append((process, access))
        return self.token

    def GetTokenInformation(self, token, kind):
        assert token is self.token
        if kind == self.TokenUser:
            return ACCOUNT, 0
        if kind == self.TokenGroups:
            return [(self.logon_sid, 0xC0000000)]
        return {"AuthenticationId": self.logon_id}

    @staticmethod
    def ConvertSidToStringSid(sid):
        return sid


def test_real_token_query_uses_creation_handle_and_closes_token(monkeypatch):
    security = Security()
    monkeypatch.setattr(native, "_modules", lambda: {"security": security})
    identity = native._process_audit_identity("creation-handle", 4321, LOGON)
    assert identity == IDENTITY
    assert security.opened == [("creation-handle", security.TOKEN_QUERY)]
    assert security.token.closed


@pytest.mark.parametrize("change", [{"logon_sid": "other"}, {"logon_id": 0}, {"logon_id": -1}])
def test_broker_rejects_wrong_token_identity_and_closes_handle(monkeypatch, change):
    security = Security(**change)
    monkeypatch.setattr(native, "_modules", lambda: {"security": security})
    with pytest.raises(SandboxInitializationError):
        native._process_audit_identity("creation-handle", 4321, LOGON)
    assert security.token.closed


def test_missing_token_fields_fail_closed(monkeypatch):
    security = Security()
    original = security.GetTokenInformation
    security.GetTokenInformation = lambda token, kind: {} if kind == security.TokenStatistics else original(token, kind)
    monkeypatch.setattr(native, "_modules", lambda: {"security": security})
    with pytest.raises(SandboxInitializationError, match="cannot inspect"):
        native._process_audit_identity("creation-handle", 4321, LOGON)
    assert security.token.closed


def test_identity_is_captured_before_resume(monkeypatch):
    order = []
    pipes = iter((Handle(), Handle(), Handle(), Handle(), Handle(), Handle()))
    modules = {
        "api": SimpleNamespace(SetHandleInformation=lambda *a: None, CloseHandle=lambda h: h.Close()),
        "pipe": SimpleNamespace(CreatePipe=lambda *a: (next(pipes), next(pipes))),
        "con": SimpleNamespace(
            HANDLE_FLAG_INHERIT=1, STARTF_USESTDHANDLES=256, CREATE_SUSPENDED=4, CREATE_UNICODE_ENVIRONMENT=1024
        ),
        "process": SimpleNamespace(
            STARTUPINFO=lambda: SimpleNamespace(dwFlags=0),
            CreateProcessAsUser=lambda *a: (Handle(), Handle(), 4321, 123),
            ResumeThread=lambda h: order.append("resume"),
        ),
    }
    monkeypatch.setattr(native, "_modules", lambda: modules)
    monkeypatch.setattr(native, "_object_security_attributes", lambda *a: SimpleNamespace(bInheritHandle=False))

    def identity(handle, pid, logon_sid):
        order.append("identity")
        assert pid == 4321 and logon_sid == LOGON
        return IDENTITY

    monkeypatch.setattr(native, "_process_audit_identity", identity)
    job = SimpleNamespace(assign=lambda h: order.append("assign"))
    process = native._NativeWindowsProcess.launch(
        "token",
        ["cmd.exe", "/c", "echo ok"],
        "C:/workspace",
        {},
        job,
        logon_sid=LOGON,
        service_sid="service",
        desktop_name="private",
    )
    assert order == ["assign", "identity", "resume"]
    assert process.audit_identity == IDENTITY


def test_backend_binds_verified_identity_without_openprocess(monkeypatch):
    import win32api

    monkeypatch.setattr(win32api, "OpenProcess", lambda *a: pytest.fail("Backend must not reopen private process"))
    audit = CommandAudit(None, ACCOUNT, 0, 10)
    audit.bind_process(4321, LOGON, IDENTITY)
    assert audit.root_pid == 4321 and audit.logon_id == IDENTITY["logon_id"]


@pytest.mark.parametrize(
    "identity",
    [
        None,
        {},
        "identity",
        {**IDENTITY, "pid": 4322},
        {**IDENTITY, "pid": True},
        {**IDENTITY, "account_sid": "other"},
        {**IDENTITY, "logon_sid": "other"},
        {**IDENTITY, "logon_id": True},
        {**IDENTITY, "logon_id": "123"},
        {**IDENTITY, "logon_id": 0},
        {**IDENTITY, "logon_id": 2**64},
    ],
)
def test_backend_rejects_missing_mismatched_or_invalid_identity(identity):
    audit = CommandAudit(None, ACCOUNT, 0, 10)
    with pytest.raises(SandboxAuditUnavailable):
        audit.bind_process(4321, LOGON, identity)
    assert audit.root_pid == 0 and audit.logon_id == 0


def test_authenticated_launch_identity_reaches_process_proxy():
    key = b"test-only-key"

    def transport(payload):
        request = json.loads(payload)
        response = {
            "nonce": request["nonce"],
            "accepted": True,
            "pid": 4321,
            "process_id": "process-1",
            "audit_identity": IDENTITY,
        }
        response["hmac"] = hmac.new(
            key, json.dumps(response, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest()
        return json.dumps(response).encode()

    client = WindowsBrokerClient(installation_key=key, transport=transport, is_windows=True)
    process = client.launch(
        argv=["cmd.exe", "/c", "echo ok"],
        cwd="C:/workspace",
        environment={},
        reservation_id="reservation",
        policy_hash="policy",
        capability_digest="digest",
        user_id="user",
    )
    assert process.audit_identity == IDENTITY
    audit = CommandAudit(None, ACCOUNT, 0, 10)
    audit.bind_process(process.pid, LOGON, process.audit_identity)
    assert audit.root_pid == 4321
