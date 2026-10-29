# Sensei D1/D2 Advisor 구현 결과

> 이 문서는 1차 구현 당시의 역사적 스냅샷입니다. 본문의 경로와 테스트 수치는
> 당시 상태를 설명합니다. 현재 구조와 검증 결과는
> [현행 DAG/domain 보고서](DAG_DOMAIN_LAYOUT_REFACTOR_REPORT_KR.md)를 확인해 주세요.

작성일: 2026-09-07. 실제 변경 대상은 현재 저장소의 `azure_function_main_app`이다.
첨부 명세의 브랜치 생성·전환 지시보다 사용자의 명시적 금지 요청을 우선했다.
커밋, 푸시, 병합, 브랜치 전환, 배포 및 실서비스 데이터 쓰기를 수행하지 않았다.
파일 변경과 로컬 테스트만 수행했다.

## 1. 추가한 파일: 16개

- [`chatbot/nodes/d1_sensor_data/workflow.py`](../chatbot/nodes/d1_sensor_data/workflow.py)
- [`chatbot/nodes/d2_coaching_strategy/plan_coaching_strategy.py`](../chatbot/nodes/d2_coaching_strategy/plan_coaching_strategy.py)
- [`chatbot/nodes/d2_coaching_strategy/prepare_coaching_context.py`](../chatbot/nodes/d2_coaching_strategy/prepare_coaching_context.py)
- [`chatbot/nodes/d2_coaching_strategy/validate_coaching_plan.py`](../chatbot/nodes/d2_coaching_strategy/validate_coaching_plan.py)
- [`chatbot/nodes/domain_orchestration.py`](../chatbot/nodes/domain_orchestration.py)
- [`chatbot/nodes/planning/plan_domain_execution.py`](../chatbot/nodes/planning/plan_domain_execution.py)
- [`chatbot/prompts/d2_coaching_strategy/advisor_coaching_policy.py`](../chatbot/prompts/d2_coaching_strategy/advisor_coaching_policy.py)
- [`chatbot/prompts/d2_coaching_strategy/coaching_strategy_planning_prompt.py`](../chatbot/prompts/d2_coaching_strategy/coaching_strategy_planning_prompt.py)
- [`chatbot/prompts/planning/domain_execution_planning_prompt.py`](../chatbot/prompts/planning/domain_execution_planning_prompt.py)
- [`chatbot/prompts/user_initiated/response_generator/coaching_answer.txt`](../chatbot/prompts/user_initiated/response_generator/coaching_answer.txt)
- [`chatbot/resources/coaching/bct_catalog.py`](../chatbot/resources/coaching/bct_catalog.py)
- [`chatbot/schemas/d2_coaching_strategy_plan.py`](../chatbot/schemas/d2_coaching_strategy_plan.py)
- [`chatbot/schemas/domain_execution_plan.py`](../chatbot/schemas/domain_execution_plan.py)
- [`chatbot/schemas/domain_execution_result.py`](../chatbot/schemas/domain_execution_result.py)
- [`docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md`](../docs/D1_D2_ADVISOR_IMPLEMENTATION_REPORT_KR.md)
- [`tests/test_domain_orchestration.py`](../tests/test_domain_orchestration.py)

## 2. 수정한 파일: 17개

- [`.gitignore`](../.gitignore)
- [`ARCHITECTURE.md`](../ARCHITECTURE.md)
- [`README.md`](../README.md)
- [`chatbot/README.md`](../chatbot/README.md)
- [`chatbot/graph/README.md`](../chatbot/graph/README.md)
- [`chatbot/graph/state.py`](../chatbot/graph/state.py)
- [`chatbot/graph/workflow.py`](../chatbot/graph/workflow.py)
- [`chatbot/nodes/README.md`](../chatbot/nodes/README.md)
- [`chatbot/nodes/hydrate_context.py`](../chatbot/nodes/hydrate_context.py)
- [`chatbot/nodes/persist_context.py`](../chatbot/nodes/persist_context.py)
- [`chatbot/nodes/response_generator.py`](../chatbot/nodes/response_generator.py)
- [`chatbot/prompts/README.md`](../chatbot/prompts/README.md)
- [`chatbot/prompts/user_initiated/response_generator/data_answer.txt`](../chatbot/prompts/user_initiated/response_generator/data_answer.txt)
- [`chatbot/schemas/README.md`](../chatbot/schemas/README.md)
- [`chatbot/services/sensor_analytics.py`](../chatbot/services/sensor_analytics.py)
- [`local_test.py`](../local_test.py)
- [`requirements.txt`](../requirements.txt)

