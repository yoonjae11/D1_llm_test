# Strict ownership: 저장소 전체 참조 감사

> **역사 문서:** 이 감사는 root shim을 유지했던 3차 작업 시점의 검색 결과입니다.
> 이후 shim과 tooling import가 모두 canonical 경로로 교체되었습니다. 현재 결과는
> [현행 DAG/domain 보고서](DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md)를 확인하세요.

작성일: 2026-09-08. `rg`로 전체 저장소의 구형 node import, prompt resource 및 proactive import를 이동 전후 검색했다. 이 문서와 최종 ownership 보고서는 old → new 기록 자체가 새로운 검색 hit를 만들므로 후속 집계에서 제외했다. 그 외 문서·소스·평가·local tooling은 검색에 포함했다. 아래 숫자는 각 문자열의 출현 횟수다.

당시 runtime의 구형 import/resource 참조는 0이었고 Python tooling의 옛 node import는 shim으로 보호했다. 이 문장은 당시 상태이며, 현재 shim은 제거되었다. historical 문서의 옛 경로는 당시 상태를 보존하는 이력이며 runtime 이동 유보를 뜻하지 않는다.

| Old import/resource | Before | Runtime after | Tooling after | Historical docs after |
|---|---:|---:|---:|---:|
| `chatbot.nodes.plan_builder` | 4 | 0 | 2 | 0 |
| `chatbot.nodes.query_generator` | 8 | 0 | 6 | 0 |
| `chatbot.nodes.kql_guard` | 3 | 0 | 3 | 0 |
| `chatbot.nodes.query_repair` | 5 | 0 | 3 | 0 |
| `chatbot.nodes.query_executor` | 5 | 0 | 5 | 0 |
| `chatbot.nodes.hydrate_context` | 1 | 0 | 1 | 0 |
| `chatbot.nodes.persist_context` | 2 | 0 | 2 | 0 |
| `chatbot.nodes.response_generator` | 6 | 0 | 4 | 0 |
| `chatbot.nodes.support` | 12 | 0 | 12 | 0 |
| `chatbot.nodes.intent_router` | 3 | 0 | 1 | 0 |
| `chatbot.nodes.alert_config_responder` | 2 | 0 | 0 | 0 |
| `chatbot.nodes.capabilities_responder` | 2 | 0 | 0 | 0 |
| `chatbot.nodes.onboarding_responder` | 0 | 0 | 0 | 0 |
| `chatbot.nodes.domain_orchestration` | 1 | 0 | 1 | 0 |
| `user_initiated/plan_builder/` | 10 | 0 | 0 | 0 |
| `user_initiated/query_generator/` | 9 | 0 | 0 | 0 |
| `user_initiated/query_repair/` | 8 | 0 | 0 | 0 |
| `user_initiated/response_generator/` | 34 | 0 | 0 | 6 |
| `shared/adx_schema_context.txt` | 11 | 0 | 0 | 1 |

## 이동 후 남은 각 hit

`kept for compatibility`는 평가/로컬 도구/회귀 테스트가 사용하는 옛 Python 경로다. `intentionally deferred`는 과거 상태 문서 또는 기존 이관 기록이다. 실행 프롬프트의 옛 경로를 호환 목적으로 남긴 경우는 없다.

