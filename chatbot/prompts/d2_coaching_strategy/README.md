# D2 Advisor prompt source map

D2 loads the task shell and the six policy fragments below in a fixed order.
Each fragment begins with source metadata comments so a prompt review can trace
the operational paraphrase back to the original publication or official page.
The source registry is authoritative for IDs, versions, rule origin, ownership,
and redistribution notes. Prompt hashes are recorded in each D2 result.

| Prompt resource | External source | Runtime adaptation | Boundary |
|---|---|---|---|
| `01_nhs_role_and_personalised_care.txt` | NHS health and wellbeing coach framework; Supported self-management | Concise non-clinical role, partnership, and personalised-care constraints | Advisor |
| `02_comb_framework.txt` | Michie, van Stralen & West (2011) | Evidence-grounded behavioural formulation, not diagnosis | Advisor |
| `03_bct_framework.txt` | Michie et al. (2013) BCTTv1 | Selection from the explicit 38-item Sensei subset only | Advisor |
| `04_mi_framework.txt` | Rollnick & Miller (1995); Miller & Rose (2009) | Collaborative delivery and ambivalence constraints | Advisor; no MITI scoring |
| `05_sdt_framework.txt` | Ryan & Deci (2000) | Autonomy, competence, and relatedness delivery constraints | Advisor |
| `06_interaction_depth_and_scope.txt` | Sensei internal policy | Runtime depth and scope labels | Advisor |

`plan_coaching_strategy.txt` is the Sensei task shell. Source-derived rules and
Sensei operational rules must remain explicitly distinct. Evaluation labels,
gold answers, Auditor rubrics, MITI scores, and self-report outcome scores must
not enter this runtime prompt bundle.
