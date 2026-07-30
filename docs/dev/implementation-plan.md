# Neryva Implementation Plan - Phased Roadmap

**Status:** Complete implementation backlog with phase assignments
**Last Updated:** Current session
**Scope:** End-to-end production implementation for enterprise deployment

---

## Executive Summary

This document breaks down the complete Neryva Agent Studio implementation into four phases, with detailed task lists, file paths, and dependencies. OpenCode is used internally for development but is not part of the production runtime.

**Total Estimated Effort:** 9-13 weeks to full production readiness
**Pilot-Ready MVP:** 4-6 weeks (end of Phase 2)

---

## Phase 1: Config Plane + Core Loop (Weeks 1-3)

**Goal:** Single-tenant functional backend with basic orchestration, policy enforcement, and output validation.

### 1.1 Backend Foundation

#### Domain Layer (Complete: 100%)
- [x] `backend/app/domain/tenant/` - Tenant entity and value objects
- [x] `backend/app/domain/policy/` - Policy rules and constraints
- [x] `backend/app/domain/workflow/` - Workflow definitions
- [x] `backend/app/domain/conversation/` - Conversation state models
- [x] `backend/app/domain/knowledge/` - Knowledge base entities
- [x] `backend/app/domain/safety/` - Safety classification types
- [x] `backend/app/domain/pii/` - PII categories and redaction rules

#### Application Layer (Complete: 40%)
- [ ] `backend/app/application/orchestration/agent_service.py` - Main orchestration service using LangGraph
- [ ] `backend/app/application/orchestration/state_machine.py` - Conversation state machine
- [ ] `backend/app/application/orchestration/tool_router.py` - Tool call routing logic
- [ ] `backend/app/application/retrieval/retriever.py` - RAG retrieval with filtering
- [ ] `backend/app/application/retrieval/context_builder.py` - Context assembly with spotlighting
- [ ] `backend/app/application/validation/output_validator.py` - Schema and policy validation
- [ ] `backend/app/application/validation/confidence_scorer.py` - Confidence threshold logic
- [x] `backend/app/application/handoff/handoff_service.py` - Escalation logic (COMPLETE)
- [ ] `backend/app/application/ingestion/document_processor.py` - Document parsing and chunking
- [ ] `backend/app/application/ingestion/embedding_pipeline.py` - Embedding generation pipeline

#### Adapters Layer (Complete: 20%)
- [ ] `backend/app/adapters/llm/base_provider.py` - Abstract base provider interface
- [ ] `backend/app/adapters/llm/anthropic_adapter.py` - Claude provider implementation
- [ ] `backend/app/adapters/llm/openai_adapter.py` - GPT provider implementation
- [ ] `backend/app/adapters/llm/google_adapter.py` - Gemini provider implementation
- [ ] `backend/app/adapters/llm/provider_factory.py` - Provider selection factory
- [ ] `backend/app/adapters/vectorstore/pgvector_adapter.py` - pgvector implementation
- [ ] `backend/app/adapters/vectorstore/qdrant_adapter.py` - Qdrant implementation
- [ ] `backend/app/adapters/vectorstore/vectorstore_factory.py` - Vector store factory
- [ ] `backend/app/adapters/dlp/presidio_adapter.py` - Presidio PII detection
- [ ] `backend/app/adapters/dlp/cloud_dlp_adapter.py` - Cloud DLP integration (optional)
- [ ] `backend/app/adapters/tracing/langfuse_adapter.py` - Langfuse observability
- [ ] `backend/app/adapters/ticketing/webhook_adapter.py` - Webhook-based ticketing
- [ ] `backend/app/adapters/ticketing/zendesk_adapter.py` - Zendesk integration (optional)
- [ ] `backend/app/adapters/ticketing/servicenow_adapter.py` - ServiceNow integration (optional)
- [ ] `backend/app/adapters/auth/api_key_auth.py` - API key authentication
- [ ] `backend/app/adapters/auth/oauth_adapter.py` - OAuth2 authentication (optional)

#### Infrastructure Layer (Complete: 0%)
- [ ] `backend/app/infrastructure/db/postgres_client.py` - PostgreSQL connection pool
- [ ] `backend/app/infrastructure/db/migrations/` - Database migration scripts
- [ ] `backend/app/infrastructure/cache/redis_client.py` - Redis connection and caching
- [ ] `backend/app/infrastructure/queue/redis_queue.py` - Job queue implementation
- [ ] `backend/app/infrastructure/storage/s3_client.py` - S3-compatible object storage
- [ ] `backend/app/infrastructure/storage/local_storage.py` - Local file storage (dev only)

