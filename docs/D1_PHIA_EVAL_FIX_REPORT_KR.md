# D1 PHIA 평가 정확도 수정 보고서 (2026-10-15, 2차 반영 / 2026-10-18, 4차 반영)

> 4차(2026-10-18) 변경은 바로 아래 "4차 변경 요약"에 정리했습니다. 2장 이하는 2차 기준 설명이며, 4차에서 바뀐 부분은 요약에 적었습니다.

## 4차 변경 요약 (2026-10-18)

### A. 새 정답 파일 재확인
- 다시 올려 주신 `Objective_Query_-_PHIA_v3.xlsx`와 이전 파일을 비교했습니다. 차이는 text 정답 라벨 57행뿐이고, 숫자 gold는 같습니다.
- 지금까지의 trace(원본 claude 전체 실행, round5 등)를 새 파일로 다시 채점해도 정오가 바뀐 행은 **0개**입니다. 3.3의 오답 분류는 그대로 유효합니다.

### B. PHIA D1 전용 규칙을 `run_phia_all.py`로 이동
챗봇 코드와 프롬프트에서 평가 전용 분기를 모두 제거했습니다. `SENSEI_D1_EVALUATION` / `SENSEI_EVAL_ROUTING` 환경변수를 읽는 코드는 이제 없습니다.

| 규칙 | 이전 위치 | 지금 |
|---|---|---|
| D1 data path 강제 (`--routing d1`) | `plan_domain_execution.py` 환경변수 분기 | 러너가 하위 프로세스 안에서 domain planner를 D1 고정 함수로 교체 (`plan_domain_execution.py`는 원본으로 복원) |
| 기준일 = 이미 끝난 과거 날짜 (오늘 시작한 수면도 이미 있음) | planner 프롬프트 문장 + 템플릿 주석 | 러너가 D1 planner의 운영 메모리 입력(`memory_context`)에 평가 맥락으로 전달. 질문 텍스트에는 넣지 않습니다(결정적 파서가 질문의 날짜·시간 단어를 읽기 때문). |
| 확인 질문 생략 / typed 계획 실패 시 computation 제외 | `plan_sensor_query.py` 평가 분기 | 러너가 plan 노드를 감싸서 처리 (`plan_sensor_query.py`의 평가 코드 삭제) |
| KQL repair 2회, 잘못 고친 쿼리 1회 재시도 | `repair_sensor_query.py`, `workflow.py` 평가 분기 | 러너가 해당 함수를 교체 (챗봇 기본값은 원본대로 1회) |

동작 방식: 러너는 청크마다 `run_phia_all.py --phia-d1-child <d1|auto> <local_llm_test 인자>`를 실행합니다. 하위 프로세스는 위 규칙을 자기 프로세스에만 설치한 뒤 `local_llm_test.py`를 그대로 실행합니다.

planner 프롬프트에는 실시간 챗봇에도 맞는 일반 원칙만 남겼습니다. "오늘 수면 = 오늘 시작하는 밤"이라는 라벨 규칙과, 기준 시각과 맥락으로 이미 지난 밤인지 판단한다는 원칙입니다.

### C. 하영 학생 버전과 통일
- `run_phia_all.py`: 하영 학생 최신본(`run_phia_all_하영학생.py`)을 그대로 가져왔습니다. 질문 parser, chunk/재개, `--redo-network-errors`, `--retry-failed`(LLM 호출·네트워크 실패 문항만 다시 묻고 같은 위치에 넣기)가 모두 같습니다.
  - 추가한 것: `--routing` 옵션, 위 B의 하위 프로세스 규칙.
  - 이전에 제가 넣었던 parser 확장과 merge 백업은 제거했습니다.
- `grade_phia_traces.py`: label 판정을 하영 학생 버전(수정된정답이 "제외"로 시작)으로 되돌렸습니다. 추가한 것은 실행시간 열뿐입니다. 새 workbook에서 두 방식의 채점 결과는 같습니다.
- 출력 토큰: 하영 학생 원본(`local_models.py`)은 기본 상한이 8192였습니다. 우리 쪽은 모델별 실제 최대치(65536, claude 64000, llama 32768)로 설정해 답이 잘리지 않게 했습니다.

