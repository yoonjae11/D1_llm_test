# Sensei 실제 구현·프롬프트 소유권 리팩토링 결과

> **역사 문서:** 이 보고서는 3차 strict 작업 당시의 스냅샷입니다. 여기에 기록된
> root compatibility shim과 이전 prompt 이름은 후속 리팩토링에서 제거되었습니다.
> 현재 구조와 검증 결과는
> [현행 DAG/domain 보고서](DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md)를 기준으로 확인하세요.

작성일: 2026-09-08. `Sensei_Domain_Ownership_Refactor_STRICT_Codex_Spec.md`와 사용자의 proactive 분리 요청에 따라, 현재 `azure_function_main_app/` working tree를 이어서 수정했다. 기존 변경사항을 초기화하지 않았다. 모든 파일 경로는 별도 표시가 없으면 이 앱 기준이다.

## 1. 작업 전 실제 상태

파일 내용과 import를 확인한 결과 D1 실제 구현은 이미 `chatbot/nodes/d1_sensor_data/`에 있었고, D2 네 단계 하위 그래프 및 common·planning·operational 코드도 각 패키지에 있었다. 루트 node 14개는 5–8줄짜리 module identity shim으로, 중복 business logic은 없었다. D1 Python 파일 합계는 1,211 LOC, 기존 D1 shim 5개는 합계 40 LOC였다.

미완료 부분은 실제 prompt 소유권이었다. `d1_sensor_data/`와 `response/`에는 README만 있고, 실제 텍스트는 옛 `user_initiated/`와 `shared/` 경로에 있었다. 선제 알림·일일 보고 prompt도 `system_initiated/`에, 실행 구현은 `chatbot/threshold_monitor.py`와 `chatbot/system_steps_alert.py`에 남아 있었다.

이번에는 **실제 prompt 21개 전부와 proactive 구현 2개를 물리적으로 이동**했다. 21개 prompt의 이동 전후 SHA-256이 모두 같아 텍스트·개행·변수·role marker가 보존되었음을 확인했다. canonical node와 helper 및 local smoke의 소비 경로도 함께 변경했다.

## 2. Domain Ownership Audit

수정 전에 `nodes/`와 `prompts/`의 모든 소스·리소스·README 85개 및 추가 proactive 구현 2개, 총 87개를 읽고 분류했다. 캐시 파일은 제외했다. 아래 표는 **작업 전 위치와 역할** 및 **결정한 소유권**이다. 이동 대상별 새 위치는 3절에 있다.

명세의 분류 enum을 유지하면서 proactive는 OPERATIONAL의 선제 발신 책임으로 표시했다. 실제 파일은 사용자의 직접 요청에 따라 `chatbot/proactive/`와 `prompts/proactive/`로 분리했다. 기존 `episode_service.py`는 reactive와 proactive를 함께 다루므로 공용 위치를 유지했다. 재사용 가능한 service/policy와 NLU·timezone helper의 공개 API도 유지했다.

