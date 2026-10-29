# D4 Multi-turn Context prompt ownership

Current status:

- D4 deterministically resolves explicit reply-to, latest goals, preferences,
  barriers, and prior query conditions in the current session, then validates
  every resolved item against source-turn IDs.
- Ambiguous or unsupported references produce a clarification request. D4 does
  not claim long-term memory and currently makes no model call.
- If a semantic resolver is later required, its prompt and typed source-ID
  contract belong here; its output must still pass deterministic validation.