### D. 프롬프트 구조: 스키마 → 수집 가능한 데이터 → 그래서 KQL은 이렇게
`adx_schema_context.txt`를 세 부분으로 다시 썼습니다. 해커톤 문서의 Type 카탈로그("Collection time unit")와 `SensoryLLM_kql`의 raw→flat 및 MV 정의를 근거로 했습니다. PHIA, Custom Logger, PMData 어느 것에도 특정하지 않습니다.

1. **스키마**: 여섯 Sensor 함수 각각에서 한 행이 무엇인지 설명합니다. `HealthDataFlat` 열의 의미(`[DateFrom, DateTo)`는 레코드가 덮는 시간, `IngestionTime`은 도착 시각)도 포함합니다.
2. **수집될 수 있는 데이터**: Type을 다음 종류로 나눕니다. 그리고 생산자마다 다른 저장 형태(하루 1행 vs 10:35~10:39 같은 가변 짧은 구간, 여러 소스 겹침, 세션 재전송)와 메타데이터로 이를 구분하는 법을 설명합니다.
   - 누적량(걸음, 거리, 에너지, 운동 시간)
   - 측정값(심박, HRV, 안정시 심박, 체중 등. 더하지 않음)
   - 수면(세션·단계·비율)
   - 운동 세션(WORKOUT JSON)
   - 프로필 값
   - 이벤트·기록
3. **그래서 쿼리는 이렇게**: 다음 내용을 담습니다.
   - 종류별로 어느 함수를 읽는지
   - 재전송 중복 제거
   - 날짜·밤 라벨
   - 경계에 걸친 구간의 겹친 비율 배분(추정치 표시)
   - 단위

`generate_sensor_query.txt` 2단계는 이 분류를 참조하고, 어디서 읽는지만 네 줄로 요약합니다. 예시 수는 그대로입니다(planner 3개, KQL 2개).

### E. 오답 유형별 일반 보완 (대표 오답 문항으로 확인)
틀렸던 문항을 유형으로 묶었습니다. 각 유형을 특정 데이터셋이 아닌 일반 사실이나 방법으로 보완했고, 확인은 대표 문항으로만 했습니다.

| 유형 (주로 나온 모델) | 보완 (프롬프트) | 확인 결과 |
|---|---|---|
| plan 활동 필터 단어("WALKS", "HIKING")를 라벨처럼 비교해 0건 (llama) | "plan 필터는 사용자 단어이지 라벨이 아니다 → 관측 라벨로 비교" + 예시 1을 "ROWING → Indoor Rowing" 개수 예시로 교체 | 해당 문항 정답 |
| 빈 집합의 개수를 NULL로 반환 (llama) | "개수는 그 값 자체, iff(..., real(null))로 감싸지 않는다" + 예시 1이 개수 패턴을 보여 줌 | 3문항 모두 0으로 정답, claude·kimi 회귀 없음 |
| `20 / 60` 정수 나눗셈으로 속도 조건이 0이 됨 (llama) | KQL 사실: 정수 리터럴 나눗셈 | 해당 문항 정답 |
| 질문이 "total"인데 평균으로 바꿈 (claude 1회) | 제가 넣었던 "측정값은 절대 더하지 않는다"는 금지형 문장이 원인이라 사실 서술로 교체 + "total은 퍼센트·측정값이어도 합" | claude 정답 |
| 수면 시간을 반올림된 SensorSleepNights나 `DateTo - DateFrom`으로 계산 (grok, llama) | SensorSleepNights 설명 옆에 "flat 기록이 원본"을 되살림. MINUTE 기록은 ValueNumber가 곧 분. 생성 프롬프트의 timespan 문장은 llama가 구간 길이로 계산하게 만들어 repair 프롬프트로만 옮김 | grok 해결, llama 일부 남음 |
| 시간대 걸음에서 일별 rollup이나 여러 소스가 겹친 flat을 씀 (grok, kimi) | 시간별 rollup은 일별과 같은 원천(일별에 행이 있으면 시간별도 있음), 일별은 하루 일부를 답할 수 없음, 여러 소스 flat을 더하면 중복 | kimi·llama 정답, grok 미해결 |
| "최신 심박"에서 오늘이 빠짐 (kimi, grok) | planner에 "기간을 주지 않으면 오늘을 뺀 14일만 검색"이라는 인터페이스 사실 추가 | kimi·llama 정답 |
| iff/case/coalesce 타입 불일치, timespan 비교 오류 (llama, grok) | 생성 프롬프트에 분기 타입 일치, repair 프롬프트에 해당 오류별 수정법 | 대부분 해결 |