| ?? ? ?? | ?? | ??? | Canonical/shim | ?? ?? | ?? ?? |
|---|---|---|---|---|---|
| `chatbot/nodes/__init__.py` | """One canonical implementation module per registered LangGraph node.""" | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/alert_config_responder.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/capabilities_responder.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/common/__init__.py` | """common package ownership.""" | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/common/aggregate_domain_results.py` | aggregate_domain_results | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/common/generate_final_response.py` | call_user_facing_response; _user_facing_generate; _coaching_response; response_generator; generate_final_response | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/common/persist_trace_and_context.py` | _thread_id; _compact_turn; _assistant_text; persist_trace_and_context | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/common/prepare_context.py` | _thread_id; _merge_memory_context; hydrate_context; prepare_context | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/common/support.py` | call_chat_completion; _message_to_dict; _llm_invoke; latest_question; stored_turns_history | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/__init__.py` | """d1 sensor data package ownership.""" | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/execute_sensor_query.py` | _build_retry_candidates; _parse_row_date; _coverage_context; execute_sensor_query | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/generate_sensor_query.py` | _configured_model_name; _generation_trace; _generate_kql_with_llm; generate_kql_candidate; generate_sensor_query | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/guard_sensor_query.py` | guard_sensor_query | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/plan_sensor_query.py` | _enrich_plan_from_state; _resolve_all_available_for_state; _plan_node_result; plan_sensor_query | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/produce_sensor_result.py` | produce_sensor_result | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/repair_sensor_query.py` | repair_query_with_llm; repair_sensor_query | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/routing.py` | guard_decision; plan_decision; query_generation_decision | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d1_sensor_data/workflow.py` | build_d1_graph; run_d1 | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/__init__.py` | """d2 coaching strategy package ownership.""" | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/plan_coaching_strategy.py` | _fallback_strategy; plan_coaching_strategy | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/prepare_coaching_context.py` | build_coaching_context; prepare_coaching_context | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/produce_coaching_result.py` | produce_coaching_result | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/state.py` | D2State | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/validate_coaching_plan.py` | validate_coaching_plan; validate_coaching_strategy | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d2_coaching_strategy/workflow.py` | build_d2_graph; run_d2 | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/nodes/d3_health_knowledge/__init__.py` | """d3 health knowledge package ownership.""" | D3_HEALTH_KNOWLEDGE | canonical | No | canonical imports retained |
| `chatbot/nodes/d3_health_knowledge/placeholder_health_knowledge.py` | placeholder_health_knowledge | D3_HEALTH_KNOWLEDGE | canonical | No | canonical imports retained |
| `chatbot/nodes/d3_health_knowledge/workflow.py` | D3State; build_d3_graph; run_d3 | D3_HEALTH_KNOWLEDGE | canonical | No | canonical imports retained |
| `chatbot/nodes/d4_multiturn_context/__init__.py` | """d4 multiturn context package ownership.""" | D4_MULTITURN_CONTEXT | canonical | No | canonical imports retained |
| `chatbot/nodes/d4_multiturn_context/placeholder_multiturn_context.py` | placeholder_multiturn_context | D4_MULTITURN_CONTEXT | canonical | No | canonical imports retained |
| `chatbot/nodes/d4_multiturn_context/workflow.py` | D4State; build_d4_graph; run_d4 | D4_MULTITURN_CONTEXT | canonical | No | canonical imports retained |
| `chatbot/nodes/domain_orchestration.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/hydrate_context.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/intent_router.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/kql_guard.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/onboarding_responder.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/operational/__init__.py` | """operational package ownership.""" | OPERATIONAL | canonical | No | canonical imports retained |
| `chatbot/nodes/operational/alert_config_responder.py` | _to_minutes; _num; _msg; alert_config_responder; _save_alert | OPERATIONAL | canonical | No | canonical imports retained |
| `chatbot/nodes/operational/capabilities_responder.py` | _latest_user_text; _gender_label; _profile_answer; _humanize_metric_type; _format_local_date | OPERATIONAL | canonical | No | canonical imports retained |
| `chatbot/nodes/operational/onboarding_responder.py` | onboarding_responder | OPERATIONAL | canonical | No | canonical imports retained |
| `chatbot/nodes/orchestration/__init__.py` | """orchestration package ownership.""" | PLANNING | canonical | No | canonical imports retained |
| `chatbot/nodes/orchestration/execute_domains.py` | execute_domains | PLANNING | canonical | No | canonical imports retained |
| `chatbot/nodes/persist_context.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/plan_builder.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/planning/__init__.py` | """planning package ownership.""" | PLANNING | canonical | No | canonical imports retained |
| `chatbot/nodes/planning/plan_domain_execution.py` | plan_domain_execution | PLANNING | canonical | No | canonical imports retained |
| `chatbot/nodes/planning/route_helpers.py` | _norm; _fast_intent; intent_router; _route_name; _reply_block | PLANNING | canonical | No | canonical imports retained |
| `chatbot/nodes/planning/validate_domain_execution_plan.py` | validate_domain_execution_plan; safe_domain_execution_plan | PLANNING | canonical | No | canonical imports retained |
| `chatbot/nodes/query_executor.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/query_generator.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/query_repair.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/README.md` | Domain node ownership | COMMON | canonical | No | canonical imports retained |
| `chatbot/nodes/response_generator.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/nodes/support.py` | module identity alias | LEGACY_COMPAT | shim | No | legacy imports and patches |
| `chatbot/prompts/__init__.py` | """UTF-8 prompt resources used by the chatbot runtime. | COMMON | canonical | No | canonical imports retained |
| `chatbot/prompts/d1_sensor_data/README.md` | D1 prompt ownership | D1_SENSOR_DATA | canonical | No | canonical imports retained |
| `chatbot/prompts/d2_coaching_strategy/advisor_coaching_policy.py` | """General Advisor policy, separate from all evaluation material.""" | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/prompts/d2_coaching_strategy/coaching_strategy_planning_prompt.py` | coaching_strategy_messages | D2_COACHING_STRATEGY | canonical | No | canonical imports retained |
| `chatbot/prompts/d3_health_knowledge/README.md` | D3 placeholder | D3_HEALTH_KNOWLEDGE | canonical | No | canonical imports retained |
| `chatbot/prompts/d4_multiturn_context/README.md` | D4 placeholder | D4_MULTITURN_CONTEXT | canonical | No | canonical imports retained |
| `chatbot/prompts/loader.py` | _resource_path; load_prompt; split_role_prompt; load_role_prompt; load_chat_prompt | COMMON | canonical | No | canonical imports retained |
| `chatbot/prompts/planning/domain_execution_planning_prompt.py` | """Policy-only domain planning, combined with the legacy intent decision.""" | PLANNING | canonical | No | canonical imports retained |
| `chatbot/prompts/README.md` | Prompt Runtime Guide | COMMON | canonical | No | canonical imports retained |
| `chatbot/prompts/response/README.md` | Final response prompt ownership | COMMON | canonical | No | canonical imports retained |
| `chatbot/prompts/shared/adx_schema_context.txt` | You are a Kusto Query Language (KQL) expert for a personal health assistant. | D1_SENSOR_DATA | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/system_initiated/threshold_monitor/daily_report_coach.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/system_initiated/threshold_monitor/rewrite_threshold_alert.txt` | Rewrite this health alert as ONE warm, calm, natural English message (2-3 sentences). Keep ALL facts exactly: the detected time window, the metric and threshold, the peak value, th | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/alert_config_responder/extract.txt` | You extract an ALERT configuration from the user's message for a health chatbot. | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/capabilities_responder/classify.txt` | You route a meta question in a connected health-data chatbot (curated mobile | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/age.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/auto_manual.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/duration.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/gender.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/metric.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/opt_in.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/threshold_bound.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/guided_input_nlu/timezone.txt` | [SYSTEM] | OPERATIONAL | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/intent_router/classify.txt` | You route one user message for a personal health assistant. Choose exactly one | PLANNING | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/plan_builder/build_data_plan.txt` | Convert the user's personal-sensor question into one structured semantic plan. | D1_SENSOR_DATA | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/query_generator/generate_kql.txt` | Generate one read-only KQL query from the sanitized plan. | D1_SENSOR_DATA | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/query_repair/repair_kql.txt` | Repair one KQL query that failed validation. Make the smallest semantic change | D1_SENSOR_DATA | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/response_generator/chat.txt` | You are a friendly health assistant. Answer in {answer_language} with proper | COMMON | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/response_generator/coaching_answer.txt` | You write the final user-facing answer of a health-coaching Advisor in | COMMON | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/response_generator/data_answer.txt` | You answer a user's personal health-data question in {answer_language}. | COMMON | canonical | Yes | update runtime/tooling resource references |
| `chatbot/prompts/user_initiated/response_generator/unsupported_metric.txt` | Answer in {answer_language} as a concise health assistant. | COMMON | canonical | Yes | update runtime/tooling resource references |
| `chatbot/threshold_monitor.py` | proactive alerts/daily report orchestration | OPERATIONAL | canonical | Yes | Function blueprint and local tooling; retain identity shim |
| `chatbot/system_steps_alert.py` | proactive STEPS evaluation/delivery | OPERATIONAL | canonical | Yes | Function blueprint and local tooling; retain identity shim |

분류와 이동 전 파일 해시는 로컬 `test-results/strict-ownership-baseline.json`에 기록했다. 이후 신규 README·테스트는 아래 구조를 설명하거나 검증하기 위한 파일이다.

## 3. 물리 이동된 파일

### 실제 prompt 원본

아래 경로는 `chatbot/prompts/` 기준이다. 복사본을 두지 않고 원본을 이동했으며, 모든 loader 소비자를 새 경로로 수정했다.

| Old path | New canonical path | SHA-256 ?? |
|---|---|---|
| `user_initiated/plan_builder/build_data_plan.txt` | `d1_sensor_data/build_sensor_data_plan.txt` | PASSED |
| `user_initiated/query_generator/generate_kql.txt` | `d1_sensor_data/generate_sensor_kql.txt` | PASSED |
| `user_initiated/query_repair/repair_kql.txt` | `d1_sensor_data/repair_sensor_kql.txt` | PASSED |
| `shared/adx_schema_context.txt` | `d1_sensor_data/adx_schema_context.txt` | PASSED |
| `user_initiated/response_generator/chat.txt` | `response/chat_answer.txt` | PASSED |
| `user_initiated/response_generator/data_answer.txt` | `response/sensor_data_answer.txt` | PASSED |
| `user_initiated/response_generator/coaching_answer.txt` | `response/coaching_answer.txt` | PASSED |
| `user_initiated/response_generator/unsupported_metric.txt` | `response/unsupported_metric.txt` | PASSED |
| `user_initiated/intent_router/classify.txt` | `planning/legacy_intent_classification.txt` | PASSED |
| `user_initiated/alert_config_responder/extract.txt` | `operational/alert_config/extract.txt` | PASSED |
| `user_initiated/capabilities_responder/classify.txt` | `operational/capabilities/classify.txt` | PASSED |
| `system_initiated/threshold_monitor/rewrite_threshold_alert.txt` | `proactive/threshold_alert/rewrite_threshold_alert.txt` | PASSED |
| `system_initiated/threshold_monitor/daily_report_coach.txt` | `proactive/daily_report/daily_report_coach.txt` | PASSED |
| `user_initiated/guided_input_nlu/age.txt` | `operational/guided_input/age.txt` | PASSED |
| `user_initiated/guided_input_nlu/auto_manual.txt` | `operational/guided_input/auto_manual.txt` | PASSED |
| `user_initiated/guided_input_nlu/duration.txt` | `operational/guided_input/duration.txt` | PASSED |
| `user_initiated/guided_input_nlu/gender.txt` | `operational/guided_input/gender.txt` | PASSED |
| `user_initiated/guided_input_nlu/metric.txt` | `operational/guided_input/metric.txt` | PASSED |
| `user_initiated/guided_input_nlu/opt_in.txt` | `operational/guided_input/opt_in.txt` | PASSED |
| `user_initiated/guided_input_nlu/threshold_bound.txt` | `operational/guided_input/threshold_bound.txt` | PASSED |
| `user_initiated/guided_input_nlu/timezone.txt` | `operational/guided_input/timezone.txt` | PASSED |

### 실제 proactive 구현

| 기존 파일 | 현재 canonical 구현 | 기존 경로 처리 |
|---|---|---|
| `chatbot/threshold_monitor.py` | `chatbot/proactive/threshold_monitor.py` | 5줄 compatibility shim |
| `chatbot/system_steps_alert.py` | `chatbot/proactive/system_steps_alert.py` | 5줄 compatibility shim |

이 두 모듈은 timer/queue에서 시작되는 기존 선제 처리이므로 reactive LangGraph의 D1–D4 node에 넣지 않았다. `chatbot/proactive/`가 실제 코드 소유자이며, 두 Function blueprint는 새 canonical 모듈을 직접 import한다. detector·cooldown·retry·quiet hours·메시지 조립·저장·전송 처리는 기존 구현을 그대로 이전했다. threshold 모듈에서 바꾼 실행 코드는 두 prompt 경로뿐이다.

### 함께 수정하거나 추가한 파일

| File | Change |
|---|---|
| [chatbot/proactive/README.md](../chatbot/proactive/README.md) | Added |
| [chatbot/proactive/__init__.py](../chatbot/proactive/__init__.py) | Added |
| [chatbot/prompts/operational/README.md](../chatbot/prompts/operational/README.md) | Added |
| [chatbot/prompts/proactive/README.md](../chatbot/prompts/proactive/README.md) | Added |
| [docs/STRICT_DOMAIN_OWNERSHIP_REPORT_KR.md](../docs/STRICT_DOMAIN_OWNERSHIP_REPORT_KR.md) | Added |
| [docs/STRICT_OWNERSHIP_REFERENCE_AUDIT_KR.md](../docs/STRICT_OWNERSHIP_REFERENCE_AUDIT_KR.md) | Added |
| [tests/test_strict_ownership.py](../tests/test_strict_ownership.py) | Added |
| [.funcignore](../.funcignore) | Modified |
| [.gitignore](../.gitignore) | Modified |
| [ARCHITECTURE.md](../ARCHITECTURE.md) | Modified |
| [Instruction_modification_guide.md](../Instruction_modification_guide.md) | Modified |
| [chatbot/README.md](../chatbot/README.md) | Modified |
| [chatbot/nlu.py](../chatbot/nlu.py) | Modified |
| [chatbot/nodes/README.md](../chatbot/nodes/README.md) | Modified |
| [chatbot/nodes/common/generate_final_response.py](../chatbot/nodes/common/generate_final_response.py) | Modified |
| [chatbot/nodes/d1_sensor_data/generate_sensor_query.py](../chatbot/nodes/d1_sensor_data/generate_sensor_query.py) | Modified |
| [chatbot/nodes/d1_sensor_data/plan_sensor_query.py](../chatbot/nodes/d1_sensor_data/plan_sensor_query.py) | Modified |
| [chatbot/nodes/d1_sensor_data/repair_sensor_query.py](../chatbot/nodes/d1_sensor_data/repair_sensor_query.py) | Modified |
| [chatbot/nodes/operational/alert_config_responder.py](../chatbot/nodes/operational/alert_config_responder.py) | Modified |
| [chatbot/nodes/operational/capabilities_responder.py](../chatbot/nodes/operational/capabilities_responder.py) | Modified |
| [chatbot/nodes/planning/route_helpers.py](../chatbot/nodes/planning/route_helpers.py) | Modified |
| [chatbot/policies/README.md](../chatbot/policies/README.md) | Modified |
| [chatbot/prompts/README.md](../chatbot/prompts/README.md) | Modified |
| [chatbot/prompts/d1_sensor_data/README.md](../chatbot/prompts/d1_sensor_data/README.md) | Modified |
| [chatbot/prompts/response/README.md](../chatbot/prompts/response/README.md) | Modified |
| [chatbot/system_steps_alert.py](../chatbot/system_steps_alert.py) | Modified |
| [chatbot/threshold_monitor.py](../chatbot/threshold_monitor.py) | Modified |
| [chatbot/timezone_onboarding.py](../chatbot/timezone_onboarding.py) | Modified |
| [docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md](../docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md) | Modified |
| [docs/D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md](../docs/D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md) | Modified |
| [docs/refactoring/MIGRATION_MAP.md](../docs/refactoring/MIGRATION_MAP.md) | Modified |
| [docs/refactoring/README.md](../docs/refactoring/README.md) | Modified |
| [docs/refactoring/TO_BE.md](../docs/refactoring/TO_BE.md) | Modified |
| [functions/README.md](../functions/README.md) | Modified |
| [functions/system_initiated_steps_bp.py](../functions/system_initiated_steps_bp.py) | Modified |
| [functions/threshold_monitor_bp.py](../functions/threshold_monitor_bp.py) | Modified |
| [local_test.py](../local_test.py) | Modified |
| [tests/test_domain_architecture.py](../tests/test_domain_architecture.py) | Modified |

## 4. 삭제된 old file

3절의 prompt 원본 21개는 새 위치에서 보존되며 **이전 경로의 파일은 남기지 않았다**. 소비자가 모두 새 경로를 사용하므로 prompt alias나 fallback loader를 두지 않았다. 빈 `prompts/user_initiated/`, `prompts/system_initiated/`, `prompts/shared/`와 그 하위 빈 폴더도 제거했다. prompt 내용은 삭제하지 않았다.

기존 public Python 모듈은 명확한 호환 계약이 있어 shim으로 유지했다. 사용하지 않는 것으로 추정해 동작 구현을 삭제한 파일은 없다. Git의 reset/restore/clean을 사용하지 않았다.

## 5. 남은 compatibility shim

모든 shim은 canonical 모듈을 가리키는 하나의 import alias다. 함수·클래스·정책·LLM/ADX 호출·graph node body가 없다. canonical 구현이 shim을 역으로 import하지 않는다. `sys.modules`의 동일 객체를 사용해 기존 `mock.patch`와 helper import도 유지했다.

| Shim | LOC | ???? ? import | Canonical module |
|---|---|---|---|
| `chatbot/nodes/alert_config_responder.py` | 8 | `chatbot.nodes.alert_config_responder` | `chatbot.nodes.operational.alert_config_responder` |
| `chatbot/nodes/capabilities_responder.py` | 8 | `chatbot.nodes.capabilities_responder` | `chatbot.nodes.operational.capabilities_responder` |
| `chatbot/nodes/domain_orchestration.py` | 5 | `chatbot.nodes.domain_orchestration` | `chatbot.nodes.orchestration.execute_domains` |
| `chatbot/nodes/hydrate_context.py` | 8 | `chatbot.nodes.hydrate_context` | `chatbot.nodes.common.prepare_context` |
| `chatbot/nodes/intent_router.py` | 8 | `chatbot.nodes.intent_router` | `chatbot.nodes.planning.route_helpers` |
| `chatbot/nodes/kql_guard.py` | 8 | `chatbot.nodes.kql_guard` | `chatbot.nodes.d1_sensor_data.guard_sensor_query` |
| `chatbot/nodes/onboarding_responder.py` | 8 | `chatbot.nodes.onboarding_responder` | `chatbot.nodes.operational.onboarding_responder` |
| `chatbot/nodes/persist_context.py` | 8 | `chatbot.nodes.persist_context` | `chatbot.nodes.common.persist_trace_and_context` |
| `chatbot/nodes/plan_builder.py` | 8 | `chatbot.nodes.plan_builder` | `chatbot.nodes.d1_sensor_data.plan_sensor_query` |
| `chatbot/nodes/query_executor.py` | 8 | `chatbot.nodes.query_executor` | `chatbot.nodes.d1_sensor_data.execute_sensor_query` |
| `chatbot/nodes/query_generator.py` | 8 | `chatbot.nodes.query_generator` | `chatbot.nodes.d1_sensor_data.generate_sensor_query` |
| `chatbot/nodes/query_repair.py` | 8 | `chatbot.nodes.query_repair` | `chatbot.nodes.d1_sensor_data.repair_sensor_query` |
| `chatbot/nodes/response_generator.py` | 8 | `chatbot.nodes.response_generator` | `chatbot.nodes.common.generate_final_response` |
| `chatbot/nodes/support.py` | 8 | `chatbot.nodes.support` | `chatbot.nodes.common.support` |
| `chatbot/system_steps_alert.py` | 5 | `chatbot.system_steps_alert` | `chatbot.proactive.system_steps_alert` |
| `chatbot/threshold_monitor.py` | 5 | `chatbot.threshold_monitor` | `chatbot.proactive.threshold_monitor` |

`nodes/` 루트 14개는 이전 작업에서 문서화한 public import 계약을 유지한다. 특히 평가 runner와 local smoke가 D1 helper와 callable을 사용한다. 나머지 root operational/common alias도 기존 공개 경로의 호환 약속을 유지하기 위해 남겼다. proactive 2개 alias는 `local_test.py`와 기존 module 소비자를 보호한다. 새 production 코드는 canonical path를 사용한다.

## 6. 최종 nodes tree

실제 파일 시스템에서 생성했다. `__pycache__`는 제외했다.

```text
chatbot/nodes/
|-- common/
|   |-- __init__.py
|   |-- aggregate_domain_results.py
|   |-- generate_final_response.py
|   |-- persist_trace_and_context.py
|   |-- prepare_context.py
|   `-- support.py
|-- d1_sensor_data/
|   |-- __init__.py
|   |-- execute_sensor_query.py
|   |-- generate_sensor_query.py
|   |-- guard_sensor_query.py
|   |-- plan_sensor_query.py
|   |-- produce_sensor_result.py
|   |-- repair_sensor_query.py
|   |-- routing.py
|   `-- workflow.py
|-- d2_coaching_strategy/
|   |-- __init__.py
|   |-- plan_coaching_strategy.py
|   |-- prepare_coaching_context.py
|   |-- produce_coaching_result.py
|   |-- state.py
|   |-- validate_coaching_plan.py
|   `-- workflow.py
|-- d3_health_knowledge/
|   |-- __init__.py
|   |-- placeholder_health_knowledge.py
|   `-- workflow.py
|-- d4_multiturn_context/
|   |-- __init__.py
|   |-- placeholder_multiturn_context.py
|   `-- workflow.py
|-- operational/
|   |-- __init__.py
|   |-- alert_config_responder.py
|   |-- capabilities_responder.py
|   `-- onboarding_responder.py
|-- orchestration/
|   |-- __init__.py
|   `-- execute_domains.py
|-- planning/
|   |-- __init__.py
|   |-- plan_domain_execution.py
|   |-- route_helpers.py
|   `-- validate_domain_execution_plan.py
|-- README.md
|-- __init__.py
|-- alert_config_responder.py
|-- capabilities_responder.py
|-- domain_orchestration.py
|-- hydrate_context.py
|-- intent_router.py
|-- kql_guard.py
|-- onboarding_responder.py
|-- persist_context.py
|-- plan_builder.py
|-- query_executor.py
|-- query_generator.py
|-- query_repair.py
|-- response_generator.py
`-- support.py
```

