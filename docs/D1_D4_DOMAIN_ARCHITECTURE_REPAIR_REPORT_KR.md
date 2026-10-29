# Sensei D1–D4 구조 보정 및 D2 Advisor 결과보고

> 2차 보정 당시의 역사적 스냅샷입니다. 본문의 경로와 테스트 수치는 당시 상태이며,
> 유보로 표시된 prompt 이동도 후속 작업에서 완료했습니다. 현재 파일 위치와 검증 결과는
> [현행 DAG/domain 보고서](DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md)를 확인해 주세요.

작성일: 2026-09-08. 기준 명세: `Sensei_D1_D4_Domain_Architecture_Repair_D2_Advisor_Codex_Spec_FINAL.md`.
현재 저장소의 `azure_function_main_app/`에서 기존 1차 변경사항을 이어서 보정했다.
사용자의 요청에 따라 working tree 수정과 로컬 검증만 수행했다. 커밋·푸시·병합·rebase·브랜치 전환·checkout·reset·restore·clean·배포 및 실제 외부 서비스 호출을 수행하지 않았다.

## 1. 현재 코드 분석 결과

작업 시작 시 D1 workflow는 존재했지만 실제 계획·쿼리 생성·검증·수리·실행은 루트의 `plan_builder.py`, `query_generator.py`, `kql_guard.py`, `query_repair.py`, `query_executor.py`를 호출했다. `produce_sensor_result()`도 workflow 내부에 섞여 있었다.

D2는 준비·전략·검증 코드가 있었으나 실제 네 단계 LangGraph로 구성되지 않았다. D3·D4는 독립 패키지와 runner 없이 executor 내부의 포괄적인 placeholder 처리에 의존했다. 계획은 `d2_requires_d1`와 `execution_waves` 중심이어서 D4 → D1, [D1 ∥ D3] → D2 같은 관계를 표현하기 어려웠다. 공통 응답·문맥·저장 및 operational 구현도 루트에 남아 있었다.

보정 후 실제 구현 소유권을 패키지별로 옮겼고, 상위 그래프는 6개 노드를 유지했다. 기존 policy/service를 재사용하며 D1–D4 모두 같은 registry/runner 계약으로 실행한다.

## 2. 이동/추가/수정 파일

아래 목록의 기준 경로는 `azure_function_main_app/`이다. Git HEAD가 아니라 **이번 보정 시작 시점의 working tree 파일 해시**와 비교했다. 따라서 1차 작업에서 이미 추가했던 파일도 이번에 내용이 바뀌었으면 수정으로 표시한다.

### Physically moved — 실제 구현 이동

| 기존 `chatbot/nodes/` 구현 | 현재 단일 구현 위치 |
|---|---|
| `plan_builder.py` | `d1_sensor_data/plan_sensor_query.py` |
| `query_generator.py` | `d1_sensor_data/generate_sensor_query.py` |
| `kql_guard.py` | `d1_sensor_data/guard_sensor_query.py` |
| `query_repair.py` | `d1_sensor_data/repair_sensor_query.py` |
| `query_executor.py` | `d1_sensor_data/execute_sensor_query.py` |
| `hydrate_context.py` | `common/prepare_context.py` |
| `persist_context.py` | `common/persist_trace_and_context.py` |
| `response_generator.py` | `common/generate_final_response.py` |
| `support.py` | `common/support.py` |
| `intent_router.py` | `planning/route_helpers.py` |
| `alert_config_responder.py` | `operational/alert_config_responder.py` |
| `capabilities_responder.py` | `operational/capabilities_responder.py` |
| `onboarding_responder.py` | `operational/onboarding_responder.py` |

위 13개 파일의 구현을 물리적으로 이전하고 기존 경로에는 shim만 남겼다. 추가로 `domain_orchestration.py`의 실행·집계 책임을 `orchestration/execute_domains.py`와 `common/aggregate_domain_results.py`로 분리했다. D1 결과 변환 및 routing helper도 각각 `produce_sensor_result.py`, `routing.py`로 분리했다.

### Newly added — 신규 파일

구현 이동 대상도 파일 시스템상 신규 파일이므로 포함한다.