**금지형 규칙의 부작용**을 두 번 확인했습니다. "측정값은 더하지 않는다"는 문장은 명시적인 합계 요청을 덮어썼습니다. timespan 문장은 llama가 기록값 대신 구간 길이를 쓰게 만들었습니다. 둘 다 사실 서술로 바꾸거나, 오류가 났을 때만 보는 repair 프롬프트로 옮겼습니다.

**대표 오답 세트 결과** (`evaluation-output/dev_check_20261018_round4/targeted_failed_set_*`)

| 모델 | 1차 보완 후 | 2차 보완 후 (남은 문항만) |
|---|---|---|
| claude | 1/1, 회귀 확인 2/2 | 개수 2/2 |
| kimi | — | 개수 2/2 |
| grok | 6/8, 회귀 확인 4/4 | 1개 남음 (AZM 170 vs gold 200) |
| llama | 7/14, 회귀 확인 3/6 | 남은 8문항 중 3개 추가 해결. 남은 5개는 수면 조건 교차 질문 등 llama 단독 오류 |

**kist008 대표 5문항** (시간대 걸음 2, 최신 심박, 어젯밤 수면, 3밤 수면): claude 5/5, kimi 5/5, llama 5/5(이전 3/5), grok 1/5.

**kist008 전체 22문항** (영어 18, 한국어 3, 일반 대화 1; 챗봇 자체 라우팅, 일반 답변 모드, PHIA 러너 규칙 없음): 데이터 질문 21개를 ADX로 직접 계산한 참조값과 비교했습니다(마지막 두 보완 전).
- claude 21/21, kimi 21/21, llama 19/21, grok 18/21
- 일반 대화 질문은 claude만 chat 경로로 갔고, 나머지는 아래 F의 콘텐츠 필터 문제로 막혔습니다.
- 결과 파일: `evaluation-output/dev_check_20261018_round4/kist008_*`

**PHIA 개발셋 전체(93회)**: 마지막 두 보완 전의 프롬프트(round9) 기준으로 claude 93.5%, kimi 92.5%, grok 86.0%, llama 77.4%입니다. 요청하신 대로 마지막 보완은 대표 문항으로만 확인했고, 전체 셋은 다시 돌리지 않았습니다.
- claude의 남은 오답은 "longest run 날짜"(gold가 숫자인데 claude는 실제 날짜를 답함)와 "heart rate = 안정시 심박" 관례입니다.
- "total percentage"를 grok·llama는 전체 비율(평균)로 해석합니다. 사람에게도 자연스러운 해석이라 PHIA 관례에 더 맞추지 않았습니다.

### F. 실행 중 발견한 문제와 조치
- **청크가 멈춰도 감지하지 못함**:
  - 증상: grok 청크 하나가 53분 동안 CPU 17초만 쓰고 끝나지 않았습니다. 하위 프로세스 출력이 버퍼에 있어 로그가 비었고, 멈춘 위치도 남지 않았습니다.
  - 재현: 같은 질문을 스택 덤프를 켜고 다시 실행하니 582초에 정상 종료돼, 일회성 네트워크 대기로 판단했습니다.
  - 조치: `run_phia_all.py`가 하위 프로세스 로그를 버퍼 없이 쓰게 했습니다. 로그가 `--stall-minutes`(기본 20분) 동안 늘지 않으면 그 청크를 멈추고 실패로 기록하며, 같은 명령을 다시 실행하면 재시도합니다(테스트 포함).