**[LEGACY COMPATIBILITY SHIM]**: 트리 루트의 14개 옛 node 파일은 5절 목록과 동일하다. 실질 구현은 하위 소유 패키지에만 있다.

선제 동작의 실제 코드 트리도 별도로 확인했다.

```text
chatbot/proactive/
|-- README.md
|-- __init__.py
|-- system_steps_alert.py
`-- threshold_monitor.py
```

## 7. 최종 prompts tree

실제 파일 시스템에서 생성한 전체 prompt 트리다.

```text
chatbot/prompts/
|-- d1_sensor_data/
|   |-- README.md
|   |-- adx_schema_context.txt
|   |-- build_sensor_data_plan.txt
|   |-- generate_sensor_kql.txt
|   `-- repair_sensor_kql.txt
|-- d2_coaching_strategy/
|   |-- advisor_coaching_policy.py
|   `-- coaching_strategy_planning_prompt.py
|-- d3_health_knowledge/
|   `-- README.md
|-- d4_multiturn_context/
|   `-- README.md
|-- operational/
|   |-- alert_config/
|   |   `-- extract.txt
|   |-- capabilities/
|   |   `-- classify.txt
|   |-- guided_input/
|   |   |-- age.txt
|   |   |-- auto_manual.txt
|   |   |-- duration.txt
|   |   |-- gender.txt
|   |   |-- metric.txt
|   |   |-- opt_in.txt
|   |   |-- threshold_bound.txt
|   |   `-- timezone.txt
|   `-- README.md
|-- planning/
|   |-- domain_execution_planning_prompt.py
|   `-- legacy_intent_classification.txt
|-- proactive/
|   |-- daily_report/
|   |   `-- daily_report_coach.txt
|   |-- threshold_alert/
|   |   `-- rewrite_threshold_alert.txt
|   `-- README.md
|-- response/
|   |-- README.md
|   |-- chat_answer.txt
|   |-- coaching_answer.txt
|   |-- sensor_data_answer.txt
|   `-- unsupported_metric.txt
|-- README.md
|-- __init__.py
`-- loader.py
```

D1은 실제 `.txt` 4개, final response도 실제 `.txt` 4개를 가진다. operational에는 설정·capabilities·guided input 10개, proactive에는 2개, planning에는 legacy classifier 1개가 있다. 합계 21개이며 D2의 Python 정책·schema assembler는 별도다. D3·D4는 미구현 기능에 대한 README만 갖고 빈 prompt를 호출하지 않는다.

## 8. D1 실행 흐름

```text
START -> plan_sensor_query -> generate_sensor_query -> guard_sensor_query
                                                      | execute -> execute_sensor_query
                                                      | repair -> repair_sensor_query
                                                      |              -> guard_sensor_query
         invalid/empty/blocked paths -----------------> produce_sensor_result
