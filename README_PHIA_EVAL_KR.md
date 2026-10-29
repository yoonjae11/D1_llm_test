# D1 PHIA 평가 실행 안내 (연구원용)

PHIA 객관식 질의 4,000문항(사용자 465·171·333·41 각 1,000문항)을 여러 LLM으로 돌리고 자동 채점하는 방법을 설명합니다. 이 폴더에는 실제 챗봇의 D1(센서 데이터) 코드가 그대로 들어 있습니다. 각 질문은 실제 Azure Data Explorer(ADX) 데이터와 실제 모델 API를 사용합니다.

---

## 1. 무엇이 들어 있나

| 경로 | 역할 |
|---|---|
| `run_phia_all.py` | **평가 실행기.** 정답 파일에서 문항을 읽고, 모델·사용자·청크 단위로 실행한 뒤 채점까지 합니다. PHIA D1 평가 전용 규칙도 이 파일에만 있습니다. |
| `grade_phia_traces.py` | 채점기. 실행 결과(trace JSON)를 정답과 비교해 CSV 하나로 만듭니다. `run_phia_all.py`가 마지막에 자동으로 호출합니다. |
| `local_llm_test.py` | 한 사용자·한 모델로 질문을 실행하는 도구입니다. `run_phia_all.py`가 내부에서 호출합니다. |
| `Objective_Query_-_PHIA_v3.xlsx` | 정답 파일. 수정된정답이 있으면 그 값을, 없으면 기존 PHIA 정답을 씁니다. |
| `local_models.json` | 사용할 수 있는 모델 이름과 API 형식, 출력 토큰 상한. |
| `summarize_phia_results.py` | 채점 CSV로 모델별 요약표(정답률, 순수 추론 시간, 대기 시간, 토큰)를 만듭니다. |
| `local.settings.eval.sample.json` | 평가용 설정 예시. 복사해서 `local.settings.json`으로 씁니다(2장). |
| `chatbot/` | 챗봇 본체(LangGraph 노드, 프롬프트, guard, ADX 클라이언트). |
| `chatbot/prompts/d1_sensor_data/` | D1 프롬프트. 설명은 같은 폴더의 `README.md`를 보세요. |
| `docs/D1_PHIA_EVAL_FIX_REPORT_KR.md` | 지금까지의 수정 내용과 결과 보고서. |
| `tests/` | 단위 테스트. |

실행 결과 폴더(`evaluation-output/`), 가상환경, 비밀값이 든 `local.settings.json`은 들어 있지 않습니다.

---

## 2. 처음 한 번만 준비

### 2.1 가상환경
기존에 쓰던 챗봇 가상환경(`requirements.txt` 패키지와 `openpyxl`이 설치된 환경)을 그대로 쓰면 됩니다. 실행 전에 그 가상환경을 켜세요. Git Bash 예시는 아래와 같습니다.

```bash
source <가상환경 경로>/.venv/Scripts/activate
```

이후에는 터미널을 열 때마다 이 명령을 실행하면 됩니다. 프롬프트 앞에 `(.venv)`가 보여야 합니다. `run_phia_all.py`는 하위 프로세스도 지금 실행 중인 Python으로 띄우므로, 가상환경을 켠 상태로 실행하기만 하면 됩니다.

### 2.2 Azure 로그인 (ADX 접근)
```bash
az login
```
구독 목록이 나오면 **ADX 읽기 권한이 있는 구독**를 선택하세요(보통 4번). 계정에는 ADX `YOUR-CLUSTER.YOUR-REGION`의 `healthdb`를 읽을 권한이 있어야 합니다.

### 2.3 설정 파일
```bash
cp local.settings.eval.sample.json local.settings.json
```
`local.settings.json`을 열어 `AZURE_OPENAI_API_KEY` 값을 담당자에게 받은 모델 API 키로 바꾸세요. 나머지 값(ADX 주소, DB 이름)은 그대로 두면 됩니다.