#### Modules Layer (Complete: 60%)
- [x] `backend/app/modules/escalation/` - Handoff and escalation (COMPLETE)
- [ ] `backend/app/modules/guardrails/nemo_integration.py` - NeMo Guardrails integration
- [ ] `backend/app/modules/guardrails/guardrails_ai_integration.py` - Guardrails AI integration
- [ ] `backend/app/modules/guardrails/jailbreak_detector.py` - Jailbreak detection scanner
- [ ] `backend/app/modules/guardrails/topic_classifier.py` - Topic relevance classifiers
- [ ] `backend/app/modules/guardrails/rail_compiler.py` - Rail configuration compiler
- [ ] `backend/app/modules/rag/retrieval_pipeline.py` - Full RAG pipeline
- [ ] `backend/app/modules/rag/spotlighting.py` - Spotlighting delimiters for retrieved content
- [ ] `backend/app/modules/rag/reranker.py` - Re-ranking for retrieval results
- [ ] `backend/app/modules/observability/tracing_config.py` - Tracing configuration
- [ ] `backend/app/modules/observability/metrics_collector.py` - Metrics aggregation
- [ ] `backend/app/modules/tenant_config/config_loader.py` - Tenant config resolution
- [ ] `backend/app/modules/tenant_config/config_versioning.py` - Config version management
- [ ] `backend/app/modules/tenant_config/policy_compiler.py` - Policy compilation to rails

#### API Layer (Complete: 0%)
- [ ] `backend/app/api/routes/health.py` - Health check endpoints
- [ ] `backend/app/api/routes/chat.py` - Chat message endpoints
- [ ] `backend/app/api/routes/tenants.py` - Tenant management endpoints
- [ ] `backend/app/api/routes/policies.py` - Policy CRUD endpoints
- [ ] `backend/app/api/routes/workflows.py` - Workflow management endpoints
- [ ] `backend/app/api/routes/knowledge.py` - Knowledge base management endpoints
- [ ] `backend/app/api/routes/escalations.py` - Escalation queue endpoints
- [ ] `backend/app/api/routes/traces.py` - Trace and observability endpoints
- [ ] `backend/app/api/routes/evaluations.py` - Evaluation results endpoints
- [ ] `backend/app/api/dependencies/auth.py` - Authentication dependencies
- [ ] `backend/app/api/dependencies/tenant_resolver.py` - Tenant context resolver
- [ ] `backend/app/api/middleware/request_logging.py` - Request logging middleware
- [ ] `backend/app/api/middleware/error_handler.py` - Global error handling
- [ ] `backend/app/api/middleware/cors_config.py` - CORS configuration

#### Settings (Complete: 0%)
- [ ] `backend/app/settings/env.py` - Environment variable loading
- [ ] `backend/app/settings/feature_flags.py` - Feature flag configuration
- [ ] `backend/app/settings/security_config.py` - Security settings
- [ ] `backend/app/settings/llm_config.py` - LLM provider configurations

### 1.2 Contracts and Schemas

#### OpenAPI Specs (Complete: 0%)
- [ ] `contracts/openapi/neryva-api-v1.yaml` - Main API specification
- [ ] `contracts/openapi/webhook-schemas.yaml` - Webhook payload schemas
- [ ] `contracts/openapi/ticketing-integration.yaml` - Ticketing system integration spec

#### Event Schemas (Complete: 0%)
- [ ] `contracts/events/conversation_events.json` - Conversation lifecycle events
- [ ] `contracts/events/escalation_events.json` - Escalation workflow events
- [ ] `contracts/events/policy_events.json` - Policy change events
- [ ] `contracts/events/knowledge_events.json` - Knowledge base events

#### JSON Schemas (Complete: 0%)
- [ ] `contracts/schemas/tenant-config-schema.json` - Tenant configuration schema
- [ ] `contracts/schemas/policy-schema.json` - Policy rule schema
- [ ] `contracts/schemas/workflow-schema.json` - Workflow definition schema
- [ ] `contracts/schemas/message-schema.json` - Chat message schema
- [ ] `contracts/schemas/tool-call-schema.json` - Tool call schema
- [ ] `contracts/schemas/escalation-schema.json` - Escalation request schema

### 1.3 Backend Entry Points

#### Main Application (Complete: 0%)
- [ ] `backend/app/main.py` - FastAPI application entry point
- [ ] `backend/app/lifespan.py` - Application lifespan handlers
- [ ] `backend/app/factory.py` - Application factory for testing

#### Worker Entry Points (Complete: 0%)
- [ ] `worker/src/main.py` - Worker process entry point
- [ ] `worker/src/job_runner.py` - Job execution runner

### 1.4 Configuration Files

#### Backend Configuration (Complete: 0%)
- [ ] `backend/pyproject.toml` - Python dependencies and project metadata
- [ ] `backend/Dockerfile` - Backend container build
- [ ] `backend/docker-compose.yml` - Local development compose
- [ ] `backend/.env.example` - Example environment variables

