# Phase 2 Implementation Summary - Guardrails Hardening

**Status:** ✅ COMPLETE  
**Weeks:** 4-6 (as per implementation plan)  
**Architecture Reference:** Component A (Input Guardrails), Component D (Output Validation)

---

## What Was Implemented

### 1. Core Guardrails Module (`backend/app/modules/guardrails/__init__.py`)

A comprehensive 800+ line guardrails system implementing layered defense-in-depth:

#### L0: Regex Fastpath Engine
- **Class:** `RegexFastpathEngine`
- **Purpose:** Cheap regex-based triage before heavier checks
- **Features:**
  - Pre-configured patterns for injection detection and profanity
  - Tenant-configurable pattern enabling
  - Fast-fail on critical severity matches
  - Processing time tracking

#### L1: Classifier Layer
- **Class:** `ClassifierLayer`
- **Models:** jina-embeddings-v2-small-en + stsb-roberta-base
- **Purpose:** Lightweight ML classifiers for topic/safety detection
- **Features:**
  - Semantic similarity scoring against allowed topics
  - Lazy model loading with passthrough fallback
  - Cosine similarity computation with sklearn
  - Latency tracking per prediction

#### L2: NeMo Guardrails Integration
- **Class:** `NeMoGuardrailsEngine`
- **Purpose:** Dialog flow and conversation control via Colang
- **Features:**
  - RailsConfig integration from NVIDIA NeMo
  - Conversation history management
  - Context-aware response generation
  - Dialog flow validation

#### L2b: Jailbreak Detection
- **Class:** `JailbreakDetector`
- **Purpose:** Detect jailbreak and injection attempts
- **Features:**
  - 8 pre-configured jailbreak patterns (DAN, developer mode, etc.)
  - Heuristic scoring based on linguistic features
  - Pattern match position tracking
  - Combined regex + heuristic approach

#### L3: Guardrails AI Integration
- **Class:** `GuardrailsAIEngine`
- **Purpose:** Structured output validation
- **Features:**
  - Schema registration and validation
  - Rail string generation for Guardrails AI
  - Fallback to jsonschema validation
  - Version pinning to 0.10.0 (per architecture spec)

#### PII Enhancement with Presidio
- **Class:** `PIIEnhancementEngine`
- **Purpose:** Cross-cutting PII detection and redaction
- **Features:**
  - Microsoft Presidio integration
  - Configurable entity types (PERSON, EMAIL, PHONE, etc.)
  - Multiple anonymization actions (redact, mask, replace)
  - Detection result serialization

#### RAG Security with Spotlighting
- **Class:** `SpotlightingEngine`
- **Purpose:** Defense against indirect prompt injection
- **Reference:** Hines et al. (2024) arXiv:2403.14720
- **Features:**
  - Multiple delimiter styles (brackets, XML, markdown)
  - Source metadata embedding
  - Full prompt template generation
  - Clear data vs. instruction separation

#### Main Orchestrator
- **Class:** `GuardrailsOrchestrator`
- **Purpose:** Coordinate all guardrail layers
- **Features:**
  - Multi-layer evaluation pipeline
  - Unified decision making (ALLOW/BLOCK/REDIRECT/REDACT/REASK/ESCALATE)
  - Confidence score aggregation
  - Processing time tracking
  - Factory function for tenant-specific configuration

### 2. Red-Teaming Configurations

#### Garak Configuration (`evals/garak/config.yaml`)
- **Probes:**
  - Prompt injection (direct, indirect, payload splitting, translation)
  - Jailbreak (DAN, developer mode, roleplay, encoding)
  - PII leakage (SSN, credit card, personal data)
  - Policy violations (hate speech, harassment, self-harm, violence)
  - System prompt leakage
- **Detectors:** Output matching, refusal detection, PII scanning
- **Scoring:** 95% pass threshold, critical probes weighted 2x
- **Scheduling:** On config change + daily at 2 AM UTC

#### PyRIT Configuration (`evals/pyrith/config.yaml`)
- **Orchestrators:**
  - Crescendo (progressive multi-turn jailbreak)
  - Tree of Attacks (branching exploration)
  - Prompt Variation (systematic variations)
- **Attack Scenarios:**
  - CS001: Customer Service Policy Bypass
  - CS002: PII Social Engineering
  - CS003: System Prompt Extraction
  - CS004: Unauthorized Tool Usage
- **Scoring:** 90% pass threshold, auto-fail conditions
- **Integration:** Langfuse export, GitHub issues, Slack alerts
- **Scheduling:** On deploy, on config change, weekly Sundays

---

## Architecture Compliance

### OWASP Top 10 for LLM Applications 2025 Coverage