소스와 문서는 모두 주 작업 폴더 안에 있다. 별도로 저장소 루트의 무시되는
`.venv/`에 Python 3.12 테스트 환경과 앱 의존성을 설치했다. 전역 Python 패키지는
변경하지 않았다. 앱 `.gitignore`는 새 오프라인 테스트 파일 하나만 검토 대상으로
드러내며 기존 실험 자료와 설문 데이터의 제외 규칙을 유지한다.

## 3. 최종 상위 LangGraph

```text
START
  -> prepare_context
  -> plan_domain_execution
  -> execute_domains
  -> aggregate_domain_results
  -> generate_final_response
  -> persist_trace_and_context
  -> END
```

기존 `build_graph(checkpointer=None)` 호출 계약을 유지한다. 대화 준비는 기존
프로필·시간대·가용 데이터·에피소드·응답 체인 조회를 사용한다. 매 턴 이전 실행의
쿼리와 도메인 결과를 초기화하며 대화 히스토리와 기존 센서 계획 문맥은 유지한다.

의도 분류와 도메인 계획은 모델 호출 한 번으로 합쳤다. 지원 실행 방식은
D1 단독, D2 단독, D1 → D2, D1 ∥ D2다. 두 도메인을 선택하면 기본은 순차 실행이며,
D2가 센서 근거와 독립적이라고 명시한 경우에만 병렬 실행한다. 병렬 작업은 상태의
별도 사본과 요청별 contextvars를 사용하고 결과는 정해진 순서로 합친다.

## 4. D1 하위 그래프

```text
plan_sensor_query (기존 plan_builder)
  -> query_generator
  -> kql_guard
       | execute -> execute_sensor_query (기존 query_executor)
       | repair  -> query_repair -> kql_guard
       | blocked -> produce_sensor_result
  -> produce_sensor_result
  -> END
```

실행 불가능한 typed plan과 빈 쿼리는 결과 단계로 바로 종료한다. 기존 KQL 생성 방식,
사용자·기간 범위 검사, 읽기 전용 제한, 제한된 repair 및 ADX 재시도 정책을 재사용한다.
센서 질의 처리기를 다시 구현하지 않는다. D1이 완료된 뒤 필요한 D2에 측정값,
기간·지표, coverage, 누락 및 실패 상태를 전달한다. D2 입력에는 KQL 원문을 포함하지
않는다. 유효한 0과 `no_metric_evidence`/null을 구별한다.

## 5. D2 워크플로

```text
prepare_coaching_context (결정적 입력 선택)
  -> plan_coaching_strategy (스키마를 제공하는 모델 호출 1회)
  -> validate_coaching_plan (결정적 검사·정규화)
  -> DomainExecutionResult
```

D2는 최종 문장을 작성하지 않는다. 적용 여부, 대화 목적, 장벽 표현, BCT 선택,
MI/SDT 전달 제약 및 개입 깊이만 반환한다. 준비 단계는 현재 요청, 최근 대화,
기존 요약, 인용된 답장과 명시적으로 전달받은 D1 근거를 사용한다. 임의의 inbound
payload나 평가 필드를 전달하지 않는다.

JSON 파싱·스키마 검증 또는 모델 호출 실패 시 `status=error`와 짧은 탐색 전략을
반환한다. 검증을 위한 두 번째 모델 호출이나 Auditor는 사용하지 않는다.

## 6. D2 프롬프트 구성

- Advisor 전략 구성요소라는 역할과 최종 응답 작성 단계의 분리.
- NHS personalised care에 따른 사용자 가치·선택·자기관리·공동 계획 원칙.
- 사실 요청과 행동 변화 지원의 구별, 불필요한 목표 설정 방지.
- 대화 목적 enum의 일반적 의미.
- COM-B 여섯 구성요소, 근거 없는 장벽 추정 금지 및 UNKNOWN 허용.
- 제공된 카탈로그 ID만 고르는 BCT 규칙.
- MI의 협력, 반영, 개방형 질문, 자율성, 조언 허락과 성급한 해결 방지.
- SDT의 자율성·유능감·관계성 및 요청에 비례한 상호작용 깊이.
- JSON 출력 스키마.

