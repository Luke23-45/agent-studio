# Neryva Agent Studio — OpenCode Boundary Rules

OpenCode is an **internal developer/operator workbench only**. It is NOT part of the production Neryva runtime.

## What OpenCode Is Used For

- Provider connection testing (OpenAI, Anthropic, Gemini, self-hosted)
- Model behavior comparison across providers
- MCP server prototyping and tool integration experiments
- Prompt iteration and A/B testing in a sandboxed environment
- Session forking for debugging complex orchestration flows
- Replay and debug workflows using sampled production traces (after PII redaction)

## What OpenCode Is NOT Used For

- The production customer agent runtime
- Tenant session storage or state management
- Policy enforcement or guardrail evaluation
- Direct customer-facing interactions
- Bypassing Neryva's tenant policies, PII handling, or approval boundaries

## Boundary Rules

1. OpenCode sessions are **operator sessions**, not customer sessions.
2. Any tool, provider connection, or workflow prototyped here must pass through the production policy gate, PII layer, and guardrail stack before it can be used in the customer runtime.
3. OpenCode must remain behind Neryva's security boundary.
4. Never use OpenCode to serve production traffic or store production tenant state.

## Development Workflow

- Use OpenCode sessions for early-stage provider testing and prompt iteration before moving code to `backend/app/adapters/`.
- Prototype MCP servers in OpenCode before they enter the policy gate.
- Use session forking to debug LangGraph orchestration flows with synthetic conversations.