> 이 파일에는 비밀값이 들어가므로 다른 사람에게 보내거나 git에 올리지 마세요.

### 2.4 동작 확인 (1~2분)
```bash
python run_phia_all.py "Objective_Query_-_PHIA_v3.xlsx" --models claude-opus-5-5 --dry-run
```
`Chunks: 84 total ... (4000 question-runs)`가 나오면 문항 읽기는 정상입니다. 실제 실행이 되는지는 3.1처럼 한 사용자로 짧게 돌려 보세요.

---

## 3. 실행

모든 명령은 `D1_PHIA_eval` 폴더에서, 가상환경을 켠 상태로 실행합니다.

### 3.1 한 모델, 한 사용자로 짧게 확인
```bash
python run_phia_all.py "Objective_Query_-_PHIA_v3.xlsx" --models kimi-k2-6 --users 41 --workers 2 --out-dir evaluation-output/test_kimi --csv evaluation-output/test_kimi.csv
```

### 3.2 여러 모델로 4,000문항 전체
`--models`를 생략하면 아래 11개 모델(기본 구성)을 모두 실행합니다.

| 구분 | 모델 |
|---|---|
| 운영 기준선 (Sensei가 쓰는 모델) | gpt-5 |
| 비교 쌍: GPT-6 | gpt-6-sol ↔ gpt-6-luna (경량) |
| 비교 쌍: Claude 5.5 | claude-opus-5-5 ↔ claude-haiku-5-5 (경량) |
| 비교 쌍: DeepSeek V4 | deepseek-v4-pro ↔ deepseek-v4-flash (경량) |
| 비교 쌍: grok 4.1 fast | grok-4-1-fast-reasoning (추론 켬) ↔ grok-4-1-fast-non-reasoning (추론 끔) |
| 참고 (경량판 없음) | kimi-k2-6, llama-4-maverick |

```bash
python run_phia_all.py "Objective_Query_-_PHIA_v3.xlsx" --workers 6 --out-dir evaluation-output/phia_all --csv evaluation-output/phia_all_results.csv
```
- 동시 실행 수는 ADX 부하 때문에 6~8을 권장합니다(기본값은 모델 수인 11).
- 시간 비교가 공정하도록 모든 모델을 같은 `--workers`, 같은 시간대에 실행하세요.

- 기본으로 4,000문항을 한 번씩 묻습니다(모델당 4,000회).
- 같은 문항을 여러 번 반복해 실행마다 답이 얼마나 흔들리는지 보려면 `--repeats N`을 주세요.
  - 예: `--repeats 5`면 4,000문항을 5바퀴 돌아 모델당 20,000회가 됩니다.
  - 결과는 `r1/`…`r5/` 폴더에 바퀴별로 저장되고, 채점 CSV의 `Repeat` 열(1~5)로 구분됩니다.
- 일부 모델만 돌리려면 `--models gpt-6-sol gpt-6-luna`처럼 지정합니다. 사용 가능한 이름은 `local_models.json`에 있습니다.
- 모델을 따로따로 돌려도 됩니다. 모델마다 `--out-dir`와 `--csv`를 다르게 주면 결과가 섞이지 않습니다.

### 3.3 주요 옵션

| 옵션 | 의미 | 기본값 |
|---|---|---|
| `--models` | 실행할 모델들 | 11개 모델 (3.2 표) |
| `--repeats` | 4,000문항 전체를 몇 번 반복할지 | 1 |
| `--users` | 일부 사용자만 (`465 171 333 41` 중) | 4명 모두 |
| `--chunk-size` | 한 프로세스가 묻는 문항 수 | 50 |
| `--workers` | 동시에 실행할 프로세스 수 | 모델 수 |
| `--out-dir` | trace와 로그를 저장할 폴더 | `evaluation-output/phia` |
| `--csv` | 채점 결과 CSV | `evaluation-output/phia_results.csv` |
| `--retry-failed` | 끝난 청크 중 LLM 호출·네트워크 실패 문항만 다시 묻기 | 끔 |
| `--redo-network-errors` | network/timeout 오류가 있는 청크 전체를 다시 실행 | 끔 |
| `--stall-minutes` | 로그가 이 시간 동안 늘지 않으면 그 청크를 멈추고 실패로 기록 | 20 |
| `--routing` | `d1`: 모든 문항을 D1 데이터 경로로 / `auto`: 챗봇 자체 라우팅 | `d1` |
| `--dry-run` | 실행 계획만 출력 | 끔 |