- [`chatbot/nodes/common/__init__.py`](../chatbot/nodes/common/__init__.py)
- [`chatbot/nodes/common/aggregate_domain_results.py`](../chatbot/nodes/common/aggregate_domain_results.py)
- [`chatbot/nodes/common/generate_final_response.py`](../chatbot/nodes/common/generate_final_response.py)
- [`chatbot/nodes/common/persist_trace_and_context.py`](../chatbot/nodes/common/persist_trace_and_context.py)
- [`chatbot/nodes/common/prepare_context.py`](../chatbot/nodes/common/prepare_context.py)
- [`chatbot/nodes/common/support.py`](../chatbot/nodes/common/support.py)
- [`chatbot/nodes/d1_sensor_data/__init__.py`](../chatbot/nodes/d1_sensor_data/__init__.py)
- [`chatbot/nodes/d1_sensor_data/execute_sensor_query.py`](../chatbot/nodes/d1_sensor_data/execute_sensor_query.py)
- [`chatbot/nodes/d1_sensor_data/generate_sensor_query.py`](../chatbot/nodes/d1_sensor_data/generate_sensor_query.py)
- [`chatbot/nodes/d1_sensor_data/guard_sensor_query.py`](../chatbot/nodes/d1_sensor_data/guard_sensor_query.py)
- [`chatbot/nodes/d1_sensor_data/plan_sensor_query.py`](../chatbot/nodes/d1_sensor_data/plan_sensor_query.py)
- [`chatbot/nodes/d1_sensor_data/produce_sensor_result.py`](../chatbot/nodes/d1_sensor_data/produce_sensor_result.py)
- [`chatbot/nodes/d1_sensor_data/repair_sensor_query.py`](../chatbot/nodes/d1_sensor_data/repair_sensor_query.py)
- [`chatbot/nodes/d1_sensor_data/routing.py`](../chatbot/nodes/d1_sensor_data/routing.py)
- [`chatbot/nodes/d2_coaching_strategy/__init__.py`](../chatbot/nodes/d2_coaching_strategy/__init__.py)
- [`chatbot/nodes/d2_coaching_strategy/produce_coaching_result.py`](../chatbot/nodes/d2_coaching_strategy/produce_coaching_result.py)
- [`chatbot/nodes/d2_coaching_strategy/state.py`](../chatbot/nodes/d2_coaching_strategy/state.py)
- [`chatbot/nodes/d2_coaching_strategy/workflow.py`](../chatbot/nodes/d2_coaching_strategy/workflow.py)
- [`chatbot/nodes/d3_health_knowledge/__init__.py`](../chatbot/nodes/d3_health_knowledge/__init__.py)
- [`chatbot/nodes/d3_health_knowledge/placeholder_health_knowledge.py`](../chatbot/nodes/d3_health_knowledge/placeholder_health_knowledge.py)
- [`chatbot/nodes/d3_health_knowledge/workflow.py`](../chatbot/nodes/d3_health_knowledge/workflow.py)
- [`chatbot/nodes/d4_multiturn_context/__init__.py`](../chatbot/nodes/d4_multiturn_context/__init__.py)
- [`chatbot/nodes/d4_multiturn_context/placeholder_multiturn_context.py`](../chatbot/nodes/d4_multiturn_context/placeholder_multiturn_context.py)
- [`chatbot/nodes/d4_multiturn_context/workflow.py`](../chatbot/nodes/d4_multiturn_context/workflow.py)
- [`chatbot/nodes/operational/__init__.py`](../chatbot/nodes/operational/__init__.py)
- [`chatbot/nodes/operational/alert_config_responder.py`](../chatbot/nodes/operational/alert_config_responder.py)
- [`chatbot/nodes/operational/capabilities_responder.py`](../chatbot/nodes/operational/capabilities_responder.py)
- [`chatbot/nodes/operational/onboarding_responder.py`](../chatbot/nodes/operational/onboarding_responder.py)
- [`chatbot/nodes/orchestration/__init__.py`](../chatbot/nodes/orchestration/__init__.py)
- [`chatbot/nodes/orchestration/execute_domains.py`](../chatbot/nodes/orchestration/execute_domains.py)
- [`chatbot/nodes/planning/__init__.py`](../chatbot/nodes/planning/__init__.py)
- [`chatbot/nodes/planning/route_helpers.py`](../chatbot/nodes/planning/route_helpers.py)
- [`chatbot/nodes/planning/validate_domain_execution_plan.py`](../chatbot/nodes/planning/validate_domain_execution_plan.py)
- [`chatbot/prompts/d1_sensor_data/README.md`](../chatbot/prompts/d1_sensor_data/README.md)
- [`chatbot/prompts/d3_health_knowledge/README.md`](../chatbot/prompts/d3_health_knowledge/README.md)
- [`chatbot/prompts/d4_multiturn_context/README.md`](../chatbot/prompts/d4_multiturn_context/README.md)
- [`chatbot/prompts/response/README.md`](../chatbot/prompts/response/README.md)
- [`docs/D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md`](../docs/D1_D4_DOMAIN_ARCHITECTURE_REPAIR_REPORT_KR.md)
- [`tests/test_domain_architecture.py`](../tests/test_domain_architecture.py)

### Modified — 기존 파일 수정

shim으로 전환한 파일은 아래 별도 목록으로 분리했다.

