"""Managed ports（受管端口）：系统分配、占用回退及实际端点边界。"""
import errno
import socket
from unittest.mock import Mock

import pytest

from voice_terminal.application import ports


@pytest.mark.parametrize("configured", [0, 8765, 8766, 8767, 8768, 3081])
def test_managed_services_request_system_ports_and_standalone_keeps_overrides(monkeypatch, configured):
    monkeypatch.delenv("VOICE_APP_ORIGIN", raising=False)
    assert ports.managed_bind_port(configured) == configured
    monkeypatch.setenv("VOICE_APP_ORIGIN", "http://127.0.0.1:43123")
    assert ports.managed_bind_port(configured) == 0


def test_managed_dsh_does_not_advertise_a_legacy_port_before_binding(monkeypatch):
    from voice_terminal.config import DshConfig
    from voice_terminal.dsh.runtime import DshRuntimeManager
    monkeypatch.setenv("VOICE_APP_ORIGIN", "http://127.0.0.1:43123")
    manager = DshRuntimeManager(DshConfig(port=3081))
    assert manager.status().ui_origin is None


def test_doctor_uses_real_dynamic_binding_even_when_debug_ports_are_busy():
    from types import SimpleNamespace
    from voice_terminal.doctor import _port_allocation_checks
    occupied, port = ports.bind_loopback(0)
    try:
        endpoint = SimpleNamespace(host="127.0.0.1", port=port)
        checks = _port_allocation_checks(SimpleNamespace(server=endpoint, asr_worker=endpoint))
        assert [row.status for row in checks] == ["PASS", "PASS"]
        assert occupied.getsockname()[1] == port
    finally:
        occupied.close()


@pytest.mark.parametrize("value", [True, -1, 65536, "8765"])
def test_managed_policy_does_not_hide_invalid_configuration(monkeypatch, value):
    monkeypatch.setenv("VOICE_APP_ORIGIN", "http://127.0.0.1:43123")
    with pytest.raises(ValueError):
        ports.managed_bind_port(value)


def test_actual_listener_is_owned_until_closed_and_busy_port_falls_back():
    first, port = ports.bind_loopback(0)
    second = None
    try:
        second, other = ports.bind_loopback(port)
        assert port != other
        assert first.getsockname() == ("127.0.0.1", port)
        assert second.getsockname() == ("127.0.0.1", other)
        assert not second.getblocking()
        with socket.socket() as probe:
            with pytest.raises(OSError):
                probe.bind(("127.0.0.1", other))
    finally:
        if second is not None:
            second.close()
        first.close()


@pytest.mark.parametrize("winerror", [10013, 10048])
def test_windows_exclusive_or_reserved_port_retries_zero_and_closes_failed_socket(monkeypatch, winerror):
    blocked, selected = Mock(), Mock()
    error = OSError(errno.EACCES, "测试端口被独占或保留")
    error.winerror = winerror
    blocked.bind.side_effect = error
    selected.getsockname.return_value = ("127.0.0.1", 43210)
    monkeypatch.setattr(ports.socket, "socket", Mock(side_effect=[blocked, selected]))
    listener, port = ports.bind_loopback(8765)
    assert listener is selected and port == 43210
    blocked.close.assert_called_once()
    blocked.bind.assert_called_once_with(("127.0.0.1", 8765))
    selected.bind.assert_called_once_with(("127.0.0.1", 0))


def test_system_port_failure_does_not_loop_or_hide_resource_errors(monkeypatch):
    failed = Mock()
    failed.bind.side_effect = OSError(errno.ENOBUFS, "测试资源不足")
    factory = Mock(return_value=failed)
    monkeypatch.setattr(ports.socket, "socket", factory)
    with pytest.raises(OSError) as caught:
        ports.bind_loopback(0)
    assert caught.value.errno == errno.ENOBUFS
    factory.assert_called_once()
    failed.close.assert_called_once()


@pytest.mark.parametrize("component,path", [("voice", ""), ("asr", "/asr"), ("analysis", "/analysis"), ("speaker", "/speaker")])
def test_zero_is_a_bind_request_and_never_a_connectable_endpoint(component, path):
    scheme = "http" if component == "voice" else "ws"
    value = f"{scheme}://127.0.0.1:0{path}"
    assert ports.preferred_endpoint_url(component, value) is None
    with pytest.raises(ValueError):
        ports.validate_endpoint_url(component, value)


def test_gateway_reports_share_the_voice_owner_but_reject_late_generations(monkeypatch):
    from pathlib import Path
    from voice_terminal.application.supervisor import ApplicationHost
    app = ApplicationHost(Path.cwd())
    app.generations["voice"] = "current"
    monkeypatch.setattr(app, "_endpoint_pid_is_owned", lambda key, pid, created: key == "gateway" and pid == 123)
    report = {"component": "gateway", "generation": "current", "pid": 123,
              "endpoint_url": "http://127.0.0.1:43210"}
    try:
        assert app.report(report)
        assert app.state.snapshot()["endpoints"]["gateway"]["port"] == 43210
        assert not app.report({**report, "generation": "old", "endpoint_url": "http://127.0.0.1:43211"})
        assert app.state.snapshot()["endpoints"]["gateway"]["port"] == 43210
        app.state.clear_endpoint("gateway", generation="next")
        assert not app.state.snapshot()["endpoints"]["gateway"]["url"]
    finally:
        app.outer_job.close()