execute_sensor_query --------------------------------> produce_sensor_result -> END
```

기존 D1 내부 wiring·구현·policy/service는 유지했다. canonical `workflow.py`는 로컬 D1 구현을 import하며 root shim을 호출하지 않는다. typed plan 검증, metric/time range, KQL guard, 제한된 repair 후 재검증, guarded ADX 실행, coverage·0/null·missing/error 의미를 유지한다.

`plan_sensor_query.py`, `generate_sensor_query.py`, `repair_sensor_query.py`는 이제 `d1_sensor_data/`의 실제 prompt를 load한다. ADX schema context도 D1 안에서 generator/repair가 함께 사용한다. `build_d1_graph()`와 `run_d1(state)` interface는 유지했다.

## 9. D2 실행 흐름

```text
START -> prepare_coaching_context -> plan_coaching_strategy
      -> validate_coaching_plan -> produce_coaching_result -> END
```

이미 존재하던 실제 네 단계 LangGraph와 로컬 모듈 소유권을 검증했다. D2 모델 호출은 전략 생성 단계 최대 1회이며 준비·검증·result wrapping은 deterministic Python이다. applicability, dialogue objective, COM-B, BCT interface, MI/SDT, interaction depth 및 응답 제약을 담은 구조화 전략을 만든다.

D2 Advisor 정책·schema prompt는 `prompts/d2_coaching_strategy/`에 유지했다. 최종 자연어 coaching prompt는 `prompts/response/coaching_answer.txt`로 옮겼으며 common response가 load한다. D2에는 Auditor 점수·정답 평가·최종 문장 생성 책임을 넣지 않았다.

## 10. D3/D4 placeholder interface

| 도메인 | 실제 interface | 현재 결과 |
|---|---|---|
| D3 | `nodes/d3_health_knowledge/workflow.py`: `build_d3_graph()`, `run_d3(state)` | `not_implemented`, 빈 structured result |
| D4 | `nodes/d4_multiturn_context/workflow.py`: `build_d4_graph()`, `run_d4(state)` | `not_implemented`, 빈 structured result |

각각 명시적 placeholder node가 있는 compiled graph다. registry 등록과 planner 선택 가능성을 유지했고, 모델·검색·저장소 호출이나 허위 근거를 추가하지 않았다. 기존 공통 hydration을 새 D4 구현이라고 재분류하지 않았다.

## 11. Generic orchestration

상위 그래프는 prepare → plan → execute → aggregate → final response → persist의 6개 노드다. `DOMAIN_RUNNERS`는 D1–D4 네 runner를 공통 `DomainRunOutput` interface로 실행한다.

`selected_domains`, `domain_requirements`, `dependencies`, `execution_stages`, `early_exit_conditions` schema를 유지했다. D1 only, D2 only, D1 → D2, D1 ∥ D2, D4 → D1, D1 ∥ D3, [D1 ∥ D3] → D2, D4 → D1 → D2를 표현한다. 최대 3단계, 순환·반복 실행·잘못된 순서 차단, 독립 작업 병렬 실행, generic dependency 전달은 기존 테스트로 재검증했다.

required dependency의 상태가 success가 아니면 후속 runner를 호출하지 않는다. optional 실패는 실제 상태와 missing information을 전달한다. D3·D4가 미구현인 상황을 성공 근거로 취급하지 않으며, 공통 응답이 미완료 부분을 알린다.

## 12. Prompt loader 변경

중앙 `loader.py` 구현은 변경하지 않았다. 상대경로 검증·UTF-8·캐시·`[SYSTEM]`/`[USER]` 분리·기존 chat template 생성 방식을 유지했다. 다음 소비자의 **resource 문자열**만 새 소유 경로로 변경했다.

- D1의 plan/generate/repair 및 공통 final response.
- planning의 legacy intent classifier, operational의 alert/capabilities responder.
- `chatbot/nlu.py`와 `timezone_onboarding.py`의 guided-input fallback.
- `chatbot/proactive/threshold_monitor.py`의 alert rewrite 및 daily-report narrative.
- `local_test.py`의 resource 목록·변수·내용 계약 검사.

평가 코드에는 옛 prompt 경로 직접 참조가 없어 수정할 필요가 없었다. 기존 Python node import는 shim으로 보존했다. 실제 prompt 21개가 모두 runtime Python 소비자를 가진다는 AST 검사를 추가했다.

필수 구형 경로 검색 결과를 아래와 같이 분리했다. 보고서·과거 구조 문서의 경로 설명은 실행 참조와 구분한다.

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

각 hit의 파일·행·내용·처리 구분은 [repository-wide reference audit](STRICT_OWNERSHIP_REFERENCE_AUDIT_KR.md)에 기록했다. `updated`는 실제 코드/도구/현재 문서를 바꾼 경우, `kept for compatibility`는 기존 Python 소비자, `intentionally deferred`는 과거 시점 기록을 보존하는 문서다. **runtime prompt 이동을 유보한 항목은 없다.** 이 보고서의 old → new 표와 audit 자체는 이관 기록이다.

## 13. Backward compatibility 영향

| 기존 동작 | 보존 및 로컬 확인 |
|---|---|
| Telegram 일반 대화·reply·thread | 기존 entrypoint·handler·문맥 계약 유지, mock 회귀 |
| `/help`, `/login`, `/register_user`, `/start`, `/cancel` | 기존 handler와 분기 유지, mock 회귀 |
| Onboarding·timezone | 기존 입력 helper와 결정적 경로 유지, prompt fallback 경로만 이동 |
| Sensor/ADX/KQL | 기존 D1 구현 유지, typed plan·guard·mocked ADX·missing/error·0/null 회귀 |
| Alert/daily-summary 설정 | 기존 operational responder 및 설정 저장 계약 유지 |
| 선제 threshold 알림 | detector·cooldown·성공 전송 후 state 갱신·모델 오류 fallback을 mock으로 검증 |
| 선제 daily report | 수집된 데이터·role 분리·빈 데이터 시 모델 생략·후속 plan/시간대 전달을 mock으로 검증 |
| STEPS 선제 동작 | baseline/candidate 처리·중복 처리 차단·전송 상태를 mock으로 검증 |
| Function registration | 기존 blueprint가 canonical proactive 구현을 import, binding 25개 smoke 확인 |
| 평가/local tooling | 기존 module identity·helper import 유지, 평가 26개 및 local smoke 통과 |

`.funcignore`의 기존 비고정 `proactive/` 패턴은 새 nested runtime 코드·prompt까지 제외할 수 있으므로 `/proactive/`로 고정했다. 루트의 과거 실험 폴더 제외는 유지하면서 `chatbot/proactive/`와 `chatbot/prompts/proactive/`를 포함하는 구조다. 배포는 실행하지 않았다.

새 module 위치에 따라 proactive의 Python logger 이름은 `chatbot.proactive.*`가 된다. 기존 Function binding명, 환경변수명, 저장 entity·ADX query/result 계약, prompt 내용 및 사용자 응답 계약은 유지했다.

## 14. 테스트 결과

로컬 Python 3.12 `.venv`에서 검증했다. 서비스 경계는 mock으로 대체했고 외부 socket 연결도 차단하는 테스트 기반을 사용했다.

| 검증 | 결과 | 실제 범위 |
|---|---|---|
| 기존 app 회귀·domain 구조 | PASSED | 67개 |
| 추가 physical ownership·proactive 회귀 | PASSED | 11개 |
| `evaluation.test_offline`, `evaluation.test_diagnostics` | PASSED | 26개 |
| 전체 unittest | PASSED | 합계 **104개**, FAILED 0개 |
| `local_test.py smoke` | PASSED | Function 25개, 상위 node 6개, prompt 21개, role prompt 9개 및 기존 KQL/plan 검사 |
| prompt 이동 전후 해시 비교 | PASSED | 21/21 동일 |
| Python compileall | PASSED | Function App 및 evaluation |
| `git diff --check`·신규 소스 공백 검사 | PASSED | 기존 변경 및 이번 신규 코드/문서 |
| 실제 Telegram/ADX/Table Storage/CC 호출·실제 모델 품질 평가 | NOT EXECUTED | 사용자의 로컬 작업 범위 유지 |
| Git commit/push/merge/rebase/branch 변경·배포 | NOT EXECUTED | 사용자 금지 요청 준수 |

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s azure_function_main_app/tests -v
.\.venv\Scripts\python.exe -m unittest evaluation.test_offline evaluation.test_diagnostics
.\.venv\Scripts\python.exe azure_function_main_app/local_test.py smoke
.\.venv\Scripts\python.exe -m compileall -q azure_function_main_app evaluation
git diff --check
```

