# Engineering documentation

> Status: **Source documentation; not deployed**

## Purpose

This directory contains design and migration records for the Main Function App.
It is excluded from the Azure package by [`../.funcignore`](../.funcignore).
Runtime setup, configuration, deployment, and post-deployment checks remain in
the component [`README.md`](../README.md).

## Files at a glance

| Path | Purpose |
|---|---|
| [`DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md`](DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md) | Current verified report for the D1-D4 DAG, node ownership, prompt ownership, common functions, and proactive runtime layout. |
| [`refactoring/`](refactoring/) | Auditable record of the pre-refactor design, current target architecture, compatibility mapping, and removed scaffolding. |
| [`D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md`](D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md) | Historical first-stage D1/D2 implementation snapshot. Paths and validation counts describe that point in time. |
| [`D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md`](D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md) | Historical second-stage domain-architecture repair snapshot. |
| [`STRICT_DOMAIN_OWNERSHIP_REPORT_KR.md`](STRICT_DOMAIN_OWNERSHIP_REPORT_KR.md) | Historical strict-ownership pass that preceded the final node-per-file layout. |
| [`STRICT_OWNERSHIP_REFERENCE_AUDIT_KR.md`](STRICT_OWNERSHIP_REFERENCE_AUDIT_KR.md) | Historical reference-search evidence for that strict-ownership pass. |

Additional component-level documentation lives beside the code it explains:

- [`../functions/README.md`](../functions/README.md) for Azure triggers and
  transport boundaries;
- [`../chatbot/README.md`](../chatbot/README.md) for the assistant application;
- [`../chatbot/prompts/README.md`](../chatbot/prompts/README.md) for the complete
  prompt inventory and contributor contract;
- [`../memory/README.md`](../memory/README.md) for the source-only legacy
  episode-memory research package;
- [`../scripts/README.md`](../scripts/README.md) for operator verification; and
- [`../local_test.py`](../local_test.py) for the single local smoke and
  non-production runtime-validation entry point.

## How to use these documents

Begin with the component [`README.md`](../README.md) for setup and deployment.
Use the most specific package README for current code ownership, and open the
refactoring record only when tracing a historical path or architectural move.

### Document authority

Use the most specific current-state source available:

1. Executable code, deployment configuration, and repeatable package validation
   define actual runtime behavior.
2. The current [`DAG/domain layout report`](DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md),
   [`application architecture`](../ARCHITECTURE.md),
   [`node ownership guide`](../chatbot/nodes/README.md), and
   [`prompt ownership guide`](../chatbot/prompts/README.md) define the verified
   source layout and ownership boundaries.
3. The root component README defines current operator-facing setup and
   deployment procedures.
4. Other package READMEs define current ownership and contributor boundaries.
5. Earlier implementation reports and `refactoring/AS_IS.md` are historical
   evidence, not current instructions.
6. Roadmaps or proposed designs do not become runtime contracts until their
   implementation and corresponding validation are merged.

When documentation and code differ, verify the behavior, correct the document,
and add a repeatable smoke, package, or manual check when practical. Do not
preserve a known-inaccurate statement as a compatibility promise.

## Contributor guidance

- Write documentation and code comments in English.
- Use relative Markdown links so GitHub renders navigation correctly.
- Do not include secrets, real identifiers, connection strings, OAuth values,
  raw health records, or production screenshots containing sensitive data.
- Clearly label historical, current, source-only, optional, and planned
  material.
- Update file maps when modules move or are removed.
- Keep commands copyable and state their shell, working directory,
  prerequisites, side effects, and success criteria.
- Keep evaluation answers and gold labels outside production application
  documentation and runtime prompt resources.

Documentation is not copied into the Function App artifact. Validate links and
language in the source tree before committing changes.