- [`.gitignore`](../.gitignore)
- [`ARCHITECTURE.md`](../ARCHITECTURE.md)
- [`README.md`](../README.md)
- [`chatbot/README.md`](../chatbot/README.md)
- [`chatbot/graph/README.md`](../chatbot/graph/README.md)
- [`chatbot/graph/state.py`](../chatbot/graph/state.py)
- [`chatbot/graph/workflow.py`](../chatbot/graph/workflow.py)
- [`chatbot/nodes/README.md`](../chatbot/nodes/README.md)
- [`chatbot/nodes/d1_sensor_data/workflow.py`](../chatbot/nodes/d1_sensor_data/workflow.py)
- [`chatbot/nodes/d2_coaching_strategy/plan_coaching_strategy.py`](../chatbot/nodes/d2_coaching_strategy/plan_coaching_strategy.py)
- [`chatbot/nodes/d2_coaching_strategy/prepare_coaching_context.py`](../chatbot/nodes/d2_coaching_strategy/prepare_coaching_context.py)
- [`chatbot/nodes/d2_coaching_strategy/validate_coaching_plan.py`](../chatbot/nodes/d2_coaching_strategy/validate_coaching_plan.py)
- [`chatbot/nodes/planning/plan_domain_execution.py`](../chatbot/nodes/planning/plan_domain_execution.py)
- [`chatbot/prompts/README.md`](../chatbot/prompts/README.md)
- [`chatbot/prompts/planning/domain_execution_planning_prompt.py`](../chatbot/prompts/planning/domain_execution_planning_prompt.py)
- [`chatbot/schemas/README.md`](../chatbot/schemas/README.md)
- [`chatbot/schemas/domain_execution_plan.py`](../chatbot/schemas/domain_execution_plan.py)
- [`chatbot/schemas/domain_execution_result.py`](../chatbot/schemas/domain_execution_result.py)
- [`docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md`](../docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md)
- [`local_test.py`](../local_test.py)
- [`tests/test_domain_orchestration.py`](../tests/test_domain_orchestration.py)

### Compatibility shim retained — 기존 경로 유지

14개 모두 **LEGACY COMPATIBILITY SHIM**이다. 함수·클래스 business logic을 정의하지 않으며, `sys.modules`에서 새 canonical module과 같은 객체를 가리킨다. 이 방식으로 기존 callable뿐 아니라 평가 도구의 helper import와 `mock.patch` 대상도 보존했다.

- [`chatbot/nodes/alert_config_responder.py`](../chatbot/nodes/alert_config_responder.py)
- [`chatbot/nodes/capabilities_responder.py`](../chatbot/nodes/capabilities_responder.py)
- [`chatbot/nodes/domain_orchestration.py`](../chatbot/nodes/domain_orchestration.py)
- [`chatbot/nodes/hydrate_context.py`](../chatbot/nodes/hydrate_context.py)
- [`chatbot/nodes/intent_router.py`](../chatbot/nodes/intent_router.py)
- [`chatbot/nodes/kql_guard.py`](../chatbot/nodes/kql_guard.py)
- [`chatbot/nodes/onboarding_responder.py`](../chatbot/nodes/onboarding_responder.py)
- [`chatbot/nodes/persist_context.py`](../chatbot/nodes/persist_context.py)
- [`chatbot/nodes/plan_builder.py`](../chatbot/nodes/plan_builder.py)
- [`chatbot/nodes/query_executor.py`](../chatbot/nodes/query_executor.py)
- [`chatbot/nodes/query_generator.py`](../chatbot/nodes/query_generator.py)
- [`chatbot/nodes/query_repair.py`](../chatbot/nodes/query_repair.py)
- [`chatbot/nodes/response_generator.py`](../chatbot/nodes/response_generator.py)
- [`chatbot/nodes/support.py`](../chatbot/nodes/support.py)

기존 1차 변경의 `requirements.txt`, `services/sensor_analytics.py`, D2 schema·정책 prompt·BCT loader 및 응답 prompt 변경사항도 그대로 유지했다. 이번 작업에서 Git 상태를 되돌리지 않았다.

## 3. 최종 `chatbot/nodes/` tree

실제 파일 시스템에서 생성한 트리다. `__pycache__`는 제외했다. 루트의 14개 shim 외에는 패키지 초기화 파일과 README만 남는다.

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

prompt 경로도 실제로 `planning/`, `d1_sensor_data/`, `d2_coaching_strategy/`, `d3_health_knowledge/`, `d4_multiturn_context/`, `response/`, `shared/`가 존재한다. D3·D4 및 이관 유보 폴더는 README로 소유권을 명시하며 빈 prompt 호출이나 텍스트 복제를 추가하지 않았다.

## 4. D1 subgraph

실제 파일은 `nodes/d1_sensor_data/`의 `workflow.py`, `plan_sensor_query.py`, `generate_sensor_query.py`, `guard_sensor_query.py`, `repair_sensor_query.py`, `execute_sensor_query.py`, `produce_sensor_result.py`, `routing.py`다.

```text
START -> plan_sensor_query
         | valid -> generate_sensor_query
         |           | generated -> guard_sensor_query
         |           |               | execute -> execute_sensor_query
         |           |               | repair -> repair_sensor_query
         |           |               |              -> guard_sensor_query
         |           |               | blocked -> produce_sensor_result
         |           | empty/error -> produce_sensor_result
         | invalid -> produce_sensor_result
execute_sensor_query -> produce_sensor_result -> END
```