- **Azure 콘텐츠 필터 오탐 (운영에 영향 가능)**: 한국어 질문 "잠을 더 잘 자려면 어떻게 해야 해?"가 다음과 같이 막혔습니다. 영어로 같은 질문을 하면 통과합니다.
  - 운영 기본 모델 gpt-5: 입력 단계에서 정책 위반으로 차단
  - llama, kimi, grok: 응답이 `SelfHarm` 라벨로 차단(HTTP 400)

  그 결과 domain planner가 실패해 "어느 영역을 확인할까요?"라는 되묻기로 끝납니다. 코드가 아니라 Azure 배포의 콘텐츠 필터 설정 문제라 운영 측 확인이 필요합니다.
- **최신 측정값의 기본 기간**: planner가 기간을 비우면 결정적 코드(`time_ranges._default_time_range`, 원본 그대로)가 "오늘을 뺀 14일"로 채웁니다. 실시간 챗봇에서도 "최신 심박"이 오늘 기록을 놓칠 수 있습니다. 지금은 planner 프롬프트의 인터페이스 설명으로 막았고, 코드 기본값을 바꿀지는 결정이 필요합니다.
- **grok이 KQL을 중간에 끊어 보냄**: 정상 종료(stop)인데 KQL이 `datetime(2026-10-08`처럼 잘려 guard가 거부합니다. 우리 쪽 추출 함수는 자르지 않으므로 모델/제공자 쪽 현상입니다.
- **답변 언어**: 한국어 질문에도 영어로 답합니다. `generate_final_response.py`에서 `answer_language = "English"`가 고정된 원래 설계이며, D1 원본과 main_github 모두 같습니다.

## 0. 원칙

- **LLM이 추론하고, 코드는 안전과 연결만 담당합니다.** 코드가 쿼리 모양이나 사용할 테이블을 규칙으로 정하거나 강제로 다시 쓰게 하지 않습니다.
- 프롬프트에는 과제, 답에 이르는 방법, 데이터에 대한 사실, 소수의 예시만 담습니다. 예시는 테스트셋과 다르게 만듭니다.
- PHIA에 맞추지 않습니다. 같은 코드가 나중에 Custom Logger 데이터(kist008)에서도 돌아야 하므로 kist008 데이터로 직접 검증했습니다.

## 1. 배경과 진단 (1차)

원본 D1 코드(`D1_azure_function_main_app.zip`)로 PHIA user 41의 1,000문항을 평가한 결과입니다.