#### Root Configuration (Complete: 0%)
- [ ] `pyproject.toml` - Monorepo root configuration
- [ ] `package.json` - Node.js workspace configuration
- [ ] `pnpm-workspace.yaml` - PNPM workspace definition
- [ ] `.pre-commit-config.yaml` - Pre-commit hooks
- [ ] `.editorconfig` - Editor configuration
- [ ] `README.md` - Project overview and setup

### 1.5 Testing Foundation

#### Test Infrastructure (Complete: 0%)
- [ ] `backend/tests/conftest.py` - Pytest fixtures and configuration
- [ ] `backend/tests/unit/` - Unit test directory structure
- [ ] `backend/tests/integration/` - Integration test directory structure
- [ ] `backend/tests/mocks/` - Mock objects and stubs
- [ ] `backend/tests/factories/` - Test data factories

#### Initial Test Coverage (Complete: 0%)
- [ ] `backend/tests/unit/domain/` - Domain layer unit tests
- [ ] `backend/tests/unit/application/` - Application layer unit tests
- [ ] `backend/tests/integration/api/` - API integration tests
- [ ] `backend/tests/integration/adapters/` - Adapter integration tests

---

## Phase 2: Guardrails Hardening (Weeks 4-6)

**Goal:** Production-ready guardrail stack with injection detection, PII handling, and RAG security.

### 2.1 Guardrails Module Completion

#### NeMo Guardrails Integration (Complete: 0%)
- [ ] `backend/app/modules/guardrails/nemo_rails_config.py` - NeMo configuration builder
- [ ] `backend/app/modules/guardrails/colang_templates/` - Colang rail templates
- [ ] `backend/app/modules/guardrails/dialog_rails.py` - Dialog flow control rails
- [ ] `backend/app/modules/guardrails/topic_rails.py` - Topic restriction rails
- [ ] `backend/app/modules/guardrails/safety_rails.py` - Safety policy rails
- [ ] `backend/app/modules/guardrails/rail_execution_engine.py` - Rail execution engine

#### Guardrails AI Integration (Complete: 0%)
- [ ] `backend/app/modules/guardrails/guardrails_validators.py` - Custom validator definitions
- [ ] `backend/app/modules/guardrails/output_parsers.py` - Output parsing with validation
- [ ] `backend/app/modules/guardrails/schema_enforcement.py` - JSON schema enforcement
- [ ] `backend/app/modules/guardrails/retry_logic.py` - Validation retry and fallback logic

#### Classifier Layer (Complete: 0%)
- [ ] `backend/app/modules/guardrails/classifier_loader.py` - Model loading for classifiers
- [ ] `backend/app/modules/guardrails/off_topic_detector.py` - Off-topic detection with jina-embeddings
- [ ] `backend/app/modules/guardrails/relevance_scorer.py` - Relevance scoring with stsb-roberta
- [ ] `backend/app/modules/guardrails/sentiment_analyzer.py` - Sentiment analysis for triage
- [ ] `backend/app/modules/guardrails/regex_fastpath.py` - Regex-based first-pass filtering
- [ ] `backend/app/modules/guardrails/ensemble_classifier.py` - Ensemble decision logic

#### Jailbreak Detection (Complete: 0%)
- [ ] `backend/app/modules/guardrails/jailbreak_patterns.py` - Known jailbreak pattern library
- [ ] `backend/app/modules/guardrails/prompt_injection_scanner.py` - Injection detection scanner
- [ ] `backend/app/modules/guardrails/adversarial_detector.py` - Adversarial input detection
- [ ] `backend/app/modules/guardrails/threat_classification.py` - Threat severity classification

### 2.2 PII Layer Enhancement

#### Presidio Integration (Complete: 0%)
- [ ] `backend/app/modules/pii/presidio_analyzer.py` - Presidio analyzer wrapper
- [ ] `backend/app/modules/pii/presidio_redactor.py` - Presidio redactor wrapper
- [ ] `backend/app/modules/pii/custom_recognizers.py` - Custom PII recognizers
- [ ] `backend/app/modules/pii/pii_policy_config.py` - PII policy configuration per tenant
- [ ] `backend/app/modules/pii/redaction_strategies.py` - Redaction strategy implementations
- [ ] `backend/app/modules/pii/pii_audit_logger.py` - PII detection audit logging

#### Defense in Depth (Complete: 0%)
- [ ] `backend/app/modules/pii/cloud_dlp_integration.py` - Cloud DLP integration (optional)
- [ ] `backend/app/modules/pii/pii_validation_pipeline.py` - Multi-stage PII validation
- [ ] `backend/app/modules/pii/trace_redaction.py` - PII redaction for traces and logs

### 2.3 RAG Security

#### Retrieval Filtering (Complete: 0%)
- [ ] `backend/app/modules/rag/retrieval_filters.py` - Pre-retrieval access filters
- [ ] `backend/app/modules/rag/knowledge_allowlists.py` - Knowledge source allowlists
- [ ] `backend/app/modules/rag/content_sanitizer.py` - Retrieved content sanitization
- [ ] `backend/app/modules/rag/source_verification.py` - Source trust verification

