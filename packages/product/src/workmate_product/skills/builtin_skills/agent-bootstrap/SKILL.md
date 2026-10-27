---
name: agent-bootstrap
description: Create or edit governed Workmate Agents through platform tools.
version: "2"
tags: [Workmate, agent, creation]
---

# Agent bootstrap

Use this workflow when the user asks to create or edit an Agent.

1. Call `platform_get_context` first and inspect `agent_creation`. Offer personal
   or team creation only when team_scopes is nonempty; otherwise explain that
   creation will be personal. Honor explicit scope requests and explain missing
   permissions without changing scope or ending the conversation. Wait for the
   user's choice before changing an unauthorized target. For team creation,
   offer only authorized team_scopes and confirm the chosen department.
   Use personal_scope_id for personal creation and private visibility.
   Query `capability_list` with the chosen scope_id, never the calling assistant's
   scope; then call `model_list` and relevant `agent_list` or `agent_get`.
   Never invent an identifier. On permission_denied, explain and offer allowed
   choices; changed submissions require fresh validation and HITL approval.
2. Extract intended users, tasks, inputs, deliverables, communication style,
   and boundaries from the conversation. Ask 1–3 focused questions per round
   only about consequential gaps; skip answered questions and unnecessary
   personality interviews. Do not force a fixed number of onboarding rounds.
   Translate preferences into observable behavior, not generic adjectives:
   "compare figures with source data and flag mismatches" instead of "rigorous".
   Write a proposed configuration to `working/` when substantial. Include:
   - `display_name` and a short non-empty public `description` explaining who
     it helps and what it does; never copy private prompt text into the description.
   - `system_prompt`: role, tasks, workflow, outputs, communication style, and
     limits grounded in the user's needs and available capabilities.
   - Target Scope, visibility, and only verified Skill/MCP binding IDs.
   Use the user's requested language. Model selection belongs to Conversation
   configuration, not an Agent model field. A draft file does not persist an
   Agent; use Workmate platform tools, not SOUL.md or a sandbox config file.
3. Call `agent_validate` and show the resulting summary or edit differences.
4. Show the name, description, target Scope, visibility, Skill/MCP bindings and changed
   fields. Call `agent_create` or `agent_update` to request mandatory HITL
   approval; wait for the user to approve before submission. Full Access
   does not waive confirmation. A refusal ends this submission; do not retry
   without a new user request. The platform performs permission,
   Scope, capability, approval, version, and idempotency checks.
5. Report the returned Product and Runtime synchronization status accurately.

Only Agent creation or Agent editing may change Skill/MCP bindings. Never ask
`skill_install` to bind a Skill. Conversational Agent editing cannot change
the calling Agent's tool-policy ceiling or platform-tool profile.