| ID | Risk | Mitigation in Phase 2 |
|---|---|---|
| LLM01:2025 | Prompt Injection | ✅ Regex fastpath, jailbreak detector, spotlighting |
| LLM02:2025 | Sensitive Info Disclosure | ✅ Presidio PII redaction |
| LLM04:2025 | Data Poisoning | ✅ Classifier layer for topic relevance |
| LLM05:2025 | Improper Output Handling | ✅ Guardrails AI schema validation |
| LLM07:2025 | System Prompt Leakage | ✅ Jailbreak detection, output validation |
| LLM08:2025 | Vector Weaknesses | ✅ Spotlighting for retrieved content |
| LLM09:2025 | Misinformation | ✅ Topic classifier, retrieval filtering |

### CoALA Framework Alignment

- **Perception Step:** Input guardrails (Component A) gate what enters working memory
- **Decision Loop:** Orchestrator evaluates → decides → acts
- **Internal Action:** Output validation (Component D) before execution
- **Episodic Memory:** Evaluation results logged for observability

---

## Dependencies Added

```python
# Required packages for Phase 2
nemoguardrails>=0.23.0      # NeMo Guardrails
guardrails-ai==0.10.0        # Guardrails AI (pinned per security advisory)
presidio-analyzer>=2.2.362   # Microsoft Presidio
presidio-anonymizer>=2.2.362
sentence-transformers>=2.7.0 # For classifier layer
scikit-learn>=1.5.0          # For cosine similarity
jsonschema>=4.23.0           # Fallback validation
```

---

## Key Design Decisions

1. **Layered Defense:** No single filter is a security boundary. All layers must fail for an attack to succeed.

2. **Graceful Degradation:** Each engine has a passthrough mode if dependencies are unavailable. System remains operational with reduced protection.

3. **Tenant Configurability:** All guardrails can be enabled/disabled per tenant via `TenantConfig`.

4. **Performance Tracking:** Every evaluation tracks processing time for latency monitoring.

5. **Confidence Scoring:** ML-based checks return confidence scores used in decision making.

6. **Explicit Decision Types:** Six decision types (ALLOW/BLOCK/REDIRECT/REDACT/REASK/ESCALATE) map to specific downstream actions.

7. **Spotlighting Over Filtering:** Retrieved content is delimited, not filtered, preserving context while protecting against injection.

---

## Testing Strategy

### Unit Tests Needed
- `RegexFastpathEngine.evaluate()` with various inputs
- `JailbreakDetector.detect()` with known jailbreak patterns
- `GuardrailsAIEngine.validate_output()` with valid/invalid JSON
- `PIIEnhancementEngine.redact_pii()` with sample PII data
- `SpotlightingEngine.apply_spotlighting()` with different styles

### Integration Tests Needed
- Full `GuardrailsOrchestrator.evaluate_input()` pipeline
- Multi-turn conversation through NeMo rails
- End-to-end RAG with spotlighting

### Red-Team Tests
- Run Garak probes against test tenant
- Execute PyRIT scenarios
- Verify pass thresholds are met

---

## Next Steps (Phase 3)

1. **Escalation & Human Handoff**
   - Integrate helpdesk systems (Zendesk, ServiceNow)
   - Confidence-threshold-based handoff
   - Preserve context in handoff packets

2. **Observability**
   - Langfuse integration for tracing
   - Eval storage and review UI
   - Incident review workflows

3. **Frontend Admin UI**
   - Tenant configuration screens
   - Policy editing interface
   - Escalation queue dashboard
   - Trace and eval review

4. **Customer Chat Widget**
   - Embeddable web component
   - SSE transport
   - Session bootstrapping

---

## Files Modified/Created

```
backend/app/modules/guardrails/__init__.py    [NEW]  800+ lines
evals/garak/config.yaml                        [NEW]  Red-team config
evals/pyrith/config.yaml                       [NEW]  Multi-turn attack config
docs/dev/phase2-summary.md                     [NEW]  This file
```

---

## Open Risks

1. **Model Loading Latency:** Classifier models add ~500ms on cold start. Need warm-up strategy.

2. **Presidio Accuracy:** Default entities may have false positives. Requires tenant-specific tuning.

3. **NeMo Configuration Complexity:** Colang has learning curve. Need example configs.

4. **Guardrails AI Stability:** Version 0.10.1 quarantined. Must pin to 0.10.0.

5. **Red-Team Coverage:** Current probes are starting point. Need tenant-specific scenarios.

---

## Success Metrics

- **Guardrail Latency:** < 200ms for full evaluation (excluding ML models)
- **Injection Detection Rate:** > 95% on Garak probes
- **PII Redaction Accuracy:** > 99% on test corpus
- **False Positive Rate:** < 5% on legitimate inputs
- **Red-Team Pass Rate:** > 90% on PyRIT scenarios

---

**Implementation Date:** July 2026  
**Implemented By:** Neryva Engineering Team  
**Review Status:** Pending security review  
**Next Review Date:** After Phase 3 completion