#### Spotlighting Implementation (Complete: 0%)
- [ ] `backend/app/modules/rag/spotlight_delimiters.py` - Spotlight delimiter insertion
- [ ] `backend/app/modules/rag/context_injection_detection.py` - Indirect injection detection
- [ ] `backend/app/modules/rag/retrieval_confidence.py` - Retrieval quality confidence scoring

#### RAG Evaluation (Complete: 0%)
- [ ] `backend/app/modules/rag/ragas_integration.py` - RAGAS metrics integration
- [ ] `backend/app/modules/rag/faithfulness_checker.py` - Faithfulness evaluation
- [ ] `backend/app/modules/rag/relevance_evaluator.py` - Relevance evaluation
- [ ] `backend/app/modules/rag/answer_quality_scorer.py` - Answer quality scoring

### 2.4 Red-Teaming Infrastructure

#### Garak Configuration (Complete: 0%)
- [ ] `evals/garak/neryva_config.yaml` - Garak configuration for Neryva
- [ ] `evals/garak/custom_probes/` - Custom probe definitions
- [ ] `evals/garak/tenant_specific_probes/` - Tenant-specific probe sets
- [ ] `evals/garak/garak_runner.py` - Garak execution wrapper

#### PyRIT Configuration (Complete: 0%)
- [ ] `evals/pyrith/neryva_scenarios.yaml` - PyRIT scenario definitions
- [ ] `evals/pyrith/multi_turn_attacks/` - Multi-turn attack scenarios
- [ ] `evals/pyrith/crescendo_config.yaml` - Crescendo-style attack configuration
- [ ] `evals/pyrith/pyrith_runner.py` - PyRIT execution wrapper

#### Test Corpora (Complete: 0%)
- [ ] `evals/datasets/injection_prompts.txt` - Prompt injection test corpus
- [ ] `evals/datasets/jailbreak_prompts.txt` - Jailbreak attempt corpus
- [ ] `evals/datasets/pii_test_data.txt` - PII detection test data
- [ ] `evals/datasets/off_topic_queries.txt` - Off-topic detection test data
- [ ] `evals/datasets/golden_responses.json` - Golden response set for regression

#### Regression Testing (Complete: 0%)
- [ ] `evals/prompts/regression_suite.yaml` - Regression test prompt suite
- [ ] `evals/scripts/run_regression_tests.py` - Regression test runner
- [ ] `evals/scripts/compare_eval_results.py` - Evaluation result comparison tool

### 2.5 Llama Guard Integration (Optional)

#### Classifier Setup (Complete: 0%)
- [ ] `backend/app/modules/guardrails/llama_guard_loader.py` - Llama Guard 4 model loading
- [ ] `backend/app/modules/guardrails/llama_guard_classifier.py` - Llama Guard classification wrapper
- [ ] `backend/app/modules/guardrails/safety_categories.py` - Safety category mappings
- [ ] `backend/app/modules/guardrails/llama_guard_orchestrator.py` - Integration with main guardrail stack

---

## Phase 3: Escalation + Observability + UI (Weeks 7-10)

**Goal:** Full operational visibility, human handoff workflows, and admin interfaces.

### 3.1 Observability Stack

#### Langfuse Integration (Complete: 0%)
- [ ] `backend/app/modules/observability/langfuse_client.py` - Langfuse client setup
- [ ] `backend/app/modules/observability/trace_decorators.py` - Tracing decorators for services
- [ ] `backend/app/modules/observability/span_management.py` - Span creation and management
- [ ] `backend/app/modules/observability/session_tracking.py` - Session-level tracking
- [ ] `backend/app/modules/observability/metric_exporters.py` - Metrics export configuration
- [ ] `backend/app/modules/observability/alert_rules.py` - Alert rule definitions

#### Trace Enrichment (Complete: 0%)
- [ ] `backend/app/modules/observability/trace_enrichment.py` - Adding context to traces
- [ ] `backend/app/modules/observability/guardrail_events.py` - Guardrail event logging
- [ ] `backend/app/modules/observability/pii_events.py` - PII detection event logging
- [ ] `backend/app/modules/observability/escalation_events.py` - Escalation event logging

#### Dashboard Data (Complete: 0%)
- [ ] `backend/app/api/routes/analytics.py` - Analytics data endpoints
- [ ] `backend/app/modules/observability/dashboard_aggregator.py` - Dashboard data aggregation
- [ ] `backend/app/modules/observability/report_generator.py` - Report generation

### 3.2 Worker Background Jobs

