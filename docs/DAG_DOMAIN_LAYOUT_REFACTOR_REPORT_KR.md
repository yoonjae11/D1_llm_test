# Sensei DAG/domain 소유권 리팩토링 결과보고

작성일: 2026-09-08  
대상: `azure_function_main_app/` working tree  
기준: `Sensei_Domain_Ownership_Refactor_STRICT_Codex_Spec.md` 및 사용자의 후속 정리 요청

이 보고서는 현재 코드와 로컬 검증 결과를 설명하는 현행 문서다. 이전 1~3차
보고서는 당시 작업 상태를 보존하는 역사적 스냅샷으로 분리했다.

첨부 명세는 root 구형 module을 삭제하거나 thin shim으로 남기는 두 선택을
허용했지만, 사용자가 후속 요청에서 root 구형 파일이 보이는 상태를 명시적으로
문제로 지적했다. 따라서 이번 작업에서는 shim을 전부 삭제했다. 명세의 예시
`prompts/response/`보다 사용자의 “node와 prompt의 실제 기능 소유권을 맞춘다”는
요청을 적용하여, 최종 응답 prompt는 실제 node와 동일한
`prompts/common/generate_final_response/`에 배치했다.

## 1. 이어받은 작업 트리의 실제 상태

- D1 핵심 구현은 이미 `nodes/d1_sensor_data/`에 있었지만, `nodes/` root에
  과거 이름의 14개 module shim이 남아 있어 실제 소유권이 불분명해 보였다.
- proactive 구현은 `chatbot/proactive/`에 있었지만 chatbot root에도 2개 shim이
  남아 있었다.
- D1 라우팅 함수만 별도 `routing.py`, D2의 작은 상태형만 별도 `state.py`에
  놓여 있었다.
- planning과 D2의 prompt 정책이 여러 Python module로 분리되어 있었고,
  `.txt` prompt 이름도 실제 node 이름과 일치하지 않았다.
- `prompts/response/`, D3, D4 등에는 실행 resource 없이 README만 남은 facade가
  있었다.
- 테스트와 evaluation/local smoke가 구형 import·patch·resource 경로를 계속
  소비하고 있었다.

## 2. Domain ownership audit

| 소유자 | 현재 책임 | canonical 위치 |
|---|---|---|
| Common | 문맥 준비, 결과 집계, 최종 응답, trace/context 저장, node 공용 helper | `chatbot/nodes/common/` |
| Planning | intent와 D1~D4 실행 계획, greeting/thanks fast path, route mapping, 계획 검증 | `chatbot/nodes/planning/` |
| Orchestration | D1~D4 runner registry, dependency 전달, 최대 3-stage 실행 | `chatbot/nodes/orchestration/` |
| D1 | sensor plan, KQL 생성, guard, bounded repair, ADX 실행, 결과 wrapper | `chatbot/nodes/d1_sensor_data/` |
| D2 | coaching context, Advisor strategy, deterministic validation, 결과 wrapper | `chatbot/nodes/d2_coaching_strategy/` |
| D3 | health knowledge용 명시적 model-free placeholder | `chatbot/nodes/d3_health_knowledge/` |
| D4 | 추가 multi-turn context용 명시적 model-free placeholder | `chatbot/nodes/d4_multiturn_context/` |
| Operational | onboarding, capability, alert/daily-summary 설정 응답 | `chatbot/nodes/operational/` |
| Proactive | threshold monitor, daily report, System STEPS 알림 | `chatbot/proactive/` |
| Policies/services | 재사용 가능한 순수 정책과 Azure/provider 경계 | 기존 `chatbot/policies/`, `chatbot/services/`, `chatbot/adapters/` |

`services/`와 `policies/`는 domain node를 import하지 않는다. 저수준 재사용 코드를
억지로 D1~D4에 복제하지 않고 domain node가 아래 방향으로 호출하는 구조를 유지했다.

## 3. 물리 이동·이름 정리