| 모델 | 정확도 | 주된 실패 원인 |
|---|---|---|
| gpt-6-1-sol | 86.5% | 대부분 gold 자체 문제 |
| claude-opus-5-5 | 85.1% | 추상적 규칙 위주 프롬프트로 인한 과잉 해석 |
| kimi-k2-6 | 33.7% | 추론 토큰이 출력 한도 8192를 소진(`finish_reason=length`) |
| llama-4-maverick | 1.8% | planner가 검증기가 받지 않는 time_range를 생성 |
| grok-4-1-fast-reasoning | 0.6% | ```` ```kusto ```` 코드펜스 언어 태그가 남아 guard가 차단 |

강한 두 모델이 **둘 다** 틀리는 문항이 84/931개라 실질 상한은 약 90%입니다.

## 2. 현재 변경 내용 (원본 대비)

노드 구조(plan → generate → guard → repair → execute → produce)와 프롬프트 파일 이름, placeholder는 그대로입니다. 데이터셋별 분기는 없습니다.

### 2.1 프롬프트

| 파일 | 내용 |
|---|---|
| `plan_sensor_query.txt` | 작성 절차와 검증기가 받는 JSON 인터페이스(`time_range` 형태, `time_of_day`, 비교 기간, 최근 N시간, 최신 측정값, `computation` 구성, metric 어휘)를 설명하고 예시는 **3개**만 둡니다. |
| `generate_sensor_query.txt` | 방법 8단계를 둡니다. 핵심은 "관측 메타데이터를 보고 이 사용자의 측정값이 어느 relation에 어떤 형태로 있는지 판단"하는 단계입니다. 그 밖에 KQL 언어 사실(예: by가 있는 summarize는 빈 입력에서 행이 없음, `max_of`, 정확한 median 방법, 문자열 전체 비교)과 방법을 보여 주는 예시 **2개**를 둡니다. 2개 모두 ADX에서 실행 검증했고, 교차 예시는 kist008 실제 데이터 결과가 SensorSleepNights 값과 일치합니다. |
| `adx_schema_context.txt` | 함수별 한 행의 의미, Type별 뜻(SLEEP_SESSION = 기록된 수면 세션 시간 등), 같은 측정값의 세 가지 저장 형태(완결 값 / 더해야 하는 구간 레코드 / 측정 시점 값)와 이를 메타데이터로 구분하는 법, 중복 제거, 경계 구간 배분, 날짜·단위 규칙을 담습니다. |
| `repair_sensor_query.txt` | 컴파일 오류 진단 절차와, 자주 나오는 Kusto 오류별 수정 방법을 담습니다. |

예시는 가상 사용자 `u7`, 2024년 날짜, 테스트에 없는 활동(rowing, pilates)으로 만들었습니다. `tests/test_d1_prompt_examples.py`가 예시 plan이 검증을 통과하는지, 예시 KQL이 guard를 통과하는지, 예시 질문이 workbook에 없는지 검사합니다.

### 2.2 Custom Logger(kist008) 확인에서 고친 것

이 다섯 가지는 PHIA만 보았다면 놓쳤을 문제입니다.

1. **수면 레코드 형태**
   - 문제: PHIA는 세션당 완결 값 1행이지만, kist008은 **1분 단위 단계 구간** 수십만 행입니다. 스키마가 PHIA 형태를 전제하고 있었습니다.
   - 수정: 두 형태와 그 구분법을 사실로 서술했습니다. 밤별로 더하는 방법은 두 형태에서 모두 맞습니다(kist008: flat 합계 166분 = SensorSleepNights 166분).
2. **중복 제거 키**
   - 문제: kist008 SLEEP_SESSION은 같은 Uuid로 끝 시각만 늘어나며 재전송됩니다(303분 → 381분). 기존 키 (Type, Uuid, DateFrom, DateTo)로는 중복 합산됩니다(3밤 1320분, 정답 852분).
   - 수정: 운영 ADX의 `PhoneSleepSessionDedupMV`와 같은 기준인 **(Type, Uuid, DateFrom)별 최신 수집본**으로 바꿨습니다. 단계 구간은 Uuid를 공유해도 시작 시각이 달라 보존됩니다. PHIA 결과는 동일합니다.
3. **경계에 걸친 구간**: 겹친 비율(겹친 시간 / 원래 구간 길이)로 배분하고 `IsEstimate`/`AllocationMethod`를 표시하는 설명을 되살렸습니다(1차에서 제가 뺐던 내용). claude가 이를 정확히 적용하는 것을 확인했습니다.
4. **시간대 누적 활동**
   - 문제: kist008 걸음수는 폰·Samsung Health·Withings가 겹치고, 하루 전체를 담은 요약 레코드도 섞여 있습니다. flat 레코드를 더하면 약 3배가 됩니다.
   - 수정: "일별·시간별 rollup에 행이 있으면 그것을 쓴다(소스 겹침과 경계 배분이 이미 처리됨)"를 판단 절차에 넣었습니다.
5. **inventory 버그 (`services/sensor_inventory.py`)**
   - 문제: 1시간 창으로 조회하면 일별 rollup 행(LocalDate 00:00)이 빠져 "empty"로 잘못 보고됐습니다. 그래서 모델이 flat으로 갔습니다.
   - 수정: ADX 함수와 같은 `startofday(StartLocal)` 기준으로 맞췄습니다. 이후 시간대 질문은 claude·llama·kimi 모두 정답입니다.

### 2.3 코드

| 파일 | 변경 |
|---|---|
| `nodes/d1_sensor_data/generate_sensor_query.py` | 코드가 "증거 충돌"을 감지해 LLM에 다시 쓰게 하던 루프를 **제거**했습니다. 감지 결과는 trace에 진단용(`diagnostic_conflicts`)으로만 남깁니다. KQL 생성 LLM 호출은 문항당 1회(+ 컴파일 오류 시 repair)이고, planner 호출과 합쳐 보통 2회입니다. |
| `nodes/d1_sensor_data/guard_sensor_query.py` | 따옴표 없는 사용자 ID와 정의되지 않은 `TargetUserId`를 인증 사용자로 바인딩합니다. 권한 범위는 그대로이며, 다른 사용자는 계속 차단됩니다. |
| `nodes/d1_sensor_data/plan_sensor_query.py` | measurement-scope review(추가 LLM 호출)는 기본으로 끕니다(`SENSEI_D1_SCOPE_REVIEW=1`로 켬). planner 호출 실패 시 검증 오류에 예외 타입을 기록합니다(`--retry-failed` 대상이 되도록). 평가 전용 분기는 4차에서 `run_phia_all.py`로 옮겼습니다. |
| `nodes/planning/plan_domain_execution.py` | 4차에서 원본으로 복원했습니다. D1 강제 경로는 `run_phia_all.py`에 있습니다. |
| `services/sensor_inventory.py` | 위 5번의 inventory 버그 수정입니다. |
| `adx_client.py` | 429 throttle 재시도를 강화했습니다(6회, 최대 30초 backoff+jitter). 로컬 토큰 획득 실패도 재시도합니다. |
| `local_models.py`, `local_models.json` | **출력 토큰 제한을 해제**했습니다. 모델별 실제 상한을 확인해 65536, claude는 64000으로 설정했습니다. llama만 32768인데, 반복 붕괴가 일어나도 요청 timeout 180초 안에 끝나게 하려는 것이며 정상 KQL은 1~2천 토큰입니다. llama는 temperature 0에서 반복 붕괴하면 `frequency_penalty 0.3`으로 1회 재시도합니다. |

### 2.4 평가 도구

- `run_phia_all.py --routing d1|auto`
  - `d1`(기본): 모든 질문이 D1 data path로 갑니다. 지금까지와 같습니다.
  - `auto`: 도메인 planner가 D1~D4와 chat/data/unsupported를 고릅니다. 나중에 Custom Logger를 평가할 때 씁니다.
  - 4차: 하영 학생 최신본과 통일했고, PHIA D1 전용 규칙을 이 파일에 모았습니다(위 4차 요약 B, C).
- **실행시간 열**: `local_llm_test.py`가 문항마다 단계별 시간을 trace의 `timings`에 기록하고, `grade_phia_traces.py`가 CSV 맨 뒤에 다음 열을 추가합니다. 기존 열 순서는 그대로입니다.
  - 단계별 시간(초): Total, Plan, Query Generation, Schema Inventory, Guard, Repair, ADX Execution
  - LLM Time(초)과 LLM Calls, ADX Query Time(초)과 ADX Queries

## 3. 결과

### 3.1 PHIA 개발셋 (86문항, 93회, D1 폴더 코드)

| 모델 | 원본 코드 | 1차 수정 전 | 1차 (예시 8개) | 2차 (round5, 예시 2~3개) |
|---|---|---|---|---|
| claude-opus-5-5 | — | 93.3% | 96.8% | **94.6%** |
| kimi-k2-6 | 23/60 | 49.4% | 95.7% | **91.4%** |
| grok-4-1-fast-reasoning | 1/60 | 76.9%(부분) | 94.6% | **88.2%** |
| llama-4-maverick | 1/60 | 40.9% | 77.4% | **79.6%** |

중간 시도(round2~4)에서 배운 것:
- 예시만 줄이고 판단 방법을 비워 두면 성능이 크게 떨어졌습니다(round2: llama 55.9%, claude 92.0%).
- 데이터의 **사실**(Type별 의미, rollup 우선 등)과 **판단 방법**을 넣자 회복했습니다.
- "빈 결과라도 한 행을 만들라"는 **지시**는 grok이 `datatable`/`print`로 우회 구조를 만들다 문법 오류를 내게 해 사실 서술로 바꿨습니다.

2차에서 1차보다 낮아진 부분은 대부분 **PHIA 전용 관례를 일부러 넣지 않았기 때문**입니다.
- "heart rate"를 PHIA gold는 안정시 심박으로 봅니다. 지금은 HEART_RATE 기록이 없으면 NULL입니다(모델당 2~3문항).
- "total percentage"를 gold는 비율의 합으로 봅니다(grok·llama는 평균으로 계산).
- "오늘 수면"을 gold는 오늘 시작한 밤으로 봅니다. 모델이 "오늘 깬 수면"으로 해석하는 경우가 있는데, 실제 사용자 관점에서는 자연스러운 해석입니다.
- "What is the date of my longest run?"은 gold가 행마다 거리값과 시간값으로 엇갈립니다.

### 3.2 kist008 (Custom Logger) 스모크, 11문항, gold 없음

참조값은 직접 KQL로 계산했습니다. 결과는 `evaluation-output/dev_check_20261015_round2/kist008_smoke.csv`에 있습니다.

| 질문 | 참조값 | claude | llama | grok |
|---|---|---|---|---|
| 어제 걸음수 | 13162.10 | ✓ | ✓ | ✓ |
| 7일 평균 안정시 심박 | 53.36 / 53.44 | ✓ | ✓ | ✓ |
| 3밤 깊은수면 합 | 166 | ✓ | ✓ | ✓ |
| 3밤 수면 시간 | 852(세션) / 530(실수면) | 852 | 852 | 530 |
| 14일 걷기 횟수 | 16(ID) / 9(시작시각) | 16 | 16 | 16 |
| 어제 평균 심박 | 54.8 | 54.77 | 54.77 | 54.77 |
| 30일 최장 걷기 거리 | 16021 | ✓ | ✓ | ✓ |
| 7일 최고 HRV | 88 | ✓ | ✓ | ✓ |
| 어제 10~11시 걸음수 | 231 | ✓ | ✓ | ✗ (KQL 오류) |
| 어제 정오 이전 걸음수 | 5934.11 | ✓ | ✓ | ✗ (KQL 오류) |
| 어제 3~5시 평균 심박 | 54.27 | ✓ | 54.25 | ✓ |

kimi는 시간대 2문항만 확인했고 둘 다 정답입니다.

**실행시간**: kist008처럼 데이터가 많은 사용자는 **쿼리 생성 단계 안의 스키마 inventory 조회(ADX)**가 병목입니다. 30일 창에서 최대 약 90초가 걸리고, 최종 쿼리 실행은 1~6초입니다.

### 3.3 claude가 왜 틀리는가 — 원인 분석

원본 실행에서 claude가 틀렸던 user 41의 143개 고유 문항을 현재 코드로 claude와 gpt-6-1-sol에서 다시 실행했습니다. 그리고 실패를 하나씩 열어 원인을 분류했습니다. 분류 결과는 `evaluation-output/dev_check_20261015_round2/claude_failure_classification.txt`에 있습니다.

- 현재 코드로 다시 실행하니 117회 중 52회가 정답이 됐습니다. 남은 실패는 고유 문항 61개입니다.
- **기간 해석(코드가 강제)은 원인이 아닙니다.** "지난 N일 = 기준일 제외 N일"이 계산 가능한 PHIA summary 문항 234개 모두에서 gold와 일치했습니다.

| 원인 | 문항 수 | 설명 |
|---|---|---|
| gold 오류·불일치 | 26 | 해당 기록이 있는데 gold가 0이거나 NULL입니다(Outdoor Bike를 biking으로 보지 않음). 빈 집합을 0으로 둔 미수정 gold가 있고, "average"가 실제로는 중앙값이며, 거리 질문에 속도값이 들어 있습니다. |
| 채점 정밀도 | 9 | 소수 둘째 자리로 반올림된 원본을 더해 0.01 차이가 납니다(예: 1588.36 vs 1588.37). 허용오차 0.005 때문에 오답 처리됩니다. |
| 질문의 모호함 / 정의되지 않은 관례 | 21 | "today 수면"(10문항)은 수정됨, 아래 참고. "sports"가 특정 라벨인지 운동 전체인지(8), "past week"(1), "heart rate" = 안정시 심박(PHIA 관례, 1), "How many days today …"(1) |
| claude의 실제 판단 오류 | 5 | "run"을 Treadmill 라벨에 매칭했습니다. 수정됨, 아래 참고. |

claude만 틀리고 gpt-6-1-sol은 맞힌 16문항 중 15개는 "today 수면"이었습니다.
- claude는 계획 단계에서 해석 근거를 직접 적었습니다: "오늘 밤 수면은 아직 일어나지 않았으니 오늘 아침에 끝난 수면으로 본다". 즉 **판단을 못 한 것이 아니라, 평가 기준일이 이미 끝난 날짜라는 사실을 몰랐던 것**입니다.
- planner에 이 사실(날짜만 있는 기준 시각 = 그날 전체가 이미 지남)과, 수면을 밤 라벨로 고르는 이유를 설명으로 추가했습니다. 실시간 챗봇처럼 기준 시각에 시간이 있으면 방금 끝난 수면으로 읽을 수 있다는 점도 함께 적었습니다.
- 결과: 해당 16문항과 회귀 확인 12문항 **30/30 정답**.

"run"을 Treadmill로 매칭한 문제는 "의미가 같은 다른 표현은 매칭하고, 기구나 관련 활동이 별도 라벨이면 다른 활동"이라는 의미 설명을 되살려 고쳤습니다. 원래 프롬프트에 있던 내용을 제가 줄이면서 뺐던 것입니다. 이후 run 문항은 "run 기록 없음"으로 답하고, 활동 관련 회귀는 없습니다.

결론: 남은 claude 오답은 gold 오류, 채점 정밀도, **사람에게도 모호한 질문**이 대부분입니다. 앞의 두 가지는 프롬프트로 해결할 대상이 아닙니다. 모호한 질문은 어떤 관례를 따를지 정해 주면(평가 명세) 맞출 수 있습니다.

## 4. 남은 한계 / 확인 필요

- **ADX `SensorActivityInterval` 오류**: 이 함수가 쓰는 `PhoneCumulativeIntervalDedupMV`가 "원본 테이블과 호환되지 않음(SEM0485)" 오류를 냅니다. ADX 운영 측 조치가 필요하며 D1 범위 밖입니다.
- **inventory 조회 속도**: 데이터가 많은 사용자에서 문항당 수십 초가 걸립니다. 최적화는 별도 작업입니다.
- **데이터 자체의 모호함**: 같은 산책이 두 소스에 기록되어 운동 횟수가 16과 9로 갈립니다.
- **Azure 간헐 오류**: llama의 content filter(`IPNLCitations`)와 네트워크 timeout은 간헐적이며 `--retry-failed`로 재시도됩니다.
- **동시 실행 권장**: ADX throttle과 `az` 토큰 충돌 때문에 동시 프로세스는 약 8개 이하를 권장합니다(`--workers` ≤ 모델 수).
- **테스트**: 작업 전부터 실패하던 6개는 이번 범위에서 고치지 않았습니다(`test_local_llm_models` 모델 개수, `test_kql_repair_routing` 3개, `test_sensor_inventory` 1개).
- **D1 밖 문서**: `참고자료/SensoryLLM_Azure_code_review.md` 등은 권한 밖이라 갱신하지 않았습니다. chatbot으로 옮길 때 함께 갱신해야 합니다.
- **round5 이후 수정**: 시간별 rollup 절차, inventory 수정, `max_of` 안내, 기준 시각·수면 라벨 설명, 활동 매칭 의미 설명은 PHIA 개발셋 전체를 다시 돌리지 않았습니다. 대상 문항·회귀 문항·kist008·단위 테스트로만 확인했으니 전체 실행으로 확인해 주세요.

## 5. 되돌리기

- 1차 수정 전: `_snapshots/D1_llm_azure_function_main_app_snapshot_20261015_before_fix.zip`
- 2차 수정 전: `_snapshots/D1_llm_azure_function_main_app_snapshot_20261015_round2.zip`
- 4차 수정 전: `_snapshots/D1_llm_azure_function_main_app_snapshot_20261018_round3.zip` (코드만)