### 3.4 중단과 재개
- 언제든 Ctrl+C로 멈춰도 됩니다. **같은 명령을 다시 실행하면** 끝난 청크는 건너뛰고 나머지만 실행합니다.
- 전체가 끝난 뒤 실패 문항만 다시 묻기:
  ```bash
  python run_phia_all.py "Objective_Query_-_PHIA_v3.xlsx" --models kimi-k2-6 --out-dir evaluation-output/phia_all --csv evaluation-output/phia_all_results.csv --retry-failed
  ```
  다시 물은 답은 원래 trace의 같은 위치에 들어가고, 채점 CSV가 새로 만들어집니다.
- 채점만 다시 하기:
  ```bash
  python grade_phia_traces.py "Objective_Query_-_PHIA_v3.xlsx" "evaluation-output/phia_all/**/*-r*.json" --output evaluation-output/phia_all_results.csv
  ```

### 3.5 걸리는 시간
- 질문 하나는 보통 15~60초 걸립니다(계획 수립 → 메타데이터 조회 → KQL 생성 → 실행).
- 한 모델 4,000문항을 프로세스 1개로 돌리면 대략 하루가 걸립니다. 모델 7개를 `--workers 7`로 동시에 돌려도 하루 남짓 걸립니다.
- 진행 중에는 `[완료/전체 | ETA 남은시간 h]` 줄이 출력됩니다.

---

## 4. 결과 보기

### 4.1 폴더 구조
```
evaluation-output/phia_all/
  r1/u41/kimi-k2-6/chunk000-r1.json   ← 질문별 plan, KQL, 결과 행, 답, 시간
  r1/u41/kimi-k2-6/chunk000-r1.log    ← 실행 로그 (질문·답·오류가 실시간으로 남음)
  r1/u41/kimi-k2-6/label000-r1.json   ← 정답이 숫자가 아닌 문항(57개)은 label 모드로 따로 실행
evaluation-output/phia_all_results.csv ← 채점 결과
```

### 4.2 CSV 열
| 열 | 의미 |
|---|---|
| Repeat, User ID, Question, Question Type | 반복 번호, 사용자, 질문, 질문유형(Summary-only / Exercise-only / Cross) |
| Gold Answer, Retrieved Value | 정답, 모델이 조회한 값 |
| Correct (o/x) | 정답과의 차이가 0.01 이내면 o(정답이 소수 둘째 자리로 반올림돼 있어 마지막 자리 차이는 정답으로 봄). 속도 문항은 m/s → m/min(×60) 환산 후 비교 |
| LLM Model, Generated KQL | 모델 이름, 실행된 KQL |
| Failure Reason | 오답 사유 (Value mismatch, Empty result but gold has a value, Execution error 등) |
| Total Time (s) | 질문 하나 전체 시간 |
| **LLM Inference Time (s)** | **순수 추론 시간**: 각 LLM 호출의 서버 시간 − 그 모델의 서버 기준값(아래), 모든 호출의 합 |
| LLM Wait Time (s) | 네트워크·서버 대기: 연결 시간(DNS·TCP·TLS·요청 전송, 호출마다 측정) + 서버 기준값 + 재시도 대기 |
| ADX Time (s) | 모든 ADX 조회 시간의 합 (메타데이터 조회 + 쿼리 실행) |
| Output Tokens | 모델이 생성한 토큰 수의 합. 모든 모델에서 숨은 추론 토큰을 포함합니다(grok처럼 추론 토큰을 따로 세는 API는 전체 토큰 − 입력 토큰으로 계산) |
| Reasoning Tokens | 그중 숨은 추론 토큰. API가 따로 알려 주는 모델(GPT, grok, DeepSeek)만 채워지고, Claude·kimi·llama는 빈칸입니다 |
| LLM Calls | LLM 호출 횟수 (repair가 생기면 늘어남) |