| File:line | Classification | Remaining reference |
|---|---|---|
| `evaluation/test_offline.py:283` | kept for compatibility |         from chatbot.nodes.plan_builder import _plan_node_result |
| `evaluation/test_offline.py:598` | kept for compatibility |         import chatbot.nodes.query_generator as generator_module |
| `evaluation/test_offline.py:751` | kept for compatibility |                 "chatbot.nodes.query_generator.generate_kql_candidate", |
| `evaluation/test_offline.py:762` | kept for compatibility |                 "chatbot.nodes.kql_guard.kql_guard", |
| `evaluation/test_offline.py:771` | kept for compatibility |                 "chatbot.nodes.query_executor.query_executor", |
| `evaluation/test_offline.py:781` | kept for compatibility |                 "chatbot.nodes.response_generator.response_generator", |
| `evaluation/test_offline.py:877` | kept for compatibility |                 "chatbot.nodes.query_generator.generate_kql_candidate", |
| `evaluation/test_offline.py:888` | kept for compatibility |                 "chatbot.nodes.kql_guard.kql_guard", |
| `evaluation/test_offline.py:905` | kept for compatibility |                 "chatbot.nodes.query_repair.query_repair", |
| `evaluation/test_offline.py:917` | kept for compatibility |                 "chatbot.nodes.query_executor.query_executor", |
| `evaluation/test_offline.py:927` | kept for compatibility |                 "chatbot.nodes.response_generator.response_generator", |
| `evaluation/runner.py:301` | kept for compatibility |         from chatbot.nodes.plan_builder import plan_builder |
| `evaluation/runner.py:376` | kept for compatibility |         from chatbot.nodes.kql_guard import kql_guard |
| `evaluation/runner.py:377` | kept for compatibility |         from chatbot.nodes.query_executor import query_executor |
| `evaluation/runner.py:378` | kept for compatibility |         from chatbot.nodes.query_generator import generate_kql_candidate |
| `evaluation/runner.py:379` | kept for compatibility |         from chatbot.nodes.query_repair import query_repair |
| `evaluation/runner.py:380` | kept for compatibility |         from chatbot.nodes.response_generator import response_generator |
| `azure_function_main_app/docs/refactoring/TO_BE.md:130` | intentionally deferred | - threshold alerts and daily reports: chatbot.threshold_monitor; |
| `azure_function_main_app/docs/refactoring/TO_BE.md:131` | intentionally deferred | - System STEPS alerts: chatbot.system_steps_alert; |
| `azure_function_main_app/docs/refactoring/MIGRATION_MAP.md:35` | intentionally deferred | \| Node-owned user prompts and example fragments \| chatbot/prompts/user_initiated/<owner>/ \| MOVED + MERGED \| Examples are embedded with the owning model task \| |
| `azure_function_main_app/docs/refactoring/MIGRATION_MAP.md:36` | intentionally deferred | \| ADX schema prompt resource \| chatbot/prompts/shared/adx_schema_context.txt \| MOVED \| Same query-generation and repair context \| |
| `azure_function_main_app/docs/refactoring/MIGRATION_MAP.md:37` | intentionally deferred | \| Threshold/report prompts \| chatbot/prompts/system_initiated/threshold_monitor/ \| MOVED \| Same system notification model tasks \| |
| `azure_function_main_app/docs/refactoring/MIGRATION_MAP.md:38` | intentionally deferred | \| Guided input NLU prompts \| chatbot/prompts/user_initiated/guided_input_nlu/ \| MOVED \| Same fallback parsing tasks \| |
| `azure_function_main_app/docs/D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md:446` | intentionally deferred | - D1 prompt 경로 이동: user_initiated/plan_builder, query_generator, query_repair의 실제 리소스 이동은 유보했다. prompts/d1_sensor_data/README.md에 명시했다. |
| `azure_function_main_app/docs/D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md:447` | intentionally deferred | - 공통 응답 prompt 경로 이동: 기존 user_initiated/response_generator/ 리소스는 유지하며 prompts/response/README.md에 후속 이관을 기록했다. |
| `azure_function_main_app/docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md:22` | intentionally deferred | - [chatbot/prompts/user_initiated/response_generator/coaching_answer.txt](../chatbot/prompts/user_initiated/response_generator/coaching_answer.txt) |
| `azure_function_main_app/docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md:44` | intentionally deferred | - [chatbot/prompts/user_initiated/response_generator/data_answer.txt](../chatbot/prompts/user_initiated/response_generator/data_answer.txt) |
| `azure_function_main_app/docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md:276` | intentionally deferred | \| 중간 \| chatbot/prompts/user_initiated/response_generator/data_answer.txt 및 구조 설명 문서 \| 프롬프트·설명 동시 수정 가능 \| |
| `azure_function_main_app/tests/test_domain_orchestration.py:28` | kept for compatibility | from chatbot.nodes.domain_orchestration import aggregate_domain_results, execute_domains |
| `azure_function_main_app/tests/test_domain_orchestration.py:187` | kept for compatibility |         call = self.stub("chatbot.nodes.support.call_chat_completion", return_value="bad JSON") |
| `azure_function_main_app/tests/test_domain_orchestration.py:215` | kept for compatibility |                 with patch("chatbot.nodes.support.call_chat_completion", return_value=output) as call: |
| `azure_function_main_app/tests/test_domain_orchestration.py:221` | kept for compatibility |         self.stub("chatbot.nodes.support.call_chat_completion", return_value='{"domains":["INVALID"]}') |
| `azure_function_main_app/tests/test_domain_orchestration.py:227` | kept for compatibility |         call = self.stub("chatbot.nodes.support.call_chat_completion") |
| `azure_function_main_app/tests/test_domain_orchestration.py:315` | kept for compatibility |         call = self.stub("chatbot.nodes.support.call_chat_completion", return_value=strategy().model_dump_json()) |
| `azure_function_main_app/tests/test_domain_orchestration.py:345` | kept for compatibility |         self.final_call = self.stub("chatbot.nodes.response_generator._user_facing_generate", return_value=("A grounded answer. What matters most to you?", "offline-response")) |
| `azure_function_main_app/tests/test_domain_orchestration.py:351` | kept for compatibility |         self.stub("chatbot.nodes.support.call_chat_completion", side_effect=[route.model_dump_json(), json.dumps(sensor_plan)]) |
| `azure_function_main_app/tests/test_domain_orchestration.py:352` | kept for compatibility |         adx = self.stub("chatbot.nodes.query_executor.adx_manager.execute_query", return_value=[{"Period": "2026-09-06", "Metric": "steps", "Value": 5200, "Unit": "steps"}]) |
| `azure_function_main_app/tests/test_domain_orchestration.py:363` | kept for compatibility |         self.stub("chatbot.nodes.support.call_chat_completion", side_effect=[ |
| `azure_function_main_app/tests/test_domain_orchestration.py:382` | kept for compatibility |         calls = self.stub("chatbot.nodes.support.call_chat_completion", side_effect=[ |
| `azure_function_main_app/tests/test_domain_orchestration.py:394` | kept for compatibility |         model = self.stub("chatbot.nodes.support.call_chat_completion") |
| `azure_function_main_app/tests/test_domain_orchestration.py:404` | kept for compatibility |                 with patch("chatbot.nodes.support.call_chat_completion", return_value=execution_plan(intent=intent).model_dump_json()), patch( |
| `azure_function_main_app/tests/test_domain_orchestration.py:414` | kept for compatibility |                 with patch("chatbot.nodes.support.call_chat_completion", return_value=execution_plan(intent="data", domains=["D1"]).model_dump_json()), patch.dict(DOMAIN_RUNNERS, {"D1": Mock(return_value=domain_output("D1", evidence=sensor["d1_result"], updates=sensor))}): |
| `azure_function_main_app/tests/test_domain_orchestration.py:444` | kept for compatibility |         self.stub("chatbot.nodes.support.call_chat_completion", side_effect=[ |
| `azure_function_main_app/tests/test_domain_orchestration.py:467` | kept for compatibility |         from chatbot.nodes.persist_context import persist_context |
| `azure_function_main_app/tests/test_domain_orchestration.py:469` | kept for compatibility |         self.stub("chatbot.nodes.persist_context.create_operational_storage", return_value=repository) |
| `azure_function_main_app/tests/test_domain_orchestration.py:483` | kept for compatibility |         from chatbot.nodes.hydrate_context import _merge_memory_context |
| `azure_function_main_app/tests/test_domain_architecture.py:112` | kept for compatibility |         old = importlib.import_module("chatbot.nodes.query_generator") |
| `azure_function_main_app/local_test.py:99` | kept for compatibility |     from chatbot.nodes.query_executor import _build_retry_candidates, _coverage_context |
| `azure_function_main_app/local_test.py:100` | kept for compatibility |     from chatbot.nodes.query_repair import query_repair as run_query_repair |
| `azure_function_main_app/local_test.py:149` | kept for compatibility |     from chatbot.nodes.intent_router import _fast_intent |
| `azure_function_main_app/local_test.py:151` | kept for compatibility |     from chatbot.threshold_monitor import _daily_sleep_query, _data_lines |
| `azure_function_main_app/local_test.py:3152` | kept for compatibility |         from chatbot.nodes.query_generator import ADX_SCHEMA_CONTEXT |
| `azure_function_main_app/local_test.py:3538` | kept for compatibility |     from chatbot.system_steps_alert import compose_steps_alert_message |
| `azure_function_main_app/chatbot/proactive/README.md:11` | kept for compatibility | The old chatbot.threshold_monitor and chatbot.system_steps_alert paths are |

## 이동 전 hit 중 갱신한 소비자

아래 행 번호는 이동 전 파일 기준이다. `updated`는 현재 runtime/도구/가이드에서 canonical 경로로 수정했거나, proactive 구현을 새 위치로 이전하면서 소비 경로를 바꾼 항목이다. 코드의 최종 위치는 본 보고서의 이동 표를 기준으로 한다.

| File:line before migration | Classification | Old reference |
|---|---|---|
| `azure_function_main_app/local_test.py:384` | updated |     chat_resource = "user_initiated/response_generator/chat.txt" |
| `azure_function_main_app/local_test.py:412` | updated |         "user_initiated/intent_router/classify.txt": 130, |
| `azure_function_main_app/local_test.py:413` | updated |         "user_initiated/plan_builder/build_data_plan.txt": 190, |
| `azure_function_main_app/local_test.py:414` | updated |         "user_initiated/query_generator/generate_kql.txt": 100, |
| `azure_function_main_app/local_test.py:415` | updated |         "user_initiated/query_repair/repair_kql.txt": 70, |
| `azure_function_main_app/local_test.py:416` | updated |         "user_initiated/response_generator/data_answer.txt": 248, |
| `azure_function_main_app/local_test.py:417` | updated |         "user_initiated/response_generator/chat.txt": 80, |
| `azure_function_main_app/local_test.py:418` | updated |         "user_initiated/response_generator/unsupported_metric.txt": 40, |
| `azure_function_main_app/local_test.py:442` | updated |         "shared/adx_schema_context.txt": {"user_id"}, |
| `azure_function_main_app/local_test.py:443` | updated |         "user_initiated/intent_router/classify.txt": { |
| `azure_function_main_app/local_test.py:451` | updated |         "user_initiated/plan_builder/build_data_plan.txt": { |
| `azure_function_main_app/local_test.py:462` | updated |         "user_initiated/query_generator/generate_kql.txt": { |
| `azure_function_main_app/local_test.py:475` | updated |         "user_initiated/query_repair/repair_kql.txt": { |
| `azure_function_main_app/local_test.py:483` | updated |         "user_initiated/response_generator/data_answer.txt": { |
| `azure_function_main_app/local_test.py:497` | updated |         "user_initiated/response_generator/chat.txt": { |
| `azure_function_main_app/local_test.py:503` | updated |         "user_initiated/response_generator/unsupported_metric.txt": { |
| `azure_function_main_app/local_test.py:520` | updated |     schema_context = load_prompt("shared/adx_schema_context.txt") |
| `azure_function_main_app/local_test.py:621` | updated |     intent_resource = "user_initiated/intent_router/classify.txt" |
| `azure_function_main_app/local_test.py:672` | updated |     plan_resource = "user_initiated/plan_builder/build_data_plan.txt" |
| `azure_function_main_app/local_test.py:742` | updated |     chat_resource = "user_initiated/response_generator/chat.txt" |
| `azure_function_main_app/local_test.py:771` | updated |     unsupported_resource = "user_initiated/response_generator/unsupported_metric.txt" |
| `azure_function_main_app/local_test.py:795` | updated |     data_resource = "user_initiated/response_generator/data_answer.txt" |
| `azure_function_main_app/local_test.py:912` | updated |     query_resource = "user_initiated/query_generator/generate_kql.txt" |
| `azure_function_main_app/local_test.py:953` | updated |         load_prompt("user_initiated/response_generator/data_answer.txt") |
| `azure_function_main_app/Instruction_modification_guide.md:122` | updated | chatbot/prompts/user_initiated/guided_input_nlu/. |
| `azure_function_main_app/Instruction_modification_guide.md:159` | updated | \| user_initiated/<node_owner>/ \| Routing, capability classification, planning, optional KQL generation/repair, responses, and alert configuration. \| The matching flat module in chatbot/nodes/ \| |
| `azure_function_main_app/Instruction_modification_guide.md:160` | updated | \| user_initiated/guided_input_nlu/ \| Small fallback classifiers/extractors for ambiguous onboarding and alert answers. \| chatbot/nlu.py and chatbot/timezone_onboarding.py \| |
| `azure_function_main_app/Instruction_modification_guide.md:161` | updated | \| system_initiated/threshold_monitor/ \| Optional threshold-alert rewriting and daily coaching. \| chatbot/threshold_monitor.py \| |
| `azure_function_main_app/Instruction_modification_guide.md:195` | updated | chatbot/prompts/user_initiated/intent_router/classify.txt. |
| `azure_function_main_app/Instruction_modification_guide.md:206` | updated | chatbot/prompts/user_initiated/capabilities_responder/classify.txt. |
| `azure_function_main_app/Instruction_modification_guide.md:213` | updated | - chatbot/prompts/shared/adx_schema_context.txt for the schema visible to query generation and repair; and |
| `azure_function_main_app/functions/threshold_monitor_bp.py:18` | updated | from chatbot.threshold_monitor import ( |
| `azure_function_main_app/functions/system_initiated_steps_bp.py:10` | updated | from chatbot.system_steps_alert import ( |
| `azure_function_main_app/functions/README.md:86` | updated |   -> chatbot.system_steps_alert |
| `azure_function_main_app/functions/README.md:91` | updated |   -> chatbot.threshold_monitor |
| `azure_function_main_app/chatbot/timezone_onboarding.py:252` | updated |         load_prompt("user_initiated/guided_input_nlu/timezone.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/threshold_monitor.py:264` | updated |             "system_initiated/threshold_monitor/rewrite_threshold_alert.txt" |
| `azure_function_main_app/chatbot/threshold_monitor.py:487` | updated |     "system_initiated/threshold_monitor/daily_report_coach.txt" |
| `azure_function_main_app/ARCHITECTURE.md:32` | updated | - chatbot.threshold_monitor owns threshold-breach messages and scheduled daily |
| `azure_function_main_app/ARCHITECTURE.md:34` | updated | - chatbot.system_steps_alert owns System STEPS outlier evaluation and its |
| `azure_function_main_app/chatbot/prompts/README.md:23` | updated | legacy user_initiated/... paths; the new folder READMEs record deferred |
| `azure_function_main_app/chatbot/prompts/README.md:81` | updated | \| The user sends a normal message \| user_initiated/ \| Active, but route-conditional. Deterministic fast paths can skip some or all model calls. \| |
| `azure_function_main_app/chatbot/prompts/README.md:82` | updated | \| Sensei sends a threshold alert or daily report first \| system_initiated/threshold_monitor/ \| Conditional. Threshold rewriting can be disabled; daily coaching runs only when a report contains data. \| |
| `azure_function_main_app/chatbot/prompts/README.md:83` | updated | \| Sensei sends a System STEPS outlier message first \| No prompt file \| Active deterministic path in chatbot.system_steps_alert; it does not call the model before sending. \| |
| `azure_function_main_app/chatbot/prompts/README.md:84` | updated | \| The user replies to a system-initiated message \| user_initiated/ \| The reply re-enters the ordinary graph with saved system-episode context; there is no separate reply prompt family. \| |
| `azure_function_main_app/chatbot/prompts/README.md:89` | updated | text. For example, user_initiated/response_generator/ contains Sensei's reply |
| `azure_function_main_app/chatbot/prompts/README.md:96` | updated | \|-- user_initiated/ |
| `azure_function_main_app/chatbot/prompts/README.md:105` | updated | \|-- system_initiated/ |
| `azure_function_main_app/chatbot/prompts/README.md:120` | updated | shared/adx_schema_context.txt, which supplies the same approved ADX contract |
| `azure_function_main_app/chatbot/prompts/README.md:127` | updated | \| user_initiated/intent_router/ \| User to system \| chatbot.nodes.intent_router selects data, chat, unsupported, clarification, capabilities, or alert_config. \| |
| `azure_function_main_app/chatbot/prompts/README.md:128` | updated | \| user_initiated/capabilities_responder/ \| User to system \| chatbot.nodes.capabilities_responder distinguishes timezone, profile, collected-data inventory, and general capability questions. \| |
| `azure_function_main_app/chatbot/prompts/README.md:129` | updated | \| user_initiated/plan_builder/ \| User to system \| chatbot.nodes.plan_builder emits a compatible simple plan or a typed computation plan for aggregates, filters, conditions, entity counts, and ordered selection. \| |
| `azure_function_main_app/chatbot/prompts/README.md:130` | updated | \| user_initiated/query_generator/ \| User to system \| chatbot.nodes.query_generator optionally generates KQL when the deterministic builders cannot produce a query and generic fallback is enabled. \| |
| `azure_function_main_app/chatbot/prompts/README.md:131` | updated | \| user_initiated/query_repair/ \| User to system \| chatbot.nodes.query_repair repairs a guard-rejected KQL candidate within the configured attempt limit. \| |
| `azure_function_main_app/chatbot/prompts/README.md:132` | updated | \| user_initiated/response_generator/ \| System reply to user \| chatbot.nodes.response_generator writes general chat, English unsupported-metric guidance, and grounded answers from validated query rows. \| |
| `azure_function_main_app/chatbot/prompts/README.md:133` | updated | \| user_initiated/alert_config_responder/ \| User to system \| chatbot.nodes.alert_config_responder extracts threshold-alert or scheduled-summary settings from conversational text. \| |
| `azure_function_main_app/chatbot/prompts/README.md:134` | updated | \| user_initiated/guided_input_nlu/ \| User to system \| chatbot.nlu and chatbot.timezone_onboarding resolve ambiguous guided-setup answers after deterministic parsing fails. \| |
| `azure_function_main_app/chatbot/prompts/README.md:135` | updated | \| system_initiated/threshold_monitor/ \| System to user \| chatbot.threshold_monitor optionally rewrites a threshold alert and produces the coaching portion of a daily report. \| |
| `azure_function_main_app/chatbot/prompts/README.md:183` | updated | \| Exact greeting or thanks in a supported language \| user_initiated/response_generator/chat.txt \| The top-level router is bypassed, but chat wording still uses the model. \| |
| `azure_function_main_app/chatbot/prompts/README.md:184` | updated | \| Other conversation or a general-health question \| planning/domain_execution_planning_prompt.py -> user_initiated/response_generator/chat.txt \| The chat prompt must not imply that personal records were queried. \| |
| `azure_function_main_app/chatbot/prompts/README.md:185` | updated | \| "Who are you?", profile facts, current timezone, or collected-data inventory \| planning/domain_execution_planning_prompt.py -> user_initiated/capabilities_responder/classify.txt \| The final profile, timezone, inventory, or capability reply is deterministic; no response prompt runs. \| |
| `azure_function_main_app/chatbot/prompts/README.md:186` | updated | \| New personal-data question \| planning/domain_execution_planning_prompt.py -> user_initiated/plan_builder/build_data_plan.txt -> conditional query task -> conditional data answer \| Deterministic KQL is primary. Error, no-data, entirely uncollected, and supported source-aware answers bypass the data-answer model. \| |
| `azure_function_main_app/chatbot/prompts/README.md:187` | updated | \| Behaviour-change support without a sensor request \| Domain planner -> D2 policy/schema -> user_initiated/response_generator/coaching_answer.txt \| D1 does not execute. D2 validation is deterministic. \| |
| `azure_function_main_app/chatbot/prompts/README.md:190` | updated | \| Unsupported personal metric in English \| planning/domain_execution_planning_prompt.py -> user_initiated/response_generator/unsupported_metric.txt \| No additional deterministic bypass. \| |
| `azure_function_main_app/chatbot/prompts/README.md:193` | updated | \| Natural-language threshold or scheduled-summary configuration \| planning/domain_execution_planning_prompt.py -> user_initiated/alert_config_responder/extract.txt \| Save, list, clear, and follow-up wording is deterministic. A pending answer of at most three words can bypass the router but still uses extraction. \| |
| `azure_function_main_app/chatbot/prompts/README.md:200` | updated | user_initiated/response_generator/chat.txt answers general-health questions |
| `azure_function_main_app/chatbot/prompts/README.md:211` | updated | Every plan from user_initiated/plan_builder/build_data_plan.txt includes |
| `azure_function_main_app/chatbot/prompts/README.md:284` | updated | user_initiated/intent_router/classify.txt |
| `azure_function_main_app/chatbot/prompts/README.md:285` | updated |   -> user_initiated/plan_builder/build_data_plan.txt |
| `azure_function_main_app/chatbot/prompts/README.md:288` | updated |      -> user_initiated/query_generator/generate_kql.txt |
| `azure_function_main_app/chatbot/prompts/README.md:290` | updated |      -> user_initiated/query_repair/repair_kql.txt |
| `azure_function_main_app/chatbot/prompts/README.md:294` | updated |      user_initiated/response_generator/data_answer.txt |
| `azure_function_main_app/chatbot/prompts/README.md:297` | updated | Both query tasks receive shared/adx_schema_context.txt. Loading that resource |
| `azure_function_main_app/chatbot/prompts/README.md:340` | updated | \| Age \| user_initiated/guided_input_nlu/age.txt \| Integer from 1 through 150, or NONE \| |
| `azure_function_main_app/chatbot/prompts/README.md:341` | updated | \| Gender \| user_initiated/guided_input_nlu/gender.txt \| Allowed gender token, or NONE \| |
| `azure_function_main_app/chatbot/prompts/README.md:342` | updated | \| Alert metric \| user_initiated/guided_input_nlu/metric.txt \| One caller-supplied metric code, or NONE \| |
| `azure_function_main_app/chatbot/prompts/README.md:343` | updated | \| Yes/no opt-in \| user_initiated/guided_input_nlu/opt_in.txt \| YES, NO, or UNCLEAR \| |
| `azure_function_main_app/chatbot/prompts/README.md:344` | updated | \| Automatic/manual setup \| user_initiated/guided_input_nlu/auto_manual.txt \| AUTO, MANUAL, or UNCLEAR \| |
| `azure_function_main_app/chatbot/prompts/README.md:345` | updated | \| Threshold bound \| user_initiated/guided_input_nlu/threshold_bound.txt \| One number, or NONE \| |
| `azure_function_main_app/chatbot/prompts/README.md:346` | updated | \| Sustained duration \| user_initiated/guided_input_nlu/duration.txt \| Positive integer minutes, or NONE \| |
| `azure_function_main_app/chatbot/prompts/README.md:347` | updated | \| Country or city \| user_initiated/guided_input_nlu/timezone.txt \| Valid IANA timezone, or NONE \| |
| `azure_function_main_app/chatbot/prompts/README.md:356` | updated | \| User-configured threshold breach \| Fifteen-minute timer or manual check; global feature and user rule enabled; sustained breach; outside cooldown \| system_initiated/threshold_monitor/rewrite_threshold_alert.txt, unless CHATBOT_THRESHOLD_ALERT_LLM=false \| Detection and facts are deterministic. Failure sends deterministic wording. No routing, planning, query, response, extraction, coaching, or guided NLU prompt runs. \| |
| `azure_function_main_app/chatbot/prompts/README.md:357` | updated | \| Scheduled daily report \| Five-minute timer when CHATBOT_DAILY_REPORT_ENABLED=true and a user's local schedule is due; manual report uses the same formatter \| system_initiated/threshold_monitor/daily_report_coach.txt, only when at least one data line exists; its [SYSTEM] and [USER] sections become the two messages in one model request \| ADX queries, aggregation, report structure, and fallback assessment are deterministic. No routing, planning, query-generation, response, extraction, or guided NLU prompt runs. \| |
| `azure_function_main_app/chatbot/prompts/README.md:383` | updated | \| Request to stop alerts or ask a capability question after a threshold or STEPS alert \| Normally router classification, then user_initiated/alert_config_responder/extract.txt or user_initiated/capabilities_responder/classify.txt \| |
| `azure_function_main_app/chatbot/prompts/README.md:396` | updated | \| user_initiated/query_generator/generate_kql.txt \| Explicit alternative path; runs only when KQL_GENERATE_METHOD=llm \| |
| `azure_function_main_app/chatbot/prompts/README.md:397` | updated | \| user_initiated/query_repair/repair_kql.txt \| Guard-conditional; default attempt limit is CHATBOT_MAX_KQL_REPAIR_ATTEMPTS=2 \| |
| `azure_function_main_app/chatbot/prompts/README.md:398` | updated | \| shared/adx_schema_context.txt \| Supporting context for generation and repair, not a standalone call \| |
| `azure_function_main_app/chatbot/prompts/README.md:401` | updated | \| system_initiated/threshold_monitor/daily_report_coach.txt \| Conditional on a report with nonempty collected data; there is no separate coach switch \| |
| `azure_function_main_app/chatbot/prompts/README.md:425` | updated | \| user_initiated/response_generator/coaching_answer.txt \| Final response generator \| Validated strategy plus optional D1 evidence; preserves a strategy-requested open question. \| |
| `azure_function_main_app/chatbot/prompts/README.md:431` | updated | \| user_initiated/intent_router/classify.txt \| Legacy chatbot.nodes.intent_router.intent_router() \| Retained legacy JSON intent; receives history, memory, reply-to context, prior plan, and system-episode context. \| |
| `azure_function_main_app/chatbot/prompts/README.md:432` | updated | \| user_initiated/capabilities_responder/classify.txt \| chatbot.nodes.capabilities_responder._classify_capability_subintent() \| Strict JSON target: timezone, profile, inventory, or general. \| |
| `azure_function_main_app/chatbot/prompts/README.md:433` | updated | \| user_initiated/plan_builder/build_data_plan.txt \| chatbot.nodes.plan_builder.plan_builder() \| Strict JSON version-1 or version-2 data plan with logical resources_priority; compact few-shots cover follow-ups, latest samples, aggregation, entity filters, conditions, and ordered selection. Python validates every field fail-closed. \| |
| `azure_function_main_app/chatbot/prompts/README.md:434` | updated | \| user_initiated/query_generator/generate_kql.txt \| chatbot.nodes.query_generator._generate_kql_with_llm() \| Raw read-only, user-scoped KQL only; active-profile resource placeholders are rendered at call time; default-off and guarded. \| |
| `azure_function_main_app/chatbot/prompts/README.md:435` | updated | \| user_initiated/query_repair/repair_kql.txt \| chatbot.nodes.query_repair.query_repair() \| Minimal raw-KQL correction using the same active-profile placeholders; receives the sanitized plan, preserves query intent, and returns to the guard. \| |
| `azure_function_main_app/chatbot/prompts/README.md:436` | updated | \| user_initiated/response_generator/chat.txt \| chatbot.nodes.response_generator.response_generator() \| Direct, concise general chat or health knowledge without routine data-access boilerplate, unsupported integration offers, or diagnosis. \| |
| `azure_function_main_app/chatbot/prompts/README.md:437` | updated | \| user_initiated/response_generator/unsupported_metric.txt \| English unsupported route in response_generator() \| Direct unavailability explanation with one related supported alternative only when useful. \| |
| `azure_function_main_app/chatbot/prompts/README.md:438` | updated | \| user_initiated/response_generator/data_answer.txt \| Non-deterministic successful data path in response_generator() \| Concise grounded answer using only validated rows, plan, coverage, and context; answer shape follows the user's requested detail. \| |
| `azure_function_main_app/chatbot/prompts/README.md:439` | updated | \| user_initiated/alert_config_responder/extract.txt \| chatbot.nodes.alert_config_responder.alert_config_responder() \| Strict JSON threshold or summary configuration specification. \| |
| `azure_function_main_app/chatbot/prompts/README.md:440` | updated | \| user_initiated/guided_input_nlu/age.txt \| chatbot.nlu.parse_age_with_source() \| Self-contained system/user task; age integer or NONE. \| |
| `azure_function_main_app/chatbot/prompts/README.md:441` | updated | \| user_initiated/guided_input_nlu/gender.txt \| chatbot.nlu.parse_gender_with_source() \| Self-contained system/user task; allowed gender token or NONE. \| |
| `azure_function_main_app/chatbot/prompts/README.md:442` | updated | \| user_initiated/guided_input_nlu/metric.txt \| chatbot.nlu._llm_metric() \| Self-contained system/user task; caller-approved metric code or NONE. \| |
| `azure_function_main_app/chatbot/prompts/README.md:443` | updated | \| user_initiated/guided_input_nlu/opt_in.txt \| chatbot.nlu.interpret_optin() \| Self-contained system/user task; YES, NO, or UNCLEAR. \| |
| `azure_function_main_app/chatbot/prompts/README.md:444` | updated | \| user_initiated/guided_input_nlu/auto_manual.txt \| chatbot.nlu.interpret_auto_manual() \| Self-contained system/user task; AUTO, MANUAL, or UNCLEAR. \| |
| `azure_function_main_app/chatbot/prompts/README.md:445` | updated | \| user_initiated/guided_input_nlu/threshold_bound.txt \| chatbot.nlu.interpret_bound() \| Self-contained system/user task; numeric bound or NONE. \| |
| `azure_function_main_app/chatbot/prompts/README.md:446` | updated | \| user_initiated/guided_input_nlu/duration.txt \| chatbot.nlu.interpret_duration() \| Self-contained system/user task; positive integer minutes or NONE. \| |
| `azure_function_main_app/chatbot/prompts/README.md:447` | updated | \| user_initiated/guided_input_nlu/timezone.txt \| chatbot.timezone_onboarding._llm_timezone() \| Self-contained system/user task; IANA timezone or NONE. \| |
| `azure_function_main_app/chatbot/prompts/README.md:448` | updated | \| shared/adx_schema_context.txt \| Query generator and repair \| Six-function Sensor contract, user/time scope, typed-null handling, source integrity, deduplication, and missing-data rules. \| |
| `azure_function_main_app/chatbot/prompts/README.md:449` | updated | \| system_initiated/threshold_monitor/rewrite_threshold_alert.txt \| chatbot.threshold_monitor._naturalize_alert() \| Calm English rewrite preserving every supplied fact; deterministic fallback on failure. \| |
| `azure_function_main_app/chatbot/prompts/README.md:450` | updated | \| system_initiated/threshold_monitor/daily_report_coach.txt \| chatbot.threshold_monitor._coach_narrative() \| One self-contained system/user task: grounding, safety, structure, current part of day, and measured daily metric lines. \| |
| `azure_function_main_app/chatbot/prompts/response/README.md:4` | updated | Existing resources remain under user_initiated/response_generator/ (chat, |
| `azure_function_main_app/chatbot/prompts/d1_sensor_data/README.md:4` | updated | resources remain at user_initiated/plan_builder/build_data_plan.txt, |
| `azure_function_main_app/chatbot/prompts/d1_sensor_data/README.md:5` | updated | user_initiated/query_generator/generate_kql.txt and |
| `azure_function_main_app/chatbot/prompts/d1_sensor_data/README.md:6` | updated | user_initiated/query_repair/repair_kql.txt, sharing shared/adx_schema_context.txt. |
| `azure_function_main_app/chatbot/nlu.py:92` | updated |     prompt = load_prompt("user_initiated/guided_input_nlu/metric.txt").rstrip("\n").format( |
| `azure_function_main_app/chatbot/nlu.py:166` | updated |         load_prompt("user_initiated/guided_input_nlu/age.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/nlu.py:213` | updated |         load_prompt("user_initiated/guided_input_nlu/gender.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/nlu.py:270` | updated |         load_prompt("user_initiated/guided_input_nlu/opt_in.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/nlu.py:309` | updated |         load_prompt("user_initiated/guided_input_nlu/auto_manual.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/nlu.py:373` | updated |         load_prompt("user_initiated/guided_input_nlu/threshold_bound.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/nlu.py:400` | updated |         load_prompt("user_initiated/guided_input_nlu/duration.txt").rstrip("\n").format(text=str(text or "").strip()), |
| `azure_function_main_app/chatbot/policies/README.md:176` | updated | - threshold and daily-report queries in chatbot.threshold_monitor; |
| `azure_function_main_app/chatbot/policies/README.md:177` | updated | - System STEPS MAD queries in chatbot.system_steps_alert. |
| `azure_function_main_app/chatbot/nodes/README.md:63` | updated | chatbot.threshold_monitor, chatbot.system_steps_alert and |
| `azure_function_main_app/chatbot/nodes/d1_sensor_data/generate_sensor_query.py:30` | updated | ADX_SCHEMA_CONTEXT = load_prompt("shared/adx_schema_context.txt") |
| `azure_function_main_app/chatbot/nodes/d1_sensor_data/generate_sensor_query.py:31` | updated | KQL_GENERATION_PROMPT_RESOURCE = "user_initiated/query_generator/generate_kql.txt" |
| `azure_function_main_app/chatbot/nodes/common/generate_final_response.py:89` | updated |     prompt = load_chat_prompt("user_initiated/response_generator/coaching_answer.txt") |
| `azure_function_main_app/chatbot/nodes/common/generate_final_response.py:161` | updated |         prompt = load_chat_prompt("user_initiated/response_generator/chat.txt") |
| `azure_function_main_app/chatbot/nodes/common/generate_final_response.py:197` | updated |             "user_initiated/response_generator/unsupported_metric.txt" |
| `azure_function_main_app/chatbot/nodes/common/generate_final_response.py:353` | updated |     prompt = load_chat_prompt("user_initiated/response_generator/data_answer.txt") |
| `azure_function_main_app/chatbot/nodes/d1_sensor_data/plan_sensor_query.py:110` | updated |     prompt = load_chat_prompt("user_initiated/plan_builder/build_data_plan.txt") |
| `azure_function_main_app/chatbot/nodes/d1_sensor_data/repair_sensor_query.py:21` | updated | ADX_SCHEMA_CONTEXT = load_prompt("shared/adx_schema_context.txt") |
| `azure_function_main_app/chatbot/nodes/d1_sensor_data/repair_sensor_query.py:34` | updated |     prompt = load_chat_prompt("user_initiated/query_repair/repair_kql.txt") |
| `azure_function_main_app/chatbot/nodes/operational/capabilities_responder.py:132` | updated |     "user_initiated/capabilities_responder/classify.txt" |
| `azure_function_main_app/chatbot/nodes/operational/alert_config_responder.py:44` | updated |     "user_initiated/alert_config_responder/extract.txt" |
| `azure_function_main_app/chatbot/nodes/planning/route_helpers.py:56` | updated | ROUTER_PROMPT = load_chat_prompt("user_initiated/intent_router/classify.txt") |