`workflow.py`는 D1 노드를 같은 패키지에서 import한다. typed plan 정규화, metric/time range, deterministic/LLM KQL 생성, guard, 제한된 repair와 재검증, ADX 실행·결과 파싱, coverage 및 0/null 구분은 기존 구현을 이전해 재사용했다. 안정적으로 사용 중인 `policies/`와 `services/`는 이동하지 않았다.

`build_d1_graph()`는 실제 LangGraph를 compile한다. `run_d1(state)`는 공통 결과와 D1 소유 state 필드만 반환한다. timezone onboarding이 필요하면 쿼리 그래프를 실행하지 않는다. 기존 `plan_builder`, `query_generator`, `kql_guard`, `query_repair`, `query_executor` callable은 같은 구현의 alias로 유지했다.

## 5. D2 subgraph

```text
START -> prepare_coaching_context -> plan_coaching_strategy
      -> validate_coaching_plan -> produce_coaching_result -> END
```

위 네 노드가 실제 `nodes/d2_coaching_strategy/workflow.py`에 등록된다. 각 책임은 같은 이름의 파일에 있고, 내부 상태는 `state.py`에 정의했다. 검증 노드의 Python adapter 함수명은 `validate_coaching_strategy`이며 기존 순수 검증 함수 `validate_coaching_plan`을 호출한다.

- 준비: 현재 질문·허용된 대화 문맥·명시적으로 전달된 dependency 결과만 사용한다. 과거 KQL이나 전달되지 않은 센서 결과를 임의 근거로 읽지 않는다.
- 계획: `prompts/d2_coaching_strategy/coaching_strategy_planning_prompt.py`와 `advisor_coaching_policy.py`를 사용해 구조화 전략을 위한 모델 호출을 최대 1회 수행한다.
- 검증: `D2CoachingStrategyPlan`과 deterministic policy로 applicability, objective, COM-B evidence/confidence, BCT ID, MI delivery, SDT, interaction depth 및 response constraints를 보정한다. 오류 시 짧은 탐색적 fallback과 error 상태를 남긴다.
- 결과: `produce_coaching_result.py`가 공통 wrapper를 만들고 모델 호출 수·검증 보정·catalogue 및 dependency 상태를 provenance에 기록한다. 최종 사용자 문장은 생성하지 않는다.

Advisor prompt에는 일반적인 personalised care, 자율적 선택, 협력적 계획, COM-B, MI/SDT 및 비례적인 대화 깊이 정책을 사용한다. Auditor 실행, MITI/TENS/CEISS 점수, gold label, 정답 BCT 목록이나 평가 시나리오 답안을 runtime에 추가하지 않았다. 실제 모델의 코칭 품질 평가는 수행하지 않았다.

## 6. D3/D4 placeholder

| 도메인 | 실제 패키지 | 공개 interface | 현재 결과 |
|---|---|---|---|
| D3 | `nodes/d3_health_knowledge/` | `build_d3_graph()`, `run_d3(state)` | `not_implemented` |
| D4 | `nodes/d4_multiturn_context/` | `build_d4_graph()`, `run_d4(state)` | `not_implemented` |

각각 `__init__.py`, `workflow.py`, `placeholder_health_knowledge.py` 또는 `placeholder_multiturn_context.py`가 존재한다. 한 개의 명시적 node로 compile된 실제 LangGraph이며 모델·검색·저장소 호출은 없다. 구조화 결과는 빈 객체, provenance는 placeholder로 표시한다.

Planner는 요청에 필요하면 D3·D4를 선택할 수 있다. 일반적인 기존 hydration과 별도로 추가 지식/문맥이 필요한 상황을 표현한다. 향후 domain 구현을 교체할 때 상위 registry와 DAG schema를 재작성할 필요가 없다. required placeholder에 의존하는 후속 runner는 호출하지 않으며 `blocked_dependency`를 반환한다.

## 7. Generic orchestration schema

`DomainExecutionPlan`의 도메인은 `D1 | D2 | D3 | D4`이다.

| 필드 | 의미 |
|---|---|
| `intent` | 기존 data/chat/operational/clarification 라우팅 계약 |
| `selected_domains` | 실행할 도메인 |
| `domain_requirements` | 도메인별 `need`와 `optional_dependencies` |
| `dependencies` | 대상 도메인이 참조하는 선행 도메인 목록 |
| `execution_stages` | 순차 단계 목록; 같은 단계 내 독립 도메인은 병렬 |
| `early_exit_conditions` | `ON_REQUIRED_DEPENDENCY_FAILURE`, `ON_DOMAIN_ERROR` |
| `rationale` | 선택·의존성 근거 |
| `concrete_plan_requested` | 사용자가 구체적 행동계획을 요청했는지 나타내는 의미 필드 |

`d2_requires_d1`, `domains`, `execution_waves`에 의존하지 않는다. 예를 들어 `[D1 ∥ D3] → D2`는 다음과 같이 표현한다.