질문 하나의 시간은 대략 `Total ≈ LLM Inference + LLM Wait + ADX + 코드 처리`로 나뉩니다. 코드 처리에는 매 호출마다 HTTP 클라이언트를 새로 만드는 시간(이 PC에서 약 0.7~1초)도 들어갑니다.

**측정 방식:** LLM 호출 하나를 두 구간으로 나눠 잽니다.
- **연결 시간:** 요청 시작부터 요청 전송 완료까지(DNS·TCP·TLS 연결 + 전송). 이 PC에서는 가끔 연결에 10초 안팎이 걸리는 경우가 있는데, 모두 여기로 분리됩니다.
- **서버 시간:** 요청 전송 완료부터 응답 수신 완료까지.

**서버 기준값:** 각 청크 프로세스가 시작할 때, 그 모델에 아주 짧은 요청("OK라고만 답해", 출력 16토큰)을 3번 보내 **서버 시간**의 중앙값을 잽니다(trace의 `llm_baseline_ms`). 모델이 거의 일하지 않는 요청이라 게이트웨이·대기열·요청 처리 같은 서버 고정 비용에 해당합니다. 그래서 순수 추론 시간은 "서버 시간 − 서버 기준값"입니다.

일부 모델(예: Azure의 grok)은 응답을 끝까지 만든 뒤 한꺼번에 보내서 토큰 스트리밍으로는 생성 시간을 잴 수 없습니다. 이 방식은 모든 모델에 같은 기준으로 적용됩니다.

CSV에 넣지 않은 세부 값은 trace JSON의 `timings`에 남습니다.
- 단계별 시간: `plan_ms`, `query_generation_ms`, `schema_inventory_ms`, `guard_ms`, `repair_ms`, `adx_execution_ms`
- 단계별 순수 추론 시간·출력 토큰: `plan_llm_inference_ms`, `query_generation_llm_inference_ms`, `repair_llm_inference_ms`, `plan_llm_output_tokens` 등
- 입력 토큰(`llm_input_tokens`), 연결 시간(`llm_network_ms`), 서버 시간(`llm_server_ms`), 서버 기준값(`llm_baseline_ms`), 재시도 횟수·대기(`llm_attempts`, `llm_retry_wait_ms`)
- 참고용 스트리밍 측정값(스트리밍하는 모델만 의미 있음): 응답 헤더까지(`llm_headers_ms`), 첫 출력까지(`llm_first_output_ms`), 첫 출력~마지막 토큰(`llm_generation_ms`)

### 4.3 모델별 요약표
```bash
python summarize_phia_results.py evaluation-output/phia_all_results.csv --traces "evaluation-output/phia_all/**/*-r*.json"
```
- 모델당 한 줄로 다음을 보여 줍니다. 결과는 `..._summary.csv`와 `..._summary.md`로 저장됩니다.
  - 정답률(전체, 카테고리별)
  - 시간(전체, 순수 추론, LLM 대기, ADX)마다 **중앙값과 평균** 두 열. 중앙값은 가끔 튀는 네트워크 지연·재시도에 흔들리지 않는 값, 평균은 그런 경우까지 포함한 값입니다.
  - 출력 토큰 평균, 추론 토큰 평균, LLM 호출 수 평균
  - 추론 처리 속도(출력 tokens/s, 전체 합 기준) = 모든 문항의 출력 토큰 합 ÷ 순수 추론 시간 합 (순수 추론 시간이 0인 문항 제외). 문항별 속도의 평균이 아니라 가중 평균이라, 아주 짧은 문항 하나에 값이 튀지 않습니다.
  - 서버 기준값 중앙값(s, 호출당): 모델별 서버 고정 비용
  - 모든 열 이름에 중앙값/평균/전체 합 기준을 적어 두었습니다.