새 오케스트레이션·D2 정책에는 사례별 정답 예시, required/allowed/forbidden BCT 정답,
MITI/TENS 점수표, MI-TAGS/AnnoMI 전문가 라벨을 넣지 않았다. 기존 D1·일반 응답
프롬프트의 예시는 보존했으며 새 D2 전략 입력에는 사용하지 않는다.

## 7. 구조화 출력 및 검증

`D2CoachingStrategyPlan`은 다음 필드를 갖는다.

| 필드 | 의미 |
|---|---|
| `coaching_applicable` | 코칭 적용 여부 |
| `interaction_depth` | INFORMATION_ONLY / VERY_BRIEF / BRIEF_COACHING / COACHING_CONVERSATION |
| `dialogue_objective` | NONE 및 명세의 일곱 대화 목적 |
| `comb_status`, `comb_components`, `comb_evidence_summary` | 장벽 상태, 유효한 COM-B 구성요소, 근거 요약 |
| `barrier_relevant`, `barrier_unresolved` | 중요한 미해결 장벽의 명시적 표현 |
| `planning_uncertainty_acknowledged` | 불확실성을 인정하는 잠정 계획 여부 |
| `selected_bcts`, `bct_rationale` | 지원 ID와 선택 근거 |
| `mi_delivery`, `sdt_support` | 별도 typed 모델로 표현한 전달 제약 |
| `strategy_rationale`, `response_constraints` | 전략 근거와 최종 응답 제약 |

Pydantic v2로 출력 JSON을 검증한다. 기존 텍스트 LLM 서비스에 JSON schema를 제공하고
반환값을 파싱하는 방식이며 제공자의 native structured-output API를 새로 요구하지 않는다.

코칭 미적용이면 목적 NONE, 정보 전달 깊이, 빈 BCT 목록으로 정규화한다. 중요한
미해결 UNKNOWN 장벽이 있으면 성급한 계획을 탐색으로 바꾼다. 사용자가 구체적 계획을
직접 요청했고 불확실성을 명시적으로 인정한 경우에만 잠정 계획을 허용한다.
카탈로그에 없거나 근거가 없는 BCT 선택을 제거한다. 자유문장 제약의 모순을 완전히
판별할 수 없으므로 typed 필드에서 실행 제약을 다시 생성하며 변경 필드를 trace에 남긴다.

공통 `DomainExecutionResult`는 domain, selected, status, summary, structured_result,
missing_information, confidence, provenance를 제공한다. confidence는 기본 null이다.

## 8. 재사용한 기존 서비스

- `services/sensor_analytics.py`: 상태 구성과 transport-neutral 그래프 호출 경계.
- `plan_builder`, `query_generator`, `kql_guard`, `query_repair`, `query_executor`.
- sensor contract, semantic resource/profile, KQL compiler/guard, 정규화와 coverage 정책.
- `services/llm` 및 기존 Responses API/Chat Completions fallback.
- `hydrate_context`, `persist_context`, operational storage 및 response chain.
- capabilities, onboarding, alert configuration responder.
- 기존 Telegram 명령어 처리, 인터뷰, threshold/daily-summary 관련 코드.

기존 명령어·인증 함수 파일과 Azure Function 등록 코드는 수정하지 않았다.

## 9. BCT 리소스

재사용한 BCT 카탈로그는 없다. 저장소에 검증된 자료가 없으므로 93개 분류를 임의로
만들지 않았다. 현재 카탈로그 상태는 `not_implemented`, 기본 `selected_bcts`는 빈 목록이다.

`resources/coaching/bct_catalog.py`는 source와 id/name/definition 엔트리를 검증하는
로더다. 이후 검증된 `bct_catalog.json`을 같은 디렉터리에 제공할 수 있다.
테스트에서 사용하는 가상 ID는 로더 계약 검증 전용이며 실제 BCT 분류가 아니다.

## 10. D3/D4

공통 도메인 인터페이스만 유지한다. 선택되면 `status=not_implemented`와 빈 결과를
반환한다. 도메인 계획 프롬프트는 이들을 선택하지 않도록 안내한다.
의료 RAG, 새 safety reasoner, D4 메모리 그래프, Auditor 및 CC 전용 API·인증은 추가하지 않았다.

