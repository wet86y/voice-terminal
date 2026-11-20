from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from voice_terminal import agent_debug
from voice_terminal.application.web import _match_application_operation


class SettingsWrite(BaseModel):
    text: str


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