#### Job Definitions (Complete: 0%)
- [ ] `worker/src/jobs/eval_replay_job.py` - Eval replay execution
- [ ] `worker/src/jobs/redteam_job.py` - Red-team run execution
- [ ] `worker/src/jobs/document_ingestion_job.py` - Document processing pipeline
- [ ] `worker/src/jobs/cleanup_job.py` - Retention and cleanup tasks
- [ ] `worker/src/jobs/notification_job.py` - Notification delivery
- [ ] `worker/src/jobs/metrics_aggregation_job.py` - Metrics aggregation

#### Job Orchestration (Complete: 0%)
- [ ] `worker/src/pipelines/ingestion_pipeline.py` - Document ingestion pipeline
- [ ] `worker/src/pipelines/eval_pipeline.py` - Evaluation pipeline
- [ ] `worker/src/pipelines/redteam_pipeline.py` - Red-team pipeline
- [ ] `worker/src/consumers/job_consumer.py` - Job queue consumer
- [ ] `worker/src/schedulers/job_scheduler.py` - Job scheduling logic

#### Job Management API (Complete: 0%)
- [ ] `backend/app/api/routes/jobs.py` - Job management endpoints
- [ ] `backend/app/application/jobs/job_submitter.py` - Job submission service
- [ ] `backend/app/application/jobs/job_status_tracker.py` - Job status tracking

### 3.3 Frontend Admin UI

#### Project Setup (Complete: 0%)
- [ ] `frontend/package.json` - Frontend dependencies
- [ ] `frontend/vite.config.ts` - Vite configuration
- [ ] `frontend/tsconfig.json` - TypeScript configuration
- [ ] `frontend/index.html` - HTML entry point
- [ ] `frontend/src/main.tsx` - React entry point
- [ ] `frontend/src/styles/global.css` - Global styles

#### App Structure (Complete: 0%)
- [ ] `frontend/src/app/routes.tsx` - Route definitions (TanStack Router)
- [ ] `frontend/src/app/providers.tsx` - Context providers
- [ ] `frontend/src/app/layout.tsx` - Main layout component
- [ ] `frontend/src/app/error_boundary.tsx` - Error boundary component

#### Feature Modules (Complete: 0%)
- [ ] `frontend/src/features/tenants/` - Tenant management feature
- [ ] `frontend/src/features/policies/` - Policy editor feature
- [ ] `frontend/src/features/workflows/` - Workflow editor feature
- [ ] `frontend/src/features/knowledge/` - Knowledge base management feature
- [ ] `frontend/src/features/traces/` - Trace viewer feature
- [ ] `frontend/src/features/evaluations/` - Evaluation results feature
- [ ] `frontend/src/features/handoffs/` - Escalation queue feature
- [ ] `frontend/src/features/analytics/` - Analytics dashboard feature

#### Components (Complete: 0%)
- [ ] `frontend/src/components/ui/` - Reusable UI components
- [ ] `frontend/src/components/forms/` - Form components
- [ ] `frontend/src/components/charts/` - Chart and visualization components
- [ ] `frontend/src/components/layouts/` - Layout components

#### Client Libraries (Complete: 0%)
- [ ] `frontend/src/lib/api/client.ts` - API client setup
- [ ] `frontend/src/lib/api/endpoints.ts` - API endpoint definitions
- [ ] `frontend/src/lib/auth/auth_provider.tsx` - Authentication provider
- [ ] `frontend/src/lib/auth/hooks.ts` - Auth hooks
- [ ] `frontend/src/lib/state/store.ts` - State management setup
- [ ] `frontend/src/lib/state/slices/` - State slices per feature
- [ ] `frontend/src/lib/utils/formatters.ts` - Utility functions
- [ ] `frontend/src/lib/utils/validators.ts` - Client-side validators

### 3.4 Customer Chat Widget

#### Widget Core (Complete: 0%)
- [ ] `widget/package.json` - Widget dependencies
- [ ] `widget/vite.config.ts` - Widget build configuration
- [ ] `widget/src/runtime/session_manager.ts` - Session management
- [ ] `widget/src/runtime/message_handler.ts` - Message handling logic
- [ ] `widget/src/api/widget_api.ts` - Widget API client
- [ ] `widget/src/transport/sse_transport.ts` - SSE transport implementation
- [ ] `widget/src/transport/websocket_transport.ts` - WebSocket transport (optional)

#### Widget UI (Complete: 0%)
- [ ] `widget/src/ui/chat_interface.tsx` - Main chat interface
- [ ] `widget/src/ui/message_list.tsx` - Message display component
- [ ] `widget/src/ui/input_box.tsx` - User input component
- [ ] `widget/src/ui/loading_states.tsx` - Loading indicators
- [ ] `widget/src/ui/error_states.tsx` - Error display components
- [ ] `widget/src/ui/theme_config.ts` - Theme customization

#### Widget Embedding (Complete: 0%)
- [ ] `widget/src/embedding/embed_script.ts` - Embeddable script
- [ ] `widget/src/embedding/shadow_dom.ts` - Shadow DOM isolation
- [ ] `widget/src/embedding/resize_observer.ts` - Auto-resize logic
- [ ] `widget/index.html` - Demo page for widget testing

