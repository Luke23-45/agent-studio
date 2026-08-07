"""
Contracts (P1): OpenAPI spec file + versioned event/config JSON Schemas.

- contracts/openapi/openapi.v1.json is generated from the live app and
  must be re-exported (`python backend/scripts/export_openapi.py`) when
  routes change — this test pins it to the live spec.
- Event payloads produced by the code validate against the JSON Schemas
  in contracts/events and contracts/schemas.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, ValidationError

from backend.app.adapters.llm import LLMProviderType
from backend.app.application.orchestration import create_orchestration_service
from backend.app.domain.policy import PolicyAction, PolicyRule, PolicySet, PolicyType
from backend.app.domain.tenant import TenantConfig
from backend.app.modules.webhooks import WebhookEnvelope
from backend.tests.gateway_fakes import FakeGateway

ROOT = Path(__file__).resolve().parents[2]
OPENAPI_FILE = ROOT / "contracts" / "openapi" / "openapi.v1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _validate(schema_path: Path, instance: dict) -> None:
    Draft202012Validator(_load(schema_path)).validate(instance)


class TestOpenAPISpec:
    def test_spec_matches_live_app(self):
        from backend.app.main import app

        spec = app.openapi()
        spec["info"]["version"] = "v1"
        rendered = json.dumps(spec, indent=2, default=str)
        assert OPENAPI_FILE.read_text(encoding="utf-8") == rendered

    def test_spec_is_openapi_v3(self):
        spec = _load(OPENAPI_FILE)
        assert spec["openapi"].startswith("3.")
        assert spec["info"]["title"]

    def test_key_paths_present(self):
        spec = _load(OPENAPI_FILE)
        paths = spec["paths"]
        expected = {
            "/api/v1/conversations": {"post"},
            "/api/v1/conversations/stream": {"post"},
            "/api/v1/conversations/{conversation_id}/pause": {"post"},
            "/api/v1/conversations/{conversation_id}/resume": {"post"},
            "/api/v1/tenants": {"get", "post"},
            "/api/v1/tenants/{tenant_id}/models": {"get", "post"},
            "/api/v1/escalations": {"get"},
            "/api/v1/escalations/{escalation_id}/assign": {"post"},
            "/api/v1/escalations/{escalation_id}/resolve": {"post"},
            "/api/v1/webhooks/subscriptions": {"get", "post"},
            "/api/v1/tenants/{tenant_id}/documents": {"post"},
            "/api/v1/eval/replay": {"post"},
            "/api/v1/retention/run": {"post"},
            "/api/v1/tenants/{tenant_id}/gdpr/export": {"get"},
            "/api/v1/audit/export": {"get"},
            "/api/v1/health": {"get"},
        }
        for path, methods in expected.items():
            assert path in paths, f"missing path {path}"
            for method in methods:
                assert method in paths[path], f"missing {method} on {path}"

    def test_conversation_response_schema_includes_citations(self):
        spec = _load(OPENAPI_FILE)
        schemas = spec["components"]["schemas"]
        assert "ConversationResponse" in schemas
        props = schemas["ConversationResponse"]["properties"]
        assert "citations" in props
        assert "faithfulness" in props


class _FakeAdapter:
    provider_type = LLMProviderType.OPENAI

    def __init__(self):
        self.config = SimpleNamespace(model="gpt-4")

    async def chat(self, messages):
        return SimpleNamespace(content="A complete and useful answer for the tenant.")


class TestEventContracts:
    def test_webhook_envelope_matches_schema(self):
        envelope = WebhookEnvelope(
            event_id=str(uuid4()),
            event_type="conversation.completed",
            tenant_id=str(uuid4()),
            data={"session_id": "s1", "handoff_required": False, "confidence": 0.9},
            occurred_at=datetime.now(timezone.utc).isoformat(),
        )
        _validate(ROOT / "contracts" / "events" / "webhook-envelope.schema.json", json.loads(envelope.body()))

    def test_conversation_completed_data_matches_schema(self):
        _validate(
            ROOT / "contracts" / "events" / "conversation.completed.schema.json",
            {"session_id": "s-1", "handoff_required": True, "confidence": 0.32},
        )
        with pytest.raises(ValidationError):
            _validate(
                ROOT / "contracts" / "events" / "conversation.completed.schema.json",
                {"session_id": "s-1", "confidence": 1.5},
            )

    def test_guardrail_blocked_data_matches_schema(self):
        _validate(
            ROOT / "contracts" / "events" / "guardrail.blocked.schema.json",
            {
                "session_id": "s-1",
                "violations": [
                    {
                        "category": "OFF_TOPIC",
                        "severity": "HIGH",
                        "description": "blocked",
                        "metadata": {"classifier_status": "unknown"},
                    }
                ],
            },
        )
        with pytest.raises(ValidationError):
            _validate(
                ROOT / "contracts" / "events" / "guardrail.blocked.schema.json",
                {"session_id": "s-1"},
            )

    def test_sse_stream_events_match_schema(self):
        schema = ROOT / "contracts" / "events" / "sse-stream.schema.json"
        _validate(schema, {"session_id": "s-1", "thread_id": "th-1"})
        _validate(schema, {})
        _validate(schema, {"valid": True, "blocked": False, "violations": []})
        _validate(schema, {"content": "tok"})
        _validate(
            schema,
            {
                "response": "answer",
                "confidence": 0.8,
                "handoff_required": False,
                "citations": [{"source": "refunds.md", "score": 0.9}],
                "faithfulness": {"score": 0.75, "grounded": True, "source_count": 1, "matches": [], "checked": True},
            },
        )
        _validate(schema, {"error": "boom"})
        _validate(schema, {"violations": []})
        # P4-1: retraction is distinguishable from redaction because it
        # requires retract_from_event_id (integer or null).
        _validate(
            schema,
            {"violations": [], "retract_from_event_id": None},
        )
        _validate(
            schema,
            {"violations": [], "retract_from_event_id": 3},
        )
        with pytest.raises(ValidationError):
            _validate(
                schema,
                {"violations": [], "retract_from_event_id": "three"},
            )
        # P4-1: compaction frame is emitted only when a checkpoint exists.
        _validate(schema, {"position": 12, "layer_count": 2})
        with pytest.raises(ValidationError):
            _validate(schema, {"position": 12, "layer_count": "two"})
        with pytest.raises(ValidationError):
            _validate(schema, {"unknown": True})


class TestSchemaContracts:
    def test_evidence_packet_matches_schema(self):
        tenant = TenantConfig(id=uuid4(), name="Acme", slug="acme")
        policy_set = PolicySet(
            id=uuid4(),
            tenant_id=tenant.id,
            name="default",
            version=1,
            rules=[
                PolicyRule(
                    name="block-low-confidence",
                    policy_type=PolicyType.OUTPUT_VALIDATION,
                    action=PolicyAction.ESCALATE,
                    conditions={"confidence": 0.1},
                    priority=5,
                )
            ],
        )
        service = create_orchestration_service(
            tenant_config=tenant, policy_set=policy_set, gateway=FakeGateway()
        )
        service.gateway = FakeGateway(chat_stub=_FakeAdapter())
        records = []
        service.evidence_callback = records.append
        __import__("asyncio").run(
            service.process_message("tell me about refunds", "tell me about refunds", session_id="s1")
        )
        assert records, "evidence packet not emitted"
        packet = records[0]
        _validate(ROOT / "contracts" / "schemas" / "evidence-packet.schema.json", packet)

    def test_tenant_config_payload_matches_schema(self):
        schema = ROOT / "contracts" / "schemas" / "tenant-config.schema.json"
        _validate(
            schema,
            {
                "slug": "acme-co",
                "name": "Acme Co",
                "allowed_topics": ["general"],
                "blocked_topics": ["finance"],
                "escalation_threshold": 0.7,
                "knowledge_allowlist": ["public.md"],
                "default_provider": "openai",
                "default_model": "gpt-4",
                "budgets": {"max_redact_iterations": 2, "max_graph_steps": 50, "max_duration_s": 60},
            },
        )
        with pytest.raises(ValidationError):
            _validate(schema, {"slug": "UPPERCASE"})
        with pytest.raises(ValidationError):
            _validate(schema, {"name": "No slug"})

    def test_tenant_config_hydration_respects_schema_defaults(self):
        from backend.app.modules.tenant_config import tenant_config_from_data

        config = tenant_config_from_data(
            {
                "id": str(uuid4()),
                "name": "Acme",
                "slug": "acme",
                "default_provider": "openai",
                "default_model": "gpt-4",
            }
        )
        assert config.budgets["max_redact_iterations"] == 2.0
        assert config.knowledge_allowlist == []
