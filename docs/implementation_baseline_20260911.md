# D1-D4 implementation baseline — 2026-09-11

This record freezes the production-side baseline immediately before the D2-D4
implementation described in `Sensei_D2_D4_Implementation_Work_Specification_KO.md`.

## Source revision

- Branch: `red_teaming`
- Baseline commit: `2c4085b57b433ca1e7fd35ba246c39b3565539dd`
- Top-level graph: six stages (`prepare_context` through
  `persist_trace_and_context`)
- Domain registry: D1-D4

## Baseline implementation state

| Domain | State before this work |
|---|---|
| D1 | Implemented sensor planning, KQL generation/guard/repair, ADX execution, and evidence result. |
| D2 | Four-node typed Advisor strategy workflow with deterministic validation. The BCT resource contains 38 entries but does not yet carry a complete subset rationale/evidence-reference contract. |
| D3 | Compiled deterministic `not_implemented` placeholder. |
| D4 | Compiled deterministic `not_implemented` placeholder. |

D2 baseline resource metadata:

- policy bundle SHA-256: `da5b0c28b0b138d7ce106add58420c0e8f43c31488aaa271ca1110dab44b00fb`
- catalogue ID: `sensei_d2_bcttv1_coaching_subset`
- catalogue version: `1.0.0`
- catalogue entries: 38

The deterministic validator already removed unsupported BCT IDs and changed an
unresolved `COLLABORATIVE_PLANNING` request to `EXPLORE_BARRIER`. This behavior
is a compatibility floor for the strengthened validator.

## Baseline verification

Executed before runtime edits:

```text
python azure_function_main_app/local_test.py smoke
PASSED — 26 Azure Functions, 6 graph nodes, 28 prompt resources

python -m unittest evaluation.test_offline evaluation.test_diagnostics -v
PASSED — 26 tests
```

These are offline structural/regression checks. They do not claim live ADX,
Storage, model-provider, corpus-quality, clinical-safety, or end-to-end behavior.
