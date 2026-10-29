# D3 Health Knowledge & Safety prompt ownership

Current status:

- D3 is implemented with a deterministic lexical index over approved,
  hash-validated snapshots in `chatbot/resources/health_knowledge/`.
- Retrieval, evidence validation, abstention, and the provisional safety policy
  make no model call, so no `.txt` prompt is active here.
- If a semantic query or grounded synthesis model step is later approved, its
  exact prompt, input contract, source policy, version, and hash belong here.