### 3.5 Helpdesk Integrations

#### Integration Adapters (Complete: 0%)
- [ ] `backend/app/adapters/ticketing/zendesk_full_adapter.py` - Full Zendesk integration
- [ ] `backend/app/adapters/ticketing/servicenow_full_adapter.py` - Full ServiceNow integration
- [ ] `backend/app/adapters/ticketing/intercom_adapter.py` - Intercom integration
- [ ] `backend/app/adapters/ticketing/salesforce_adapter.py` - Salesforce integration
- [ ] `backend/app/adapters/ticketing/jira_adapter.py` - Jira integration for technical escalations

#### Handoff Enhancement (Complete: 0%)
- [ ] `backend/app/application/handoff/context_serializer.py` - Context serialization for handoff
- [ ] `backend/app/application/handoff/recommendation_engine.py` - Next-step recommendations
- [ ] `backend/app/application/handoff/bidirectional_sync.py` - Bidirectional sync with helpdesk

---

## Phase 4: Continuous Testing + Production Readiness (Weeks 11-13)

**Goal:** Operationalized testing, deployment automation, and production hardening.

### 4.1 CI/CD Pipelines

#### GitHub Actions Workflows (Complete: 0%)
- [ ] `.github/workflows/ci-backend.yaml` - Backend CI pipeline
- [ ] `.github/workflows/ci-frontend.yaml` - Frontend CI pipeline
- [ ] `.github/workflows/ci-widget.yaml` - Widget CI pipeline
- [ ] `.github/workflows/test-unit.yaml` - Unit test execution
- [ ] `.github/workflows/test-integration.yaml` - Integration test execution
- [ ] `.github/workflows/test-security.yaml` - Security scanning
- [ ] `.github/workflows/deploy-staging.yaml` - Staging deployment
- [ ] `.github/workflows/deploy-production.yaml` - Production deployment
- [ ] `.github/workflows/scheduled-redteam.yaml` - Scheduled red-team runs

#### Quality Gates (Complete: 0%)
- [ ] `.github/workflows/quality-gate.yaml` - Code quality checks
- [ ] `.github/workflows/dependency-audit.yaml` - Dependency vulnerability scanning
- [ ] `.github/workflows/license-check.yaml` - License compliance checking

### 4.2 Scheduled Red-Teaming

#### Automation Scripts (Complete: 0%)
- [ ] `evals/scripts/scheduled_garak_run.py` - Scheduled Garak execution
- [ ] `evals/scripts/scheduled_pyrit_run.py` - Scheduled PyRIT execution
- [ ] `evals/scripts/regression_comparison.py` - Regression comparison on each run
- [ ] `evals/scripts/alert_on_failure.py` - Alerting on red-team failures

#### Reporting (Complete: 0%)
- [ ] `evals/scripts/generate_redteam_report.py` - Red-team report generation
- [ ] `evals/scripts/trend_analysis.py` - Trend analysis over time
- [ ] `evals/reports/` - Report output directory

### 4.3 Deployment Infrastructure

#### Docker Configuration (Complete: 0%)
- [ ] `ops/docker/backend/Dockerfile` - Production backend container
- [ ] `ops/docker/frontend/Dockerfile` - Frontend container (nginx serving static files)
- [ ] `ops/docker/widget/Dockerfile` - Widget container (optional, if served separately)
- [ ] `ops/docker/worker/Dockerfile` - Worker container
- [ ] `ops/docker/docker-compose.prod.yml` - Production compose file
- [ ] `ops/docker/docker-compose.staging.yml` - Staging compose file

#### Kubernetes (Optional) (Complete: 0%)
- [ ] `ops/kubernetes/backend/` - Kubernetes manifests for backend
- [ ] `ops/kubernetes/frontend/` - Kubernetes manifests for frontend
- [ ] `ops/kubernetes/worker/` - Kubernetes manifests for worker
- [ ] `ops/kubernetes/ingress/` - Ingress configuration
- [ ] `ops/kubernetes/secrets/` - Secret management (sealed-secrets or external-secrets)

#### Terraform (Complete: 0%)
- [ ] `ops/terraform/main.tf` - Main Terraform configuration
- [ ] `ops/terraform/variables.tf` - Variable definitions
- [ ] `ops/terraform/outputs.tf` - Output definitions
- [ ] `ops/terraform/modules/vpc/` - VPC module
- [ ] `ops/terraform/modules/database/` - Database module
- [ ] `ops/terraform/modules/cache/` - Cache module
- [ ] `ops/terraform/modules/object_storage/` - Object storage module
- [ ] `ops/terraform/modules/kubernetes/` - Kubernetes cluster module (optional)