| 이전 위치/구성 | 현재 위치/구성 |
|---|---|
| `nodes/plan_builder.py` 계열 D1 root shim 5개 | 삭제; 실제 6개 node가 `nodes/d1_sensor_data/`에만 존재 |
| root common/planning/operational/orchestration shim 9개 | 삭제; 각 canonical package에서 직접 import |
| `chatbot/threshold_monitor.py`, `chatbot/system_steps_alert.py` | 삭제; `chatbot/proactive/`만 사용 |
| D1 `routing.py` | 짧은 conditional 함수 3개를 D1 `workflow.py`에 병합 |
| D2 `state.py` | 작은 `D2State`를 D2 `workflow.py`에 병합 |
| planning `route_helpers.py` | fast intent와 route mapping을 `plan_domain_execution.py`에 병합 |
| D1 `build_sensor_data_plan.txt` | `d1_sensor_data/plan_sensor_query.txt` |
| D1 `generate_sensor_kql.txt` | `d1_sensor_data/generate_sensor_query.txt` |
| D1 `repair_sensor_kql.txt` | `d1_sensor_data/repair_sensor_query.txt` |
| `response/{chat_answer,sensor_data_answer,coaching_answer,unsupported_metric}.txt` | `common/generate_final_response/{chat,sensor_data,coaching,unsupported_metric}.txt` |
| planning policy Python + legacy intent prompt | `planning/plan_domain_execution.txt` 하나로 통합 |
| D2 policy/schema prompt Python 2개 | `d2_coaching_strategy/plan_coaching_strategy.txt` 하나로 통합 |
| `operational/alert_config/extract.txt` | `operational/alert_config_responder.txt` |
| `operational/capabilities/classify.txt` | `operational/capabilities_responder.txt` |
| proactive threshold/daily prompt 폴더 2개 | `proactive/threshold_monitor/` 아래 2개 파일 |

파일 이동은 복사본을 남기지 않는 물리 이동으로 처리했다. 기존 prompt의 task 내용,
placeholder, role 경계는 유지했고 planning/D2 정책은 기존 Python 상수를 `.txt`로
옮겼다.

## 4. 삭제한 구형 파일과 facade

삭제한 root node module:

```text
alert_config_responder.py  capabilities_responder.py  domain_orchestration.py
hydrate_context.py         intent_router.py            kql_guard.py
onboarding_responder.py    persist_context.py          plan_builder.py
query_executor.py          query_generator.py          query_repair.py
response_generator.py      support.py
```

추가 삭제:

```text
chatbot/threshold_monitor.py
chatbot/system_steps_alert.py
nodes/d1_sensor_data/routing.py
nodes/d2_coaching_strategy/state.py
nodes/planning/route_helpers.py
prompts/planning/domain_execution_planning_prompt.py
prompts/planning/legacy_intent_classification.txt
prompts/d2_coaching_strategy/advisor_coaching_policy.py
prompts/d2_coaching_strategy/coaching_strategy_planning_prompt.py
```

실행 resource가 없던 `prompts/response/`, `prompts/d3_health_knowledge/`,
`prompts/d4_multiturn_context/`와 이동 후 비어 있던 operational/proactive 하위
폴더도 삭제했다.

## 5. 남은 compatibility shim

이번 domain/node/proactive 이동과 관련해 남은 compatibility shim은 **0개**다.
`chatbot/nodes/` root의 Python 파일은 package marker인 `__init__.py` 하나뿐이다.
canonical 파일 내부에 있던 `plan_builder`, `query_generator`, `kql_guard`,
`query_repair`, `query_executor`, `persist_context`, `hydrate_context`,
`response_generator` callable alias도 제거했다.

기존 런타임 데이터 계약인 `AgentState.kql_guard`, `guard_status`, plan/KQL/error/trace
필드는 이름을 바꾸지 않았다. Python 소유권 정리와 대화 상태 호환성을 구분했다.

## 6. 최종 nodes tree