- 그룹(운영 기준선 / 비교 쌍 / 참고)은 3.2 표와 같습니다.

### 4.4 모델·카테고리별 정답률 빠르게 보기

```bash
python -c "
import csv, collections
rows = list(csv.DictReader(open('evaluation-output/phia_all_results.csv', encoding='utf-8-sig')))
t = collections.defaultdict(lambda: [0, 0])
for r in rows:
    for k in ((r['LLM Model'], 'All'), (r['LLM Model'], r['Question Type'])):
        t[k][1] += 1; t[k][0] += r['Correct (o/x)'] == 'o'
for (m, c), (ok, n) in sorted(t.items()):
    print(f'{m:26s} {c:14s} {ok:5d}/{n:5d} = {ok / n:.1%}')
"
```

### 4.5 해석·채점 기준 (팀 합의, 2026-10-21)
챗봇과 채점기가 모두 아래 기준을 따릅니다. 하영 학생 GT README의 기준과 같습니다.

| 항목 | 기준 | 적용 위치 |
|---|---|---|
| 조회 결과가 없을 때 | 값 없음(NULL). 단 "몇 번/며칠" 같은 개수 질문은 0 | 쿼리 생성 프롬프트(`generate_sensor_query.txt` 7번 항목) |
| "past week", "last week", "previous week", "지난주" | 달력상 지난주(일요일~토요일). 최근 7일이 아님 | 계획 프롬프트(`plan_sensor_query.txt` 기간 표현) |
| "past/last N days" | 오늘을 뺀 직전 N일 | 계획 프롬프트 |
| 숫자 비교 허용 오차 | 0.01 | `grade_phia_traces.py --tolerance` 기본값 |

기록 종류는 고정된 규칙으로 정하지 않습니다. 계획의 지표 이름(예: `heart_rate`)은 묻는 양을 가리킬 뿐이고, 쿼리 단계가 그 사용자의 실제 기록 목록을 보고 고릅니다. 예를 들어 하루 1개짜리 안정시 심박만 있는 데이터에서는 그 기록으로 답합니다.

쿼리가 결과 없이 끝났고, 그 쿼리가 고른 기록 종류(Type)나 운동 이름이 **전부** 그 사용자의 그 기간 기록 목록에 없으면, 쿼리 생성 단계를 한 번만 다시 실행합니다(`chatbot/policies/evidence_feedback.py`). 이때 모델에게는 "고른 X는 이 기간에 기록이 없고, 실제로 있는 것은 [...]"라는 사실과 이전 쿼리만 전달합니다. 어떤 기록으로 바꿀지, 아니면 빈 결과(NULL, 개수는 0)를 그대로 둘지는 모델이 판단합니다. 지표별 대체 규칙은 없습니다. 이 과정은 trace의 `kql_generation_trace.evidence_feedback`에 남습니다.

결과를 내는 문장이 둘 이상인 쿼리(예: `... | as Days; 두 번째 쿼리`)는 ADX가 표를 여러 개 돌려주고 그중 첫 표만 읽히므로, 가드가 실행 전에 repair로 보냅니다. 수정 방향(`let`으로 묶고 마지막에 쿼리 하나)만 알려 주고 계산 내용은 바꾸지 않습니다.

---

## 5. PHIA D1 평가 규칙 (`run_phia_all.py`에만 있음)

챗봇 코드와 프롬프트에는 평가 전용 분기가 없습니다. 아래 규칙은 평가를 실행할 때 각 청크 프로세스 안에서만 적용됩니다. 실제로는 `run_phia_all.py --phia-d1-child ...` 형태로 자기 자신을 호출하는 구조입니다.