## 11. 하위 호환성 영향

Telegram 일반 대화, /help, /login, /register_user, /start, 온보딩, /cancel,
센서 질의, ADX 실행 경계, 사용자·Telegram thread ID, 알림 및 일일 요약 설정의
기존 경로를 보존했다. 상위 노드 이름은 새 오케스트레이션 단계로 바뀌지만 공개
entrypoint 및 `ChatbotResponse` 필드는 유지한다. 평가 도구가 import하는 기존
query/guard 결정 함수도 유지한다.

최종 응답은 유효한 D2 코칭 전략이 있을 때 전용 프롬프트를 사용하고 전략에 필요한
질문을 보존한다. D1 오류, 데이터 부재, 계산 명확화 및 시간대 온보딩 응답은 기존
결정적 응답을 우선한다. 따라서 이 경우 D2 결과가 있어도 코칭 문장을 붙이지 않는다.

대화 저장 시 최종 AI 답변을 사용자 발화로 잘못 선택할 수 있던 부분을 실제 마지막
사용자 메시지를 선택하도록 수정했다. 기존 저장소의 active context에 최신 도메인
계획, 상태, D2 전략·검증 provenance와 호출·시간 정보를 기록한다. 전체 센서 행은
추가 저장하지 않으며 이전 전략 trace가 다음 턴 프롬프트로 다시 들어가지 않게 했다.

## 12. 테스트 결과

최종 환경: Python 3.12.10, langgraph 1.2.11, langchain-core 1.6.2, pydantic 2.13.5.
모든 검증은 로컬에서 수행했으며 모델·ADX·Telegram 전송·저장소 I/O는 모의 처리했다.

| 검증 | 상태 | 결과 |
|---|---|---|
| 신규 D1/D2 및 호환성 unittest | PASSED | 43개 |
| 기존 evaluation.test_offline / test_diagnostics | PASSED | 26개 |
| local_test.py smoke | PASSED | Azure Function 25개, 상위 노드 6개, D1 노드 구성, 텍스트 프롬프트 21개 및 기존 KQL 검사 |
| 앱 Python compileall | PASSED | 문법 컴파일 성공 |
| pip check | PASSED | 의존성 충돌 없음 |
| git diff --check | PASSED | 현재 저장소 줄바꿈 설정 기준 오류 없음 |
| 실제 LLM의 A–E 의미 분류 및 최종 코칭 문장 품질 | NOT EXECUTED | 구조 테스트는 고정 모의 모델 출력을 사용함 |
| 실제 Telegram–Azure OpenAI–ADX–Table Storage 통합 | NOT EXECUTED | 실서비스 호출·쓰기 없이 로컬 검증만 수행함 |
| Azure 배포 검증 | NOT EXECUTED | 사용자 요청에 따라 배포하지 않음 |

초기에는 테스트용 네트워크 차단이 Windows asyncio의 로컬 소켓도 막아 FAILED였고,
프롬프트 길이 제한 검사도 FAILED였다. 외부 통신은 차단하면서 로컬 이벤트 루프를
허용하고 프롬프트를 기존 길이 한도 안으로 정리한 뒤 통과했다. 시스템 Python 3.12에는
필수 패키지가 없어 최초 import 검사가 FAILED였으며 격리된 `.venv`에 requirements를
설치한 뒤 위 최종 검증을 모두 실행했다. 최종 실행에서 남은 FAILED 테스트는 없다.
스모크의 ADX 환경변수 부재 로그는 실서비스 자격정보 없이 수행한 로컬 검사에서
발생하며 스모크 자체의 종료 코드는 0이다.