#### Environment Bootstrap (Complete: 0%)
- [ ] `ops/scripts/bootstrap-dev.sh` - Development environment bootstrap
- [ ] `ops/scripts/bootstrap-staging.sh` - Staging environment bootstrap
- [ ] `ops/scripts/bootstrap-production.sh` - Production environment bootstrap
- [ ] `ops/scripts/migrate-database.sh` - Database migration runner
- [ ] `ops/scripts/backup-restore.sh` - Backup and restore scripts

### 4.4 Multi-Tenancy Hardening

#### Isolation Verification (Complete: 0%)
- [ ] `backend/tests/integration/tenancy/isolation_tests.py` - Tenant isolation tests
- [ ] `backend/app/modules/tenant_config/isolation_verifier.py` - Runtime isolation verification
- [ ] `backend/app/infrastructure/db/tenant_scoped_queries.py` - Tenant-scoped query enforcement

#### Per-Tenant Deployment (Complete: 0%)
- [ ] `ops/terraform/modules/tenant-isolation/` - Tenant isolation infrastructure
- [ ] `ops/scripts/deploy-tenant.sh` - Per-tenant deployment script
- [ ] `ops/templates/tenant-config-template.yaml` - Tenant configuration template

### 4.5 Documentation

#### Technical Documentation (Complete: 0%)
- [ ] `docs/implementation/architecture-decisions/` - Architecture Decision Records (ADRs)
- [ ] `docs/implementation/api-reference.md` - API reference documentation
- [ ] `docs/implementation/deployment-guide.md` - Deployment guide
- [ ] `docs/implementation/operations-manual.md` - Operations and runbook
- [ ] `docs/implementation/troubleshooting.md` - Troubleshooting guide

#### User Documentation (Complete: 0%)
- [ ] `docs/user/admin-guide.md` - Admin user guide
- [ ] `docs/user/policy-editor-guide.md` - Policy configuration guide
- [ ] `docs/user/escalation-workflow-guide.md` - Escalation workflow guide
- [ ] `docs/user/widget-integration-guide.md` - Widget integration guide for customers

#### Developer Documentation (Complete: 0%)
- [ ] `docs/dev/contributing.md` - Contribution guidelines
- [ ] `docs/dev/code-style.md` - Code style and conventions
- [ ] `docs/dev/testing-guide.md` - Testing guidelines
- [ ] `docs/dev/local-development.md` - Local development setup

### 4.6 Performance and Scaling

#### Performance Testing (Complete: 0%)
- [ ] `evals/performance/load_tests.yaml` - Load test definitions
- [ ] `evals/performance/stress_tests.yaml` - Stress test definitions
- [ ] `evals/performance/endurance_tests.yaml` - Endurance test definitions
- [ ] `evals/scripts/run_performance_tests.py` - Performance test runner

#### Optimization (Complete: 0%)
- [ ] `backend/app/infrastructure/cache/caching_strategies.py` - Caching strategies
- [ ] `backend/app/modules/rag/caching_retriever.py` - Cached retrieval
- [ ] `backend/app/application/orchestration/streaming_optimizer.py` - Streaming optimization
- [ ] `backend/app/modules/observability/performance_metrics.py` - Performance metric collection

#### Scaling Configuration (Complete: 0%)
- [ ] `ops/kubernetes/hpa/` - Horizontal Pod Autoscaler configs
- [ ] `ops/kubernetes/resource-limits/` - Resource limit configurations
- [ ] `ops/scripts/scaling-policy.md` - Scaling policy documentation

### 4.7 Security Hardening

#### Security Scanning (Complete: 0%)
- [ ] `.github/workflows/sast.yaml` - Static Application Security Testing
- [ ] `.github/workflows/dast.yaml` - Dynamic Application Security Testing
- [ ] `.github/workflows/container-scan.yaml` - Container vulnerability scanning
- [ ] `.github/workflows/secrets-scan.yaml` - Secrets detection scanning

#### Compliance Documentation (Complete: 0%)
- [ ] `docs/compliance/eu-ai-act-mapping.md` - EU AI Act compliance mapping
- [ ] `docs/compliance/owasp-mapping.md` - OWASP Top 10 mapping
- [ ] `docs/compliance/data-processing-agreement.md` - DPA template
- [ ] `docs/compliance/security-questionnaire.md` - Security questionnaire responses

#### Incident Response (Complete: 0%)
- [ ] `docs/operations/incident-response-plan.md` - Incident response procedures
- [ ] `docs/operations/security-incident-playbook.md` - Security incident playbook
- [ ] `ops/scripts/emergency-shutdown.sh` - Emergency shutdown procedures

---

## Implementation Priority Matrix