```json
{
  "intent": "data",
  "selected_domains": ["D1", "D3", "D2"],
  "domain_requirements": {
    "D1": {"need": "personal sensor evidence"},
    "D3": {"need": "supporting knowledge"},
    "D2": {"need": "evidence-based coaching strategy"}
  },
  "dependencies": {"D1": [], "D3": [], "D2": ["D1", "D3"]},
  "execution_stages": [["D1", "D3"], ["D2"]],
  "early_exit_conditions": [],
  "rationale": "Both evidence sources are required before strategy planning"
}
```

Validator는 중복 selected/dependency를 정규화하고, 미지원 도메인·미선택 참조·자기 참조·순환·단계 순서 오류·중복 실행·빈 단계·누락된 실행·3단계 초과·미선언 optional dependency를 거부한다. 단계가 생략되면 DAG에서 도출한다. 빈 선택은 정상적인 도메인 미실행으로 처리하며 malformed plan은 clarification 및 전체 미실행으로 전환한다. 기본 목표는 1–2단계이고 최대 3단계다.

`DOMAIN_RUNNERS`는 네 `run_d*` 함수에 동일한 `DomainRunOutput(result, state_updates)` 계약을 적용한다. `result`는 `DomainExecutionResult`이고 상태는 `success`, `skipped`, `not_implemented`, `insufficient_information`, `blocked_dependency`, `error`이다.

모든 선언된 dependency는 기본적으로 required다. required 결과가 success가 아니면 후속 호출 없이 차단한다. optional로 명시한 dependency는 실제 실패 상태·missing information을 전달하고 진행할 수 있다. dependency별 상태와 runner 호출 여부는 trace/provenance에 기록한다. 같은 단계의 state 복사본을 격리하고 contextvars를 전달하며, 공통 메시지/계획 덮어쓰기나 동시 state 충돌은 오류로 처리한다. 조기 종료 시 남은 선택 도메인은 이유와 함께 skipped로 표시한다.

최종 응답은 공통 노드에서 성공 근거와 실제 상태를 사용한다. 필수 정보 미확보와 미구현 기능은 완료한 것처럼 표현하지 않으며, 기존 센서 오류·데이터 없음 설명이 후속 D2 차단 때문에 사라지지 않도록 했다.

## 8. Backward compatibility

| 기존 동작 | 이번 변경의 영향과 확인 범위 |
|---|---|
| Telegram 일반 대화·reply | 기존 transport entrypoint 유지, mocked 대화 테스트 |
| `/help`, `/login`, `/register_user`, `/start`, `/cancel` | 기존 handler와 flow 유지, mocked 회귀 테스트 |
| Onboarding·timezone | 기존 hydration/operational 구현 이전, onboarding 시 센서 쿼리 차단 및 기존 응답 유지 |
| 센서 조회·ADX/KQL | D1 내부로 구현 이동, provider/policy 재사용, guarded mocked ADX 실행 |
| 데이터 없음·오류·0/null | 기존 설명과 의미 유지, D2 required 차단과 함께 발생하는 경우도 검증 |
| thread·context·응답 chain | 기존 user/thread 계약 유지, 다음 turn에서 이전 query/domain 산출물 초기화 |
| alert·daily-summary 설정 | operational 소유권으로 이동, 기존 설정 경로 mocked 검증 |
| evaluation/local tooling | root module identity shim과 callable alias 유지, 기존 평가 테스트 수정 없이 실행 |
| 최종 응답·저장 | common 소유권으로 이동, 기존 저장 API에 최신 계획·stage·status·dependency trace 기록 |

저장소 전체 Python 파일에 대해 이동 전후 `rg` import audit을 수행했다. 보정 전 검색 기록과 파일 해시는 로컬 `test-results/domain-repair-baseline.json`, 보정 후 결과는 `test-results/domain-repair-imports-after.txt`에 남겼다. 해당 파일은 로컬 작업 기록이며 배포 파일이 아니다.

보정 후 production node 구현의 루트 legacy import는 없다. `evaluation/runner.py`, `evaluation/test_offline.py`, `local_test.py` 및 회귀 테스트에는 기존 import를 의도적으로 유지했다. 특히 `_plan_node_result`, `generate_kql_candidate`, `_build_retry_candidates`, `_coverage_context`, `_fast_intent`와 기존 mock patch 경로를 보존했다. AST·module identity 검사로 루트 shim에 중복 business logic이 없음을 확인했다.

기존 transport schema, Function binding, 환경변수명 및 ADX/persistence API는 변경하지 않았다. 1차 중간 구현의 내부 실행계획 JSON은 새 schema로 교체했으며 구형 `d2_requires_d1` 입력 adapter는 두지 않았다. 저장된 과거 trace를 실행계획으로 재사용하지 않으므로 다음 요청에서 새 계획을 생성한다.

## 9. 테스트

Python 3.12의 로컬 `.venv`에서 실행했다. 모델·ADX·Telegram·Table Storage 경계는 mock을 사용했다.

