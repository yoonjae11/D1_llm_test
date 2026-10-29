# Sensei 로컬 다중 LLM 테스트

이 문서는 `azure_function_main_app/`의 로컬 테스트를 위한 모델 목록, 환경 설정, 실행 방법과 코드·프롬프트 점검 경로를 안내한다.

## 1. 현재 코드에서 선택할 수 있는 LLM

`local_models.json`에 등록된 정확한 선택 이름은 다음과 같다. Azure deployment 이름이므로 별칭을 임의로 바꾸지 않는다.

| `--model` 이름 | 공급자 | 전송 API |
|---|---|---|
| `gpt-5` | OpenAI | `/openai/v1/responses` — 기본값 |
| `gpt-6-1-sol` | OpenAI | `/openai/v1/responses` |
| `claude-opus-5-5` | Anthropic | `/anthropic/v1/messages` |
| `deepseek-v4-pro` | DeepSeek | `/openai/v1/chat/completions` |
| `llama-4-maverick` | Meta | `/openai/v1/chat/completions` |
| `grok-4-1-fast-reasoning` | xAI | `/openai/v1/chat/completions` |
| `deepseek-v4-flash` | DeepSeek | `/openai/v1/chat/completions` |
| `kimi-k2-6` | Moonshot AI | `/openai/v1/chat/completions` |

현재 registry의 resource base는 `https://your-resource.services.ai.azure.com`이다. `chatbot/local_models.py`가 API 종류에 맞는 경로를 붙인다. 모든 모델은 `AZURE_OPENAI_API_KEY`라는 **환경변수 이름**을 참조한다. 실제 키 값은 registry나 README에 쓰지 않는다.

위 목록은 **코드에 설정되어 선택 가능한 목록**이지, 각 배포가 지금 정상 응답한다는 실시간 보장은 아니다. 최근 40문항 실제 평가는 `gpt-5`로 실행했다. 다른 모델은 연구원 환경에서 각각 한 문항으로 endpoint·deployment·할당량·인증을 먼저 확인한다. 모델 실패를 다른 모델로 조용히 대체하지 않는다.

선택한 모델은 로컬 요청의 계획·KQL 생성·LLM 검토/수정·최종 자연어 생성에 적용된다. 모든 모델에 같은 의미의 공통 프롬프트를 사용하되 공급자별 전송 형식은 다르다. 코드 가드, ADX 연산, 모델 호출 없는 D3/D4 단계가 LLM으로 바뀌는 것은 아니다. `gpt-5` registry에는 `reasoning_effort: low`가 있고 나머지는 명시하지 않았으므로, 실험에 이 설정 차이도 기록한다.

## 2. 환경 설정과 ADX 조회 계약

비공개 `local.settings.json`의 `Values` 또는 `.env`에서 다음을 설정한다.

| 변수 | 설명 |
|---|---|
| `AZURE_OPENAI_API_KEY` | registry의 모델 endpoint용 실제 공통 키 |
| `AZURE_OPENAI_BASE_URL` | 기본 클라이언트 설정. 기존 정상 값 또는 해당 resource의 `/openai/v1/` URL |
| `AZURE_OPENAI_DEFAULT_DEPLOYMENT` | `gpt-5` |
| `AZURE_OPENAI_AUTH_MODE` | 키 기반 로컬 실행은 `api_key` |
| `ADX_QUERY_URI` 또는 `ADX_CLUSTER_URI` | 실제 ADX query cluster URI. ingest URI가 아님 |
| `ADX_DATABASE` | 데이터를 적재한 실제 DB 이름 |
| `KQL_GENERATE_METHOD` | `llm` — 이 로컬 CLI도 이를 강제로 설정함 |

로컬 ADX 인증은 `DefaultAzureCredential`을 사용하므로 `az login`한 계정의 DB 조회 권한을 확인한다. 같은 로그인 성공이라도 다른 tenant/DB이면 실패할 수 있다. 인증서 오류는 조직 CA/정상 TLS 설정으로 해결하고 인증서 검증을 꺼서 공유하지 않는다.

환경 우선순위: 이미 설정된 프로세스 환경변수 → `local.settings.json` → `.env.local` → `.env`. 설정 파일을 바꿔도 셸에 남은 환경변수가 우선할 수 있다. `--isolated`는 profile/conversation/inventory 저장을 프로세스 메모리로 바꾸지만 **ADX와 LLM은 실제 서비스**다.

DB에는 공통 `SensorActivityDaily`, `SensorActivityHourly`, `SensorActivityInterval`, `SensorHealthSamples`, `SensorSleepNights`, `SensorWorkoutSessions` 조회 함수가 있어야 한다. 정의 참고는 저장소의 `azure_adx_kql/11_sensor_query_contract_production.kql`이다. 테스트 때문에 테이블을 삭제하거나 KQL 배포 파일을 일괄 실행하지 않는다. 함수/컬럼 문제는 관리자에게 기존 정의를 확인받는다.