- **D1 경로 고정** (`--routing d1`): 모든 문항을 도메인 라우팅 LLM 호출 없이 D1 데이터 경로로 보냅니다.
- **평가 기준 시각**: 각 사용자 데이터의 마지막 날짜를 "오늘"로 씁니다(`--clock referenced-data`). 이날은 이미 끝난 날이라는 사실을 planner에게 맥락으로 알려 줍니다. 예를 들어 오늘 저녁에 시작한 수면 기록도 이미 있습니다.
- **되묻기 생략**: 벤치마크 질문은 되물을 수 없으므로, plan이 확인 질문만 남긴 경우 그 해석대로 진행합니다.
- **KQL repair 2회**: 챗봇 기본값은 1회입니다.
- **속도 단위**: 기록값은 m/s로, 질문 속 단위 없는 속도는 m/min으로 해석합니다.

---

## 6. 알아 두면 좋은 점

- **동시 실행 수**: 여러 명이 같은 ADX를 함께 쓰므로, 전체 프로세스를 대략 8개 이하로 유지하세요. 너무 많으면 ADX가 429(요청 과다)를 반환합니다. 클라이언트가 자동 재시도는 하지만 느려집니다.
- **멈춘 청크**: 로그가 20분 동안 늘지 않으면 자동으로 멈추고 `STALLED`로 표시됩니다. 같은 명령을 다시 실행하면 재시도됩니다.
- **모델별 특성**:
  - llama-4-maverick: 가끔 같은 토큰을 반복하는 출력을 냅니다. 감지되면 한 번 자동으로 재시도합니다. KQL 문법 실수가 다른 모델보다 잦습니다.
  - grok-4-1-fast-reasoning: KQL을 중간에 끊어 보내는 경우가 있어, guard가 거부하거나 실행 오류가 날 수 있습니다.
- **Azure 콘텐츠 필터**: 일부 질문은 Azure 쪽 콘텐츠 필터에 막혀 오류로 기록될 수 있습니다. `--retry-failed`로 다시 물어도 같은 결과면 필터 문제입니다.
- **gold 관련**: 일부 정답은 GT 정답 프로그램이 질문과 다른 것을 계산한 오류입니다. 예를 들어 "가장 긴 달리기의 날짜"의 정답이 숫자이거나, 질문의 기간을 무시하거나, 데이터에 없는 운동 이름(`Bike`, `Workout`)으로 찾는 경우가 있습니다. 강한 모델의 남은 오답 대부분이 여기에 해당합니다. 문항별 정리는 `evaluation-output/phia_1019_analysis/`의 GT 검토 파일을 보세요.

---

## 7. 문제 해결

| 증상 | 해결 |
|---|---|
| `ModuleNotFoundError: No module named 'openpyxl'` | 가상환경이 켜져 있지 않습니다. `source .venv/Scripts/activate` 후 다시 실행하세요. |
| `CERTIFICATE_VERIFY_FAILED` | `export SSL_CERT_FILE=$(python -c "import certifi;print(certifi.where())") REQUESTS_CA_BUNDLE=$(python -c "import certifi;print(certifi.where())")` |
| ADX 인증 오류 (`DefaultAzureCredential failed`) | `az login` 후 ADX 읽기 권한이 있는 구독을 선택하세요. |
| `Set AZURE_OPENAI_API_KEY in your private local.settings.json` / HTTP 401 | `local.settings.json`의 `AZURE_OPENAI_API_KEY`를 확인하세요. |
| `Unknown model` | `local_models.json`에 있는 이름을 정확히 쓰세요. |
| 한 청크만 계속 실패 | 그 청크의 `.log` 파일 마지막 부분을 확인하세요. 같은 명령을 다시 실행하면 그 청크만 다시 돕니다. |
| 테스트 실행 | `python -m unittest discover -s tests -t .` (6개는 원래 코드부터 실패하던 테스트입니다.) |
