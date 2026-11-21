from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from voice_terminal import agent_debug
from voice_terminal.application.web import _match_application_operation


class SettingsWrite(BaseModel):
    text: str


class FutureSettings(BaseModel):
    """模拟后续业务自行新增的字段，调试入口不登记字段名。"""
    enabled: bool
    nested: dict[str, list[str]]


def test_new_production_route_and_fields_are_observed_and_keep_formal_validation(monkeypatch, tmp_path):
    from voice_terminal.config import AppConfig, PersonaConfig
    from voice_terminal.server.product import ProductSettingsStore
    from voice_terminal.server.websocket import SessionRuntime, create_app

    events, saved = [], []

    def exchange(point, stage, value, call_id):
        events.append((point, stage, value, call_id))
        return {"call_id": call_id or 41}

    monkeypatch.setattr(agent_debug, "ENABLED", True)
    monkeypatch.setattr(agent_debug, "_exchange", exchange)
    config = AppConfig(model_dir=tmp_path / "models", persona=PersonaConfig(directory=tmp_path / "persona"))
    store = ProductSettingsStore(path=tmp_path / "config/product-settings.json", config=config)
    runtime = SessionRuntime(config, product_settings=store, runtime_data_dir=tmp_path / "runtime")
    app = create_app(runtime, product_settings=store)

    # 只注册正式业务路由；正式应用工厂自动接入观察和目录，不另写调试路由。
    @app.put("/api/v1/future-settings/{panel}")
    async def new_settings(panel: str, settings: FutureSettings):
        saved.append(settings.model_dump())
        return {"panel": panel, "settings": settings.model_dump()}

    client = TestClient(app)
    body = {"enabled": True, "nested": {"new_field": ["中文值"]}}
    response = client.put("/api/v1/future-settings/current", json=body, headers={"X-Voice-Agent-Call": "41"})
    assert response.status_code == 200
    assert response.json()["settings"] == body
    assert [(name, stage, call) for name, stage, _, call in events] == [
        ("PUT /api/v1/future-settings/{panel}", "input", 41),
        ("PUT /api/v1/future-settings/{panel}", "result", 41),
    ]
    schema = client.get("/openapi.json").json()
    assert "/api/v1/future-settings/{panel}" in schema["paths"]
    assert "nested" in schema["components"]["schemas"]["FutureSettings"]["properties"]
    invalid = client.put("/api/v1/future-settings/current", json={"enabled": {}, "nested": {}},
                         headers={"X-Voice-Agent-Call": "42"})
    assert invalid.status_code == 422
    assert saved == [body]
    assert events[-1][0:2] == ("PUT /api/v1/future-settings/{panel}", "error")
    assert events[-1][3] == 42


def test_registered_route_accepts_debug_input_with_production_validation(monkeypatch):
    events = []

    def exchange(point, stage, value, call_id):
        events.append((point, stage, value, call_id))
        return {"call_id": call_id or 4, **(
            {"replaced": True, "replacement": {**value, "body": {"text": "交接替换"}}}
            if stage == "input" else {}
        )}

    monkeypatch.setattr(agent_debug, "ENABLED", True)
    monkeypatch.setattr(agent_debug, "_exchange", exchange)
    app = FastAPI()
    app.router.route_class = agent_debug.route_class()

    @app.post("/api/v1/test/{item}")
    async def store(item: str, settings: SettingsWrite):
        return {"item": item, "text": settings.text}

    client = TestClient(app)
    response = client.post("/api/v1/test/current", json={"text": "原内容"}, headers={"X-Voice-Agent-Call": "9"})
    assert response.json() == {"item": "current", "text": "交接替换"}
    assert [(point, stage, call) for point, stage, _, call in events] == [
        ("POST /api/v1/test/{item}", "input", 9),
        ("POST /api/v1/test/{item}", "result", 9),
    ]
    assert "/api/v1/test/{item}" in client.get("/openapi.json").json()["paths"]


def test_invalid_replacement_keeps_original_at_typed_boundary(monkeypatch):
    stages = []

    def exchange(point, stage, value, call_id):
        stages.append(stage)
        return {"call_id": 7, "replacement": {"text": False}, "replaced": True}

    monkeypatch.setattr(agent_debug, "ENABLED", True)
    monkeypatch.setattr(agent_debug, "_exchange", exchange)
    assert agent_debug.point_sync("asr.final", "result", {"text": "原内容"},
                                  lambda value: isinstance(value["text"], str)) == ({"text": "原内容"}, 7)
    assert stages == ["result", "rejected"]


def test_application_discovery_uses_same_route_templates_as_dispatch():
    assert _match_application_operation("POST", "/api/v1/application/components/asr/retry") == (
        "POST /api/v1/application/components/{component}/retry", "component_retry",
    )
    assert _match_application_operation("POST", "/api/v1/application/components/asr/other/retry") == ("", "")