## 3. 현재 실행 가능한 로컬 CLI

```bash
python local_llm_test.py --list-models

# 대화형: UserId 생략 시 입력을 요청하며 엔터 기본값은 kist008
# Custom Logger 데이터는 실제 현재 시간 사용
python local_llm_test.py --model gpt-5 --clock current --isolated

# PHIA: 질문이 참조하는 summary/workout 데이터의 마지막 논리 날짜 사용
python local_llm_test.py --user-id 333 --model gpt-5 --clock referenced-data --isolated
```

대화형 종료는 `/exit`, `/quit`, EOF 또는 Ctrl+C다. 독립 평가에는 `--isolated --fresh-thread`를 함께 사용한다. 일반 대화 평가에서는 이전 문맥을 유지할지 실험 설계로 결정한다.

```bash
# 여러 질문을 입력 순서대로 실행. 아래 질문은 단순 실행 예시이며 정답 fixture가 아님.
python local_llm_test.py --user-id 333 --model gpt-5 --isolated --fresh-thread \
  --clock referenced-data \
  --question "How many steps did I take yesterday?" \
  --question "What was my resting heart rate yesterday?" \
  --trace-output evaluation-output/smoke-gpt5.json

# 동일 질문들을 선택한 모델별로 실행
python local_llm_test.py --user-id 333 --models gpt-5 claude-opus-5-5 deepseek-v4-pro \
  --clock referenced-data --question "How many steps did I take yesterday?" \
  --trace-output evaluation-output/model-comparison.json

# 8개 설정 모델 전체: 실제 유료 호출이므로 1문항 smoke부터 진행
python local_llm_test.py --user-id 333 --all-models --clock referenced-data \
  --question "How many steps did I take yesterday?" \
  --trace-output evaluation-output/all-models-smoke.json
```

다중 모델 옵션은 자동으로 isolated/fresh-thread를 적용한다. 실행은 모델별로 질문 순서대로 하며, 반복 측정은 같은 명령을 다시 실행하고 output 이름에 repeat 번호를 붙인다. CLI에는 **질문 파일 로더, `--repeat`, 자동 정오 판정, 자연어 생성 전 종료 옵션은 아직 없다**. 존재하지 않는 옵션을 사용하지 않는다.

비대화형은 지정 질문을 다 처리한 뒤 종료한다. 기록된 실행 error가 있으면 보통 종료코드 1, 없으면 0이다. **종료코드 0은 정답 판정이 아니며**, clarification·데이터 부족 여부도 trace에서 확인해야 한다. 설정/인증 시작 실패는 trace 저장 전 종료할 수 있으므로 프로세스 로그·종료코드도 별도로 보존한다.

## 4. 코드·프롬프트 점검과 검증

| 점검 대상 | 이 폴더 기준 상대 경로 |
|---|---|
| 공통 D1 schema | `chatbot/prompts/d1_sensor_data/adx_schema_context.txt` |
| 의미 계획·형식화된 연산 | `chatbot/prompts/d1_sensor_data/plan_sensor_query.txt` |
| 조건의 측정 범위 재검토 | `chatbot/prompts/d1_sensor_data/review_measurement_scope.txt` |
| KQL 생성·수정·재조회 | `chatbot/prompts/d1_sensor_data/{generate_sensor_query,repair_sensor_query,recheck_daily_with_flat}.txt` |
| 최종 자연어 생성 | `chatbot/prompts/common/generate_final_response/sensor_data.txt` |
| 전체 graph / D1 subgraph | `chatbot/graph/workflow.py` / `chatbot/nodes/d1_sensor_data/workflow.py` |
| 읽기 전용/user/시간 가드 | `chatbot/policies/kql_safety.py` |
| 결과 후처리 | `chatbot/policies/result_processing.py` |
| 한글 프롬프트 검토본 | `docs/D1_PROMPTS_REVIEW_KR.md` |

prompt loader는 프로세스 내에서 cache하므로 수정 후 Python 프로세스를 재시작한다. `tests/`는 회귀 검증 소스이며 운영 프롬프트나 PHIA 별도 경로가 아니다.

```bash
python local_llm_test.py --help
python local_llm_test.py --list-models
python -m unittest discover -s tests
```

모델의 정오 평가와 별도로 동작·안전성 회귀 검증을 진행한다. [OpenAI evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)도 참고해 객관적인 조회값 비교와 사람의 reference/의미 검토를 분리하고 개발 질문만으로 일반화를 주장하지 않는다.