실제 서비스 환경변수 없이 import/smoke 과정에서 ADX 설정 미존재 로그가 출력되었으나, 테스트는 로컬 mock으로 완료했다. 의도적으로 주입한 모델·runner 오류 로그도 해당 fallback 검증의 일부다. 이 결과를 실서비스 연결 또는 실제 모델 답변 품질 검증으로 보고하지 않는다.

이번에 추가한 11개 테스트의 최종 결과:

| ?? ??? | ?? |
|---|---|
| `PhysicalOwnershipTests.test_all_text_resources_are_real_and_owned_without_legacy_copies` | PASSED |
| `PhysicalOwnershipTests.test_runtime_resource_references_resolve_and_match_their_owner` | PASSED |
| `PhysicalOwnershipTests.test_d1_implementation_is_larger_than_shims_and_all_resources_load` | PASSED |
| `PhysicalOwnershipTests.test_role_prompts_keep_system_user_boundaries` | PASSED |
| `PhysicalOwnershipTests.test_canonical_code_and_low_level_services_do_not_import_root_nodes` | PASSED |
| `PhysicalOwnershipTests.test_proactive_implementations_and_function_imports_use_canonical_modules` | PASSED |
| `ProactiveRegressionTests.test_threshold_rewrite_loads_moved_prompt_and_keeps_error_fallback` | PASSED |
| `ProactiveRegressionTests.test_threshold_cooldown_and_successful_delivery_keep_state_behavior` | PASSED |
| `ProactiveRegressionTests.test_daily_report_preserves_collected_data_prompt_roles_and_followup` | PASSED |
| `ProactiveRegressionTests.test_empty_daily_report_does_not_invent_rows_or_call_model` | PASSED |
| `ProactiveRegressionTests.test_steps_detector_keeps_candidate_and_idempotence_without_external_calls` | PASSED |