| 검증 | 결과 | 실제 범위 |
|---|---|---|
| App 구조·회귀 unittest | PASSED | 67개: 새 구조/범용 의존성 24개 + 기존 조정 회귀 43개 |
| `evaluation.test_offline`, `evaluation.test_diagnostics` | PASSED | 26개, 기존 import와 평가 경로 포함 |
| `local_test.py smoke` | PASSED | Function binding 25개, 상위 graph node 6개, prompt 21개 및 기존 plan/KQL smoke |
| Python compileall | PASSED | Function App 및 evaluation 소스 |
| 변경 diff 공백 검사 | PASSED | `git diff --check` 및 신규 텍스트 파일 검사 |
| 실제 Azure/Telegram/ADX/Table Storage/CC 통합 | NOT EXECUTED | 요청에 따라 수행하지 않음 |
| 실제 모델의 domain 선택·Advisor 품질 평가 | NOT EXECUTED | mock 출력으로 구조와 정책만 검증 |
| red_teaming 병합·배포 | NOT EXECUTED | 요청에 따라 수행하지 않음 |

실행 명령은 저장소 루트 기준이다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s azure_function_main_app/tests -v
.\.venv\Scripts\python.exe -m unittest evaluation.test_offline evaluation.test_diagnostics
.\.venv\Scripts\python.exe azure_function_main_app/local_test.py smoke
.\.venv\Scripts\python.exe -m compileall -q azure_function_main_app evaluation
git diff --check
```

최종 unittest 결과는 총 **93개 PASSED, 0개 FAILED**다. 새 회귀 테스트를 추가하는 중 기대 문구가 실제 기존 문구와 달라 1개 테스트의 2개 subtest가 FAILED였으나, 기대값을 기존 catalogue 문구에 맞춘 후 67개 전체 재실행에서 PASSED를 확인했다. 실패 주입 테스트에서 출력되는 `RuntimeError`/`ValidationError` 로그는 mock 오류 경로 검증 결과다.

smoke는 ADX 환경변수 미설정 로그를 출력하지만 실제 호출 없이 `status: ok`, 종료 코드 0으로 완료했다. 이 결과를 실서비스 연결 검증으로 해석하지 않는다.

전체 unittest 목록과 최종 결과:

| Test | Result |
|---|---|
| `test_domain_architecture.OwnershipTests.test_old_modules_are_thin_identity_shims_with_one_source_of_truth` | PASSED |
| `test_domain_architecture.OwnershipTests.test_d1_business_functions_are_defined_in_the_domain` | PASSED |
| `test_domain_architecture.OwnershipTests.test_d1_workflow_uses_local_nodes_and_keeps_guard_repair_edges` | PASSED |
| `test_domain_architecture.OwnershipTests.test_all_domain_packages_and_prompt_ownership_directories_exist` | PASSED |
| `test_domain_architecture.OwnershipTests.test_top_graph_has_only_the_six_common_stages` | PASSED |
| `test_domain_architecture.OwnershipTests.test_d2_graph_runs_four_distinct_stages_and_one_model_call` | PASSED |
| `test_domain_architecture.OwnershipTests.test_d3_and_d4_are_explicit_model_free_subgraphs` | PASSED |
| `test_domain_architecture.OwnershipTests.test_legacy_patch_seam_reaches_canonical_module_globals` | PASSED |
| `test_domain_architecture.GenericPlanTests.test_supported_generic_schedules` | PASSED |
| `test_domain_architecture.GenericPlanTests.test_selected_duplicates_and_empty_plan_are_normalized` | PASSED |
| `test_domain_architecture.GenericPlanTests.test_invalid_dags_and_stages_fail_closed_without_runner_calls` | PASSED |
| `test_domain_architecture.GenericPlanTests.test_derived_four_stage_chain_is_rejected` | PASSED |
| `test_domain_architecture.GenericPlanTests.test_planner_can_select_both_placeholders` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_required_placeholder_blocks_following_d1_without_querying` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_d1_and_d3_parallel_then_required_d2_is_blocked` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_replacing_a_registry_runner_delivers_generic_dependencies` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_required_failure_blocks_while_optional_failure_reaches_d2` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_d2_sees_optional_placeholder_status_without_invented_evidence` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_early_exit_marks_later_selected_domains_skipped` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_undeclared_dependency_results_are_not_forwarded` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_conflicting_or_reserved_state_updates_fail_closed` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_blocked_required_context_gets_honest_final_response` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_sensor_error_and_missing_data_keep_priority_when_d2_is_blocked` | PASSED |
| `test_domain_architecture.GenericDependencyTests.test_domain_error_early_exit_is_visible_in_response` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_information_only_removes_unsolicited_coaching_and_conflicting_constraints` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_unknown_barrier_prevents_premature_planning` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_concrete_plan_exception_requires_request_and_acknowledged_uncertainty` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_barrier_labels_require_evidence_and_valid_enum` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_low_confidence_enforces_supportive_delivery` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_catalogue_absent_means_no_selections` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_catalogue_loader_and_id_validation` | PASSED |
| `test_domain_orchestration.StrategyValidationTests.test_malformed_strategy_falls_back_with_one_call_and_error_trace` | PASSED |
| `test_domain_orchestration.PlanningTests.test_four_domain_schedules_and_safe_default_dependency` | PASSED |
| `test_domain_orchestration.PlanningTests.test_a_to_e_structural_categories_from_mocked_model_outputs` | PASSED |
| `test_domain_orchestration.PlanningTests.test_failed_routing_clarifies_and_never_queries` | PASSED |
| `test_domain_orchestration.PlanningTests.test_greetings_skip_planner_call` | PASSED |
| `test_domain_orchestration.PlanningTests.test_operational_intents_cannot_accidentally_execute_domains` | PASSED |
| `test_domain_orchestration.PlanningTests.test_context_keeps_reply_and_history_without_arbitrary_payload` | PASSED |
| `test_domain_orchestration.PlanningTests.test_d2_runtime_prompt_has_policy_and_schema_without_evaluation_inputs` | PASSED |
| `test_domain_orchestration.PlanningTests.test_coaching_context_omits_query_text_but_keeps_evidence_status` | PASSED |
| `test_domain_orchestration.ExecutionTests.test_sequential_d2_receives_completed_d1_evidence` | PASSED |
| `test_domain_orchestration.ExecutionTests.test_parallel_domains_overlap_and_d2_has_no_d1_result` | PASSED |
| `test_domain_orchestration.ExecutionTests.test_d1_failure_reaches_d2_without_fabricated_evidence` | PASSED |
| `test_domain_orchestration.ExecutionTests.test_onboarding_blocks_d1_and_dependent_d2` | PASSED |
| `test_domain_orchestration.ExecutionTests.test_unimplemented_domains_return_honest_placeholders` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_real_d1_pipeline_compiles_guards_and_executes_mock_adx` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_coaching_only_uses_strategy_and_preserves_open_question` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_sensor_grounded_final_response_receives_both_domains` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_greeting_preserves_ordinary_chat` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_existing_special_responders_remain_reachable` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_missing_and_failed_sensor_requests_preserve_legacy_responses` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_transport_neutral_entrypoint_keeps_user_and_telegram_threads` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_invalid_typed_sensor_plan_never_reaches_adx` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_d1_zero_and_missing_metric_evidence_are_distinct` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_checkpointer_next_turn_clears_previous_domain_artifacts` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_d1_unsafe_kql_is_guarded_before_execution` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_trace_persistence_keeps_actual_user_turn_and_response_chain` | PASSED |
| `test_domain_orchestration.GraphRegressionTests.test_old_strategy_does_not_reenter_memory_prompt` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_help_keeps_command_inventory` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_login_starts_existing_pending_flow` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_login_one_shot_keeps_authentication_and_mapping` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_register_user_keeps_existing_registration_and_mapping` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_start_is_idempotent_for_completed_profiles` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_start_and_onboarding_answer_keep_existing_interview` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_cancel_handles_pending_interview_and_idle_paths` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_ordinary_message_keeps_transport_and_reply_context` | PASSED |
| `test_domain_orchestration.TelegramCompatibilityTests.test_threshold_and_daily_summary_configuration_paths` | PASSED |
| `test_offline.EvaluationOfflineTest.test_numeric_scoring_rounds_to_two_places_and_preserves_zero` | PASSED |
| `test_offline.EvaluationOfflineTest.test_typed_metric_reducers_and_common_aliases_preserve_semantics` | PASSED |
| `test_offline.EvaluationOfflineTest.test_query_result_number_uses_answer_column_not_date_evidence` | PASSED |
| `test_offline.EvaluationOfflineTest.test_csv_separates_ground_truth_from_question_input` | PASSED |
| `test_offline.EvaluationOfflineTest.test_csv_allows_unscored_questions_and_cli_persona_scopes_user` | PASSED |
| `test_offline.EvaluationOfflineTest.test_canonical_metadata_discovery_is_bounded_and_backend_neutral` | PASSED |
| `test_offline.EvaluationOfflineTest.test_time_metadata_precedence_and_workout_fallback_are_explicit` | PASSED |
| `test_offline.EvaluationOfflineTest.test_cli_method_selection_requires_a_real_pair` | PASSED |
| `test_offline.EvaluationOfflineTest.test_raw_planner_trace_is_evaluation_only` | PASSED |
| `test_offline.EvaluationOfflineTest.test_phia_entrypoint_writes_json_csv_and_summary_without_live_services` | PASSED |
| `test_offline.EvaluationOfflineTest.test_evaluation_clock_and_all_available_are_bounded_and_context_local` | PASSED |
| `test_offline.EvaluationOfflineTest.test_legacy_reference_date_is_summary_only_compatibility_shim` | PASSED |
| `test_offline.EvaluationOfflineTest.test_cached_plan_resolves_against_the_same_workout_clock` | PASSED |
| `test_offline.EvaluationOfflineTest.test_numeric_gold_score_uses_query_scalar_not_first_response_number` | PASSED |
| `test_offline.EvaluationOfflineTest.test_public_generation_service_honors_explicit_mode_without_env_mutation` | PASSED |
| `test_offline.EvaluationOfflineTest.test_runner_uses_public_generation_service_and_writes_full_trace` | PASSED |
| `test_offline.EvaluationOfflineTest.test_llm_v2_runner_reuses_production_guard_repair_loop` | PASSED |
| `test_offline.EvaluationOfflineTest.test_paired_runner_plans_once_and_truth_never_enters_runtime_item` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_workout_metric_filter_aggregation_and_absolute_window` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_cross_family_numeric_condition_is_scorer_only_and_inferable` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_last_slice_and_yesterday_use_workout_clock` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_hours_are_normalized_and_distinct_days_remain_distinct` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_pandas_std_defaults_to_sample_standard_deviation` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_last_scalar_slice_maps_to_none_and_last_one` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_incorrect_plan_components_are_false_not_reinterpreted` | PASSED |
| `test_diagnostics.PlanDiagnosticsTest.test_unsupported_reference_code_is_not_guessed` | PASSED |

## 10. 남은 TODO

- D3 실제 Health Knowledge/RAG/Safety 구현: 이번에는 명시적 placeholder와 교체 가능한 interface만 구성했다.
- D4 추가 문맥 해석·장기 기억 구현: 현재 기존 hydration은 유지하고 별도 D4는 placeholder다.
- BCT catalogue: 출처가 확인된 실제 catalogue가 포함되어 있지 않다. loader와 ID 검증 interface는 있지만 빈 catalogue에서는 선택을 비운다. 임의 BCT 목록을 만들지 않았다.
- D1 prompt 경로 이동: `user_initiated/plan_builder`, `query_generator`, `query_repair`의 실제 리소스 이동은 유보했다. `prompts/d1_sensor_data/README.md`에 명시했다.
- 공통 응답 prompt 경로 이동: 기존 `user_initiated/response_generator/` 리소스는 유지하며 `prompts/response/README.md`에 후속 이관을 기록했다.
- 실제 모델/서비스 통합 검증: 로컬 mock 검증과 구분하여, 사용자가 승인한 별도 환경에서 수행할 후속 작업이다.

## 11. main → red_teaming 통합 예상 충돌 파일

실제 working tree 변경과 구현 이동을 기준으로 한 **예상**이다. 다른 브랜치를 가져오거나 전환·병합하지 않았으므로 실제 충돌 발생 여부는 검증하지 않았다.

| 실제 변경 파일/영역 | 예상 충돌 및 검토 사항 |
|---|---|
| `chatbot/graph/workflow.py`, `state.py` | 상위 6노드 graph, 새 trace/dependency 필드와 red_teaming instrumentation 연결 |
| 루트 D1 5개 shim ↔ `nodes/d1_sensor_data/*.py` | 상대 브랜치가 루트 구현을 수정했다면 해당 로직을 새 canonical 파일에 반영해야 함 |
| `nodes/response_generator.py` ↔ `common/generate_final_response.py` | 기존 응답 instrumentation, strategy 적용, 오류 우선순위 및 평가 hook |
| `nodes/hydrate_context.py`, `persist_context.py`, `support.py` ↔ `common/*` | 문맥 필터링, thread/response chain, trace 저장과 기존 helper patch |
| `nodes/intent_router.py` ↔ `planning/route_helpers.py`, `plan_domain_execution.py` | 기존 router hook과 새 generic planner/validator 경계 |
| `nodes/domain_orchestration.py` ↔ `orchestration/execute_domains.py` | runner registry, required/optional 실패 처리 및 병렬 trace |
| `nodes/d2_coaching_strategy/*`, `schemas/domain_execution_*.py` | 1차 pair 전용 계획을 새 DAG schema로 바꾼 영향 및 Advisor 입력 계약 |
| `nodes/operational/*`와 기존 루트 responder shim | 명령·설정 관련 후속 수정의 올바른 적용 위치 |
| `local_test.py`, `tests/*`, `.gitignore` | 새 노드 이름과 구조 테스트, legacy import 보존 및 테스트 추적 예외 |
| `services/sensor_analytics.py`, `requirements.txt`, 응답/D2 prompt | 이번 시작 전부터 있던 1차 변경도 함께 검토해야 함 |
| `ARCHITECTURE.md`, 각 README, 두 한글 보고서 | 이전 flat/adapter 구조 설명과 현재 package 소유권 설명의 차이 |

평가 코드 자체는 이번에 수정하지 않았다. 기존 evaluation import가 유지되어도 shim을 통해 새 구현에 도달하므로, 향후 red_teaming 통합 시 루트에 별도 구현을 다시 추가하지 않아야 한다.