```text
nodes/
|-- common/
|   |-- prepare_context.py
|   |-- aggregate_domain_results.py
|   |-- generate_final_response.py
|   |-- persist_trace_and_context.py
|   `-- support.py
|-- planning/
|   |-- plan_domain_execution.py
|   `-- validate_domain_execution_plan.py
|-- orchestration/
|   `-- execute_domains.py
|-- d1_sensor_data/
|   |-- workflow.py
|   |-- plan_sensor_query.py
|   |-- generate_sensor_query.py
|   |-- guard_sensor_query.py
|   |-- repair_sensor_query.py
|   |-- execute_sensor_query.py
|   `-- produce_sensor_result.py
|-- d2_coaching_strategy/
|   |-- workflow.py
|   |-- prepare_coaching_context.py
|   |-- plan_coaching_strategy.py
|   |-- validate_coaching_plan.py
|   `-- produce_coaching_result.py
|-- d3_health_knowledge/
|   |-- workflow.py
|   `-- placeholder_health_knowledge.py
|-- d4_multiturn_context/
|   |-- workflow.py
|   `-- placeholder_multiturn_context.py
|-- operational/
|   |-- onboarding_responder.py
|   |-- capabilities_responder.py
|   `-- alert_config_responder.py
|-- README.md
`-- __init__.py
```

각 실제 graph node 이름과 Python 파일명이 일치한다. `workflow.py`에는 wiring,
runner와 짧은 transition만 두고 큰 D1 helper를 불필요하게 여러 파일로 쪼개지 않았다.

## 7. 최종 prompts tree

```text
prompts/
|-- common/generate_final_response/
|   |-- chat.txt
|   |-- coaching.txt
|   |-- sensor_data.txt
|   `-- unsupported_metric.txt
|-- planning/plan_domain_execution.txt
|-- d1_sensor_data/
|   |-- plan_sensor_query.txt
|   |-- generate_sensor_query.txt
|   |-- repair_sensor_query.txt
|   `-- adx_schema_context.txt
|-- d2_coaching_strategy/plan_coaching_strategy.txt
|-- operational/
|   |-- alert_config_responder.txt
|   |-- capabilities_responder.txt
|   `-- guided_input/ (8개)
|-- proactive/threshold_monitor/
|   |-- rewrite_threshold_alert.txt
|   `-- daily_report_coach.txt
|-- loader.py
|-- README.md
`-- __init__.py
```

총 `.txt` resource는 **22개**, `[SYSTEM]`/`[USER]` role resource는 **9개**다.
D3/D4와 System STEPS는 model call이 없으므로 fake prompt를 만들지 않았다.

## 8. D1 실행 흐름

```text
plan_sensor_query
  -> generate_sensor_query
  -> guard_sensor_query
       -> execute_sensor_query
       -> repair_sensor_query -> guard_sensor_query
       -> produce_sensor_result (차단/실패)
  -> produce_sensor_result
```

invalid typed plan은 query/ADX 전에 종료된다. deterministic KQL이 기본이며 설정된
경우에만 LLM candidate를 사용한다. unsafe KQL은 ADX로 가지 않는다. repair 횟수는
제한되고 repair 결과는 반드시 다시 guard를 통과한다. 기존 user scope, time bound,
result bound, error/no-data 의미를 유지한다.

## 9. D2 실행 흐름

```text
prepare_coaching_context
  -> plan_coaching_strategy
  -> validate_coaching_plan
  -> produce_coaching_result
```

D2는 최대 한 번의 strategy model call만 수행하고 final 사용자 문장을 쓰지 않는다.
COM-B, BCT interface, MI/SDT 제약과 interaction depth를 typed strategy로 반환한다.
검증은 deterministic이며, 전달 선언이 없는 D1 데이터나 과거 KQL을 임의 근거로
읽지 않는다. 최종 자연어는 common `generate_final_response`가 작성한다.

## 10. D3/D4 placeholder interface

두 domain 모두 `build_dN_graph()`와 `run_dN(state)`를 제공하고
`DomainRunOutput`을 반환한다. 현재 결과는 정직한 `status=not_implemented`이며,
빈 `structured_result`, 무 model/service call, 무 fabricated evidence를 보장한다.
향후 실제 구현은 각 placeholder node 내부를 교체하면서 runner interface를 유지할 수 있다.

## 11. Generic orchestration

최상위 graph는 다음 6개 stage만 소유한다.

```text
prepare_context -> plan_domain_execution -> execute_domains
-> aggregate_domain_results -> generate_final_response
-> persist_trace_and_context
```

`DOMAIN_RUNNERS`에는 D1~D4가 모두 등록되어 있다. 계획은
`selected_domains`, `domain_requirements`, `dependencies`, `execution_stages`,
`early_exit_conditions`를 사용한다. cycle, self-dependency, unselected reference,
잘못된 stage 순서, 중복 실행, 3단계 초과는 실행 전에 fail closed 된다. 동일 stage의
독립 domain은 격리된 state snapshot으로 실행되고 필수 dependency 실패는 downstream을
차단하며 optional 실패는 실제 status 그대로 전달한다.

## 12. Prompt loader와 호출부 변경