## 15. 남은 TODO

- D3 실제 knowledge/RAG/safety 기능 구현: 현재 explicit placeholder.
- D4 실제 추가 문맥/기억 기능 구현: 현재 explicit placeholder, 기존 hydration은 공통 기능으로 유지.
- 출처가 확인된 BCT catalogue 제공: 현재 loader/검증 interface를 유지하며 빈 catalogue에서는 선택을 비움.
- 실제 서비스·모델 품질 확인: 사용자가 승인한 별도 환경에서 후속 수행.

**D1·response·operational·proactive prompt의 물리 이동 TODO는 완료했다.** prompt 리소스 중복, 빈 fake prompt, runtime의 옛 prompt 경로는 남기지 않았다. public Python shim 제거는 별도의 명시적 호환 계약 폐기 없이는 진행하지 않는다.

## 16. main → red_teaming 향후 충돌 예상 파일

다른 브랜치를 전환·병합하거나 원격 상태를 가져오지 않았다. 아래는 실제 working tree 수정에 따른 예상이며 실제 충돌 결과는 아니다.

| 실제 변경 영역 | 통합 시 확인 사항 |
|---|---|
| D1·response의 기존 prompt 경로 삭제 및 새 `.txt` 8개 | 상대 브랜치의 prompt 내용 변경은 새 canonical 경로에 적용; 옛 복사본 부활 방지 |
| planning·operational·proactive prompt 이동 13개 | classifier/guided-input/alert/report 참조 경로와 prompt 변수 계약 |
| `nodes/d1_sensor_data/{plan,generate,repair}_sensor_query.py`, `common/generate_final_response.py` | red_teaming의 prompt hook 또는 resource path 기반 계측 |
| `chatbot/proactive/threshold_monitor.py`, `system_steps_alert.py`와 루트 shim | 상대 브랜치가 옛 full module을 수정한 경우 새 구현 파일로 반영 |
| `functions/threshold_monitor_bp.py`, `system_initiated_steps_bp.py` | canonical import 변경, binding·진입점 유지 |
| `chatbot/nlu.py`, `timezone_onboarding.py`, operational/planning node | 새 prompt 위치와 기존 fallback 동작 |
| `.funcignore`, `.gitignore`, `local_test.py`, `tests/*` | nested proactive 포함, 새 검증 파일 추적, resource/legacy import 계약 |
| 현재 README·ARCHITECTURE·수정 가이드·한글 보고서 | 실제 경로와 이전 작업의 역사적 설명 구분 |
| 이전 작업에서 변경된 graph/state/schema/executor/D2 파일 | generic DAG와 Advisor 계측의 호환성; 이번 작업에서는 그 구조를 유지 |

모든 결과는 working tree에 남겨 사용자 검토가 가능하게 했다. Git 기록 변경과 배포는 수행하지 않았다.