| Priority | Component | Business Value | Technical Risk | Effort |
|---|---|---|---|---|
| **P0** | Tenant config + orchestration | Critical | Medium | High |
| **P0** | Guardrails (NeMo + Guardrails AI) | Critical | High | High |
| **P0** | PII detection + redaction | Critical | Medium | Medium |
| **P0** | Output validation | Critical | Low | Medium |
| **P1** | LLM provider adapters | High | Low | Medium |
| **P1** | RAG with spotlighting | High | Medium | High |
| **P1** | Escalation + handoff | High | Medium | Medium |
| **P1** | Observability (Langfuse) | High | Low | Medium |
| **P2** | Admin UI | Medium | Low | High |
| **P2** | Chat widget | Medium | Low | Medium |
| **P2** | Red-teaming automation | Medium | Medium | Medium |
| **P3** | Advanced helpdesk integrations | Low | Medium | Medium |
| **P3** | Per-tenant deployment automation | Low | High | High |
| **P3** | Llama Guard integration | Low | Medium | Low |

---

## OpenCode Usage Throughout Implementation

**Phase 1:**
- Prototype LLM provider adapters before implementing in production code
- Test different orchestration patterns in LangGraph
- Iterate on prompt templates for various use cases
- Debug tenant config resolution logic

**Phase 2:**
- Test guardrail configurations against adversarial prompts
- Prototype classifier models for topic detection
- Validate PII detection accuracy with custom test data
- Experiment with RAG retrieval strategies

**Phase 3:**
- Debug escalation workflows with synthetic conversations
- Test observability tracing with complex multi-turn sessions
- Prototype widget embedding patterns
- Validate helpdesk integration payloads

**Phase 4:**
- Replay production traces (redacted) for regression testing
- Run red-team scenarios before deploying to production
- Compare model behavior across providers for optimization
- Fork sessions for debugging complex edge cases

---

## Definition of Done by Phase

### Phase 1 Done:
- [ ] Single tenant can send messages and receive validated responses
- [ ] Basic policy enforcement prevents out-of-scope queries
- [ ] Output validation catches schema violations
- [ ] Tenant configuration is loaded and applied
- [ ] At least one LLM provider works end-to-end
- [ ] Unit test coverage > 60% for domain and application layers

### Phase 2 Done:
- [ ] Injection attempts are detected and blocked
- [ ] PII is detected and redacted before model calls
- [ ] RAG retrieval includes spotlighting and filtering
- [ ] Jailbreak detection has > 90% catch rate on test corpus
- [ ] Garak and PyRIT suites pass with no critical findings
- [ ] Regression test suite established with baseline scores

### Phase 3 Done:
- [ ] Human handoff works with webhook-based ticketing
- [ ] Full trace visibility in Langfuse dashboard
- [ ] Admin UI allows tenant and policy configuration
- [ ] Chat widget embeddable in customer websites
- [ ] Background jobs execute scheduled tasks
- [ ] At least one major helpdesk integration complete

### Phase 4 Done:
- [ ] CI/CD pipelines automate testing and deployment
- [ ] Red-team runs execute on schedule and on config changes
- [ ] Multi-tenant isolation verified with automated tests
- [ ] Production deployment documented and repeatable
- [ ] Compliance mapping complete for EU AI Act and OWASP
- [ ] Performance benchmarks meet SLA requirements

---

## Risk Mitigation Strategies

| Risk | Mitigation | Owner |
|---|---|---|
| LangGraph licensing costs | Use core + custom wrapper; evaluate AG2 fallback | Backend Lead |
| Guardrail false positives | Implement ensemble approach; tune thresholds with evals | Safety Lead |
| PII detection gaps | Layer Presidio + Cloud DLP; regular eval updates | Security Lead |
| Multi-tenant leakage | Automated isolation tests; tenant-scoped queries | Platform Lead |
| Red-team coverage gaps | Multiple tools (Garak + PyRIT); community probe sharing | Security Lead |
| Performance degradation | Caching strategies; streaming; load testing | Platform Lead |
| Vendor lock-in concerns | Abstraction layers; swappable adapters | Architecture Lead |

---

## Next Immediate Actions

1. **Start Phase 1, Week 1:**
   - Create `backend/app/main.py` with FastAPI app skeleton
   - Implement `backend/app/settings/env.py` for configuration loading
   - Build `backend/app/modules/tenant_config/config_loader.py` 
   - Create `backend/app/adapters/llm/base_provider.py` and one concrete adapter
   - Implement `backend/app/application/orchestration/agent_service.py` with LangGraph
   - Set up `backend/tests/conftest.py` with initial fixtures

2. **Parallel Track:**
   - Draft `contracts/openapi/neryva-api-v1.yaml` for API contract-first development
   - Create `backend/app/modules/guardrails/regex_fastpath.py` for immediate basic filtering
   - Set up `evals/datasets/` directory structure with initial test corpora

3. **OpenCode Setup:**
   - Configure OpenCode sessions for provider testing
   - Create initial prompt iteration workflows
   - Establish session forking patterns for debugging

---

**Document Status:** Ready for engineering execution
**Review Cycle:** Update weekly during implementation
**Stakeholders:** Engineering Lead, Product Lead, Security Lead, Platform Lead