중앙 loader의 UTF-8, 상대경로 제한, traversal 방지, cache, role 분리 동작은 유지했다.
모든 model-calling node가 새 canonical `.txt` 경로를 직접 load하도록 바꿨다.
planning과 D2의 typed schema/runtime context 조립은 각 owning node Python에 두고,
고정 task 정책만 `.txt`에 둔다. 전체 22개 resource의 존재, 비어 있지 않음,
template 변수 계약, role marker를 local smoke와 구조 테스트가 검증한다.

## 13. Backward compatibility 영향

다음 외부 동작 계약은 유지했다.

- `chatbot.entrypoint.handle_user_message`와 `chatbot.graph.build_graph`
- `build_initial_state`, `run_sensor_analytics_graph`
- Azure Function blueprint 이름·route·trigger와 proactive 함수 signature
- `AgentState`, `ChatbotResponse`, thread/episode/response-chain 의미
- 일반 대화, `/help`, `/login`, `/register_user`, `/start`, onboarding, `/cancel`
- sensor 질문, mocked ADX 실행, unsafe KQL 차단, missing-data 응답
- alert/daily-summary 설정과 proactive threshold/STEPS 동작

저장소 내부 production, tests, evaluation, local tooling은 모두 canonical import로
전환했다. 저장소 밖에서 삭제된 private root module을 직접 import하던 비공식 소비자가
있다면 경로를 갱신해야 한다. 확인된 package/public API 계약에는 해당 root import가 없다.

## 14. 로컬 검증 결과

| 검증 | 결과 |
|---|---|
| `python -m unittest discover -s tests -v` | **78 PASSED, 0 FAILED** |
| `python -m unittest discover -s evaluation -p 'test_*.py' -v` | **26 PASSED, 0 FAILED** |
| `python local_test.py` | **status=ok**, Azure Functions 25, top graph nodes 6, prompts 22, role prompts 9 |
| 전체 app/evaluation Python AST parse | **139 files OK** |
| 구형 Python module import/path 검색 | **0 hits** |
| 구형 runtime prompt resource 경로 검색 | **0 hits** |
| `nodes/` root Python 검사 | **`__init__.py` only** |

테스트는 network를 차단하고 LLM, ADX, Telegram, storage 경계를 mock했다. 로컬 환경에
ADX 설정이 없다는 초기 log와 Python 3.14에서의 dependency warning은 예상된 진단이며
테스트 실패가 아니다. 사용자 요청에 따라 live 서비스 호출과 배포 검증은 수행하지 않았다.

## 15. 남은 TODO

- D3의 실제 health knowledge/RAG/safety 구현은 별도 제품 작업이다.
- D4의 추가 memory/context retrieval 구현은 별도 제품 작업이다. 기존 common 대화
  문맥 복원 기능은 정상 동작한다.
- 출처가 채워진 BCT catalogue가 제공되면 현재 loader/validator interface에 연결할 수 있다.
- 실제 model 응답 품질과 Azure 통합은 보호된 비운영 환경에서 별도로 확인해야 한다.

이 TODO 때문에 현재 기능을 가짜 성공으로 표시하지 않는다. D3/D4는 명시적 placeholder,
빈 BCT catalogue는 빈 selection으로 처리한다.

## 16. main → red_teaming 향후 충돌 예상 파일

사용자가 Git operation을 금지했으므로 branch 전환·비교·merge를 수행하지 않았다.
따라서 아래는 실제 branch diff가 아니라 이번 변경 범위에 따른 예상 목록이다.

```text
chatbot/graph/workflow.py
chatbot/nodes/**
chatbot/prompts/**
chatbot/proactive/**
functions/threshold_monitor_bp.py
functions/system_initiated_steps_bp.py
local_test.py
tests/test_domain_orchestration.py
tests/test_domain_architecture.py
tests/test_strict_ownership.py
evaluation/runner.py
evaluation/test_offline.py
ARCHITECTURE.md 및 package README
```

향후 통합 시에는 root shim이나 old prompt path를 되살리지 말고 이 보고서의 canonical
tree를 기준으로 red-teaming의 실제 보안·행동 변경만 각 owning node/prompt에 적용해야
한다. 특히 D1 guard→repair→guard 경로, top 6-stage graph, D2 final-answer 분리,
proactive의 graph 외부 소유권을 우선 보존해야 한다.

이번 작업에서는 commit, push, merge, rebase, checkout, branch switch, reset, restore,
clean, deploy를 수행하지 않았다.