실행 명령은 저장소 루트 기준이다.

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s azure_function_main_app/tests -v
.\.venv\Scripts\python.exe -m unittest evaluation.test_offline evaluation.test_diagnostics
.\.venv\Scripts\python.exe azure_function_main_app/local_test.py smoke
.\.venv\Scripts\python.exe -m compileall -q azure_function_main_app
.\.venv\Scripts\python.exe -m pip check
git diff --check
```

각 unittest의 최종 상태는 문서 끝에 기록했다.

## 13. 지연 시간 및 LLM 호출 수

| 경로 | 일반적인 호출 구성 |
|---|---|
| 정확한 인사·감사 | 라우팅 0 + 기존 최종 응답 최대 1 |
| D1 사실 조회 | 도메인 계획 1 + 기존 센서 계획 통상 1 + 최종 응답 0~1 |
| D2 단독 | 도메인 계획 1 + 코칭 전략 1 + 최종 응답 1 |
| D1 → D2 | 도메인 계획 1 + 기존 센서 계획 통상 1 + 코칭 전략 1 + 최종 응답 1 |
| D1 ∥ D2 | 호출 수는 위와 같고 독립 D1/D2 작업 시간이 겹침 |

기존 센서 후속 질의는 계획을 이어받아 센서 계획 호출을 생략할 수 있다.
기존 `KQL_GENERATE_METHOD=llm` 설정에서는 KQL 생성·수리 호출이 추가될 수 있고,
Responses API 실패 시 기존 fallback 호출도 발생할 수 있다. D2 검증 자체는 모델을
호출하지 않는다. 실제 서비스 지연 시간은 측정하지 않았다. trace에는 계획 및
도메인 실행 시간과 D2 호출 횟수·정규화 내역이 기록된다.

## 14. 남은 TODO

- 출처를 검증한 BCT 카탈로그 확보 및 로더 연결. 현재 빈 선택은 명세에 따른 의도된 상태다.
- 사용자 검토 후 별도 승인 범위에서 실제 배포 모델의 한국어·영어 라우팅, A–E 코칭
  행동과 문장 품질, 실제 Azure·Telegram 통합 및 지연 시간을 검증한다.
- D3/D4, Auditor, 평가 gold 작성 및 CC 통합은 이번 범위 밖의 후속 작업이다.

현재 요청 범위의 코드 구현 및 로컬 검증은 완료했다.

## 15. 향후 main → red_teaming 병합 시 충돌 가능 파일

아래는 공유 코어 수정 위치에 근거한 예상이며 브랜치를 전환하거나 실제 병합을
시도하지 않았다.

| 가능성 | 파일·영역 | 검토 이유 |
|---|---|---|
| 높음 | `chatbot/graph/workflow.py`, `graph/state.py` | 상위 그래프 교체와 상태 계약 추가 |
| 높음 | `chatbot/nodes/response_generator.py` | 코칭 전략과 최종 응답 결합 |
| 높음 | `chatbot/nodes/hydrate_context.py`, `persist_context.py` | 저장소 문맥, 실제 사용자 턴, trace와 응답 체인 |
| 중간 | `chatbot/services/sensor_analytics.py` | 공유 입력 상태 및 transport-neutral 경계 |
| 중간 | `requirements.txt`, `.gitignore`, `local_test.py` | 의존성, 새 테스트 공개 범위, 그래프 검사 |
| 중간 | `chatbot/prompts/user_initiated/response_generator/data_answer.txt` 및 구조 설명 문서 | 프롬프트·설명 동시 수정 가능 |
| 이름 충돌 여부 확인 | 새 planning/D1/D2/schema/resource 파일 | private 브랜치에 같은 이름의 미공개 구현이 있는지 확인 필요 |

CC 전용 transport·인증·사용자 매핑은 이번 변경에 포함하지 않았다. 향후 통합 시
공유 entrypoint에 다시 연결하고 Telegram과 CC를 각각 회귀 검증해야 한다.

## 부록: 실행한 개별 unittest 최종 상태

| 테스트 | 상태 |
|---|---|
| `test_domain_orchestration.py::StrategyValidationTests.test_information_only_removes_unsolicited_coaching_and_conflicting_constraints` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_unknown_barrier_prevents_premature_planning` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_concrete_plan_exception_requires_request_and_acknowledged_uncertainty` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_barrier_labels_require_evidence_and_valid_enum` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_low_confidence_enforces_supportive_delivery` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_catalogue_absent_means_no_selections` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_catalogue_loader_and_id_validation` | PASSED |
| `test_domain_orchestration.py::StrategyValidationTests.test_malformed_strategy_falls_back_with_one_call_and_error_trace` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_four_domain_schedules_and_safe_default_dependency` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_a_to_e_structural_categories_from_mocked_model_outputs` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_failed_routing_clarifies_and_never_queries` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_greetings_skip_planner_call` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_operational_intents_cannot_accidentally_execute_domains` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_context_keeps_reply_and_history_without_arbitrary_payload` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_d2_runtime_prompt_has_policy_and_schema_without_evaluation_inputs` | PASSED |
| `test_domain_orchestration.py::PlanningTests.test_coaching_context_omits_query_text_but_keeps_evidence_status` | PASSED |
| `test_domain_orchestration.py::ExecutionTests.test_sequential_d2_receives_completed_d1_evidence` | PASSED |
| `test_domain_orchestration.py::ExecutionTests.test_parallel_domains_overlap_and_d2_has_no_d1_result` | PASSED |
| `test_domain_orchestration.py::ExecutionTests.test_d1_failure_reaches_d2_without_fabricated_evidence` | PASSED |
| `test_domain_orchestration.py::ExecutionTests.test_onboarding_blocks_d1_and_dependent_d2` | PASSED |
| `test_domain_orchestration.py::ExecutionTests.test_unimplemented_domains_return_honest_placeholders` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_real_d1_pipeline_compiles_guards_and_executes_mock_adx` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_coaching_only_uses_strategy_and_preserves_open_question` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_sensor_grounded_final_response_receives_both_domains` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_greeting_preserves_ordinary_chat` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_existing_special_responders_remain_reachable` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_missing_and_failed_sensor_requests_preserve_legacy_responses` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_transport_neutral_entrypoint_keeps_user_and_telegram_threads` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_invalid_typed_sensor_plan_never_reaches_adx` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_d1_zero_and_missing_metric_evidence_are_distinct` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_checkpointer_next_turn_clears_previous_domain_artifacts` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_d1_unsafe_kql_is_guarded_before_execution` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_trace_persistence_keeps_actual_user_turn_and_response_chain` | PASSED |
| `test_domain_orchestration.py::GraphRegressionTests.test_old_strategy_does_not_reenter_memory_prompt` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_help_keeps_command_inventory` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_login_starts_existing_pending_flow` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_login_one_shot_keeps_authentication_and_mapping` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_register_user_keeps_existing_registration_and_mapping` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_start_is_idempotent_for_completed_profiles` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_start_and_onboarding_answer_keep_existing_interview` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_cancel_handles_pending_interview_and_idle_paths` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_ordinary_message_keeps_transport_and_reply_context` | PASSED |
| `test_domain_orchestration.py::TelegramCompatibilityTests.test_threshold_and_daily_summary_configuration_paths` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_numeric_scoring_rounds_to_two_places_and_preserves_zero` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_typed_metric_reducers_and_common_aliases_preserve_semantics` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_query_result_number_uses_answer_column_not_date_evidence` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_csv_separates_ground_truth_from_question_input` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_csv_allows_unscored_questions_and_cli_persona_scopes_user` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_canonical_metadata_discovery_is_bounded_and_backend_neutral` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_time_metadata_precedence_and_workout_fallback_are_explicit` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_cli_method_selection_requires_a_real_pair` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_raw_planner_trace_is_evaluation_only` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_phia_entrypoint_writes_json_csv_and_summary_without_live_services` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_evaluation_clock_and_all_available_are_bounded_and_context_local` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_legacy_reference_date_is_summary_only_compatibility_shim` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_cached_plan_resolves_against_the_same_workout_clock` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_numeric_gold_score_uses_query_scalar_not_first_response_number` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_public_generation_service_honors_explicit_mode_without_env_mutation` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_runner_uses_public_generation_service_and_writes_full_trace` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_llm_v2_runner_reuses_production_guard_repair_loop` | PASSED |
| `test_offline.py::EvaluationOfflineTest.test_paired_runner_plans_once_and_truth_never_enters_runtime_item` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_workout_metric_filter_aggregation_and_absolute_window` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_cross_family_numeric_condition_is_scorer_only_and_inferable` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_last_slice_and_yesterday_use_workout_clock` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_hours_are_normalized_and_distinct_days_remain_distinct` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_pandas_std_defaults_to_sample_standard_deviation` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_last_scalar_slice_maps_to_none_and_last_one` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_incorrect_plan_components_are_false_not_reinterpreted` | PASSED |
| `test_diagnostics.py::PlanDiagnosticsTest.test_unsupported_reference_code_is_not_guessed` | PASSED |
