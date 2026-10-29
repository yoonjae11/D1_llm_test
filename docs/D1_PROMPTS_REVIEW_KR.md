# 현재 D1 프롬프트 한글 검토본

2026-10-11 기준 `chatbot/prompts/`의 실제 공통 reactive 데이터 조회 프롬프트에 대응하는 한글 검토본이다. 별도 운영 프롬프트가 아니며 loader가 이 파일을 읽지 않는다. 원문을 새 규칙으로 고치지 않고, 반복 설명을 통합하고 JSON/KQL 식별자는 유지해 번역했다. 문법 코드와 전체 컬럼 목록의 정확한 원문은 각 `.txt` 파일을 함께 확인한다. 채팅에 제공하는 번역도 이 범위다. D2 코칭·proactive·로그인/알림 설정 프롬프트 전체의 번역이라고 주장하지 않는다.

## 1. 도메인 실행 계획 — planning/plan_domain_execution.txt

개인 건강 도우미의 이번 요청에 필요한 작은 도메인 실행 계획을 선택한다. 대화 맥락으로 현재 목표를 판단하며 문장 길이만으로 분류하지 않는다. 이전 주제보다 현재 요청을 우선하고, 생략된 후속 질문은 최근 대화와 reply 대상에 근거해 해석한다.

- data: 개인 측정값, 수면/활동 시점, 추세, 비교, 데이터 최신성, 기록을 이용한 설명. 측정하지 않은 증상만 말했다고 조회가 필요한 것은 아니다.
- chat: 기록 조회가 필요 없는 일반 대화·건강 지식·코칭.
- capabilities: 도우미 정체성, 데이터 종류, profile/설정.
- unsupported: 수집하지 않는 개인 영양·수분·약물·검사·영상·DNA 기록. 연결 기능을 약속하지 않는다.
- clarification: 건강 영역/지시 대상을 해결할 수 없는 요청.
- alert_config: 임계치 알림과 예정 요약의 생성·변경·삭제·조회.

지표·기간·연산이 식별되면 표현이 서툴러도 데이터 요청이다. 여러 건강 영역을 결합하거나 기록된 비율의 합처럼 특이한 연산이라고 임의로 거절하지 않는다. 가용성은 D1이 조사한다.

D1은 센서 근거와 쿼리 추론, D2는 행동 변화 코칭 전략, D3는 건강 지식·안전 근거, D4는 현재 대화의 지시 대상 해결이다. 단순 사실 조회는 D1만 선택한다. 사실 질문에 목표 설정 코칭을 시작하지 않는다. 센서 기반 코칭은 D1→D2, 안전 근거가 필요한 코칭은 D3→D2, 미해결 대화 참조는 D4를 먼저 실행한다. D3는 진단·처방을 하지 않고 D4는 장기 기억/센서 조회가 아니다.

selected_domains, requirements, dependencies, execution_stages를 JSON으로 반환한다. 모든 도메인은 선택된 것만 한 번 실행하며 의존성은 이전 stage에 있어야 한다. cycle/self-dependency를 금지하고 최대 3stage를 사용한다. 필수 근거 실패는 종속 도메인을 차단하며 optional로 위장하지 않는다. 실제 오류/의존성 실패에만 early exit한다.

## 2. 질문 의미 계획 — d1_sensor_data/plan_sensor_query.txt

센서 질문을 의미 계획으로 변환하고 **JSON만 반환한다**. 제공된 local reference clock을 사용하며 날짜만 있는 평가 clock에 시각을 만들어 넣지 않는다. 질문과 관련 대화에서 지표·단위·기간·관측 단위·연산·조건·선택을 유지한다. 완전한 새 요청은 이전 계획을 대체하고 생략된 후속 요청만 빠진 정보를 이어받는다. 실제 계획을 막는 필수 모호성만 한 번 확인한다. 이 단계는 KQL·물리 테이블이 아니라 의미를 계획한다.

### 시간·수면·연산의 명시된 기본 해석

- 완료된 지난 N일/밤 집계는 보통 `[오늘-N, 오늘-1]`이다. 하지만 최근/마지막 세션 검색은 오늘까지 포함한다. 최근 기간으로 제한된 연대순 검색은 include_today=true, 완료 기간 집계는 false로 표현한다. 명시 제외·절대 날짜·비교 조건을 우선한다.
- rolling hours는 일별 달력 기간과 다르다. 지난주는 이전의 완전한 **일요일~토요일**, 지난달은 이전 달력 월이다. 상대 기간은 상대 형태로 유지해 요청 clock으로 해석한다.
- 수면 밤/수면 조건 날짜의 기본값은 `sleep_date_basis=night_day`: local 정오부터 다음 정오까지의 활동 밤이며 시작 날짜를 label로 쓴다. 수면 시작이 00~12시이면 이전 날짜의 밤, 저녁이면 그 날짜의 밤이다. 이것은 원본 보고일 복원이 아니라 **공통으로 선언한 날짜 규약**이다.
- 기상일을 명시하면 wake_day, 실제 잠든 civil date/낮잠을 명시하면 start_day다. 실제 onset/wake timestamp는 바꾸지 않는다. 이 수면 label을 운동 시각에 적용하지 않는다. 운동/일반 활동은 자신의 civil date를 유지한다.
- 현재 상태이고 기간이 없으면 오늘, 기간 없는 역사 집계는 bounded all_available다. 여러 날의 지표만 요청하면 임의 합/평균 대신 series다. 명시된 계산을 보존한다. `>`와 `>=`, `<`와 `<=`를 구분한다.
- 관측 단위는 달력 기간의 길이가 아니라 요청한 양으로 결정한다. 기록된 완전한 수면·운동 속성의 통계는 session 관측들에 대해 계산한다. 날짜가 같다고 여러 세션을 합쳐 새 통계 변수를 만들지 않는다. 날짜 총량을 요청한 경우만 일별 양을 먼저 만든다.
- 결측은 기록된 0과 다르다. 여러 날 평균에서 기록 없는 날짜를 0인 세션으로 넣거나 요청 날짜 수로 나누지 않는다. 조건 근거의 시작/끝 범위 전체가 아니라 정확히 조건을 만족한 관측/날짜를 선택한다.
- 비교에는 주 기간과 기준 기간을 따로 유지한다. 매일 같은 시각까지 비교하면 같은 시각 창을 각 날짜에 적용한다. 일별 총량의 평균은 센서 행들의 평균과 다르다. 추세는 날짜별 series를 유지한다.

### 논리 리소스와 측정 범위

activity.daily는 일별 누적 활동, activity.hourly는 시각 창 활동, activity.interval은 구간 근거, health.samples는 생리/일별 관측, sleep.nightly는 수면, workout.sessions는 운동 세션이다. 논리 이름은 의미/관측 단위이며 항상 그 물리 계층을 조회해야 한다는 지시가 아니다.

canonical metric 이름을 자연어 키워드와 일대일로 연결하지 않는다. 일상 단어 calories/heart rate만으로 resting/basal/all-day stream을 만들어 넣지 않는다. `calories`는 독립 active energy, `total_calories`는 basal을 포함하는 양, `workout_calories`는 세션 기록 에너지다. `heart_rate`는 독립 표본 생리, `phone_resting_heart_rate`는 resting, `rmssd`와 `sdnn1`도 다른 측정이다. `total_sleep_time`은 문서화된 기록 수면량이며 in-bed/asleep과 다르다.

운동 기록에 관한 질문의 한정되지 않은 속성 조건은 독립 범위를 명시하지 않은 한 그 session 관측에 속하는 것으로 해석하고 가정을 공개한다. 날짜 조건 문구만으로 all-day 생리나 에너지 관측을 새로 만들지 않는다. 독립 일별/휴식/기초대사/시각별 관측을 명시했다면 가용하지 않아도 그 의미를 유지한다. 이 **기본 해석 자체는 프롬프트의 명시적 규약**이며 schema에 있는 필드만으로 보편적으로 입증되는 사실은 아니다.

관측 자체에 붙은 평균/총량/비율과 관측들 사이의 바깥 집계를 구분한다. 세션 평균 심박의 합을 명시하면 그대로 합하고 의미를 설명한다. 표본이 우연히 같다고 다른 연산으로 대체하지 않는다. 운동 이름은 사용자 표현을 유지하고 query 단계에서 관측 label과 의미를 맞춘다. 세션 수와 distinct 날짜 수를 구분하며 first/last에서 선택된 optional 값이 null이라고 다른 세션으로 건너뛰지 않는다.

운동 날짜가 다른 stream 값을 찾는 키라면 먼저 식별 stream에서 날짜를 선택한 뒤 그 날짜의 목표값을 조회한다. 목표값이 결측이라고 더 오래된 날짜로 바꾸지 않는다. 최근 검색에 기간이 없으면 bounded 전체 이력이며 미리 정한 짧은 window를 만들지 않는다.

### 출력 인터페이스와 few-shot 조각

Version1은 최신 표본·간단한 일별 값·시각 창·overview다. Version2는 계산·시계열·조건·횟수·선택을 지원한다. computation 안에 entity, outputs, filters, conditions, selector를 둔다. entity는 day/night/sleep_session/workout_session/sample/hour다. aggregation은 none/sum/mean/min/max/median/stddev_sample/stddev_population/count/count_distinct다. **한정되지 않은 표준편차의 기본값은 표본 표준편차**다.

condition의 단위는 명시 단위를 유지하며 unstated는 native다. Output은 metric 또는 entity 중 하나와 aggregation만 가진다. first/last/top/bottom 선택을 지원한다. absolute 날짜 end_date는 포함, 실제 쿼리 경계는 exclusive-end로 해석한다. 날짜만 있는 clock에서 현재 시각까지를 요청하면 시각을 확인한다. 이 형식은 지원 기능의 typed interface이며 질문-template lookup은 아니다.

원문에는 다음 **합성 JSON 문법 조각**이 있다. 완전한 평가 질문·정답·특정 사용자 데이터는 없다.

1. workout_session에서 첫 3개의 workout_speed를 그대로 출력하는 selector 조각.
2. day에서 respiration_rate≥16(native)인 관측의 distance 중앙값 조각.
3. workout_duration>17분인 세션의 workout_steps 중앙값 조각.
4. Rowing 세션의 workout_elevation_gain 평균을 별도 rmssd<23ms의 날짜 근거로 선택하는 조각.

숫자·지표·운동명·선택을 다른 질문으로 상속하지 말고 실제 요청에서 다시 조합하라고 지시한다. 입력에는 질문/대화/이전 계획/지원 지표/관측 metadata/검증 feedback을 제공한다. 정답표는 제공하지 않는다.

## 3. 측정 범위 독립 검토 — review_measurement_scope.txt

원래 질문을 관측 정의로 다시 해석해 computation/assumptions/needs_followup/followup_question의 JSON patch만 반환한다. KQL이나 날짜·리소스 목록을 새로 반환하지 않는다. session 속성과 독립 daily/resting/basal/sample 범위를 구분하고, 날짜 membership을 측정 범위와 혼동하지 않는다. 미세한 timestamp가 없다고 미세한 관측을 만들어 내지 않는다.

원래 조건·단위·연산·선택·기간을 보존한다. `semantic_contract.sleep_date_basis`를 수면 eligibility와 다른 stream과의 날짜 matching에 **같게 적용**한다. 다른 stream은 civil date를 유지한다. 잠에서 깬 뒤 운동 같은 별도 관계는 질문에 명시되어야 한다. IngestionTime은 같은 기록의 최신 사본을 선택할 metadata이지 event/report date가 아니다.

원문에는 cycling 세션 duration 조건과 별도 daily walking total 조건의 대비 설명, 그리고 duration>17분 세션 steps 중앙값의 합성 JSON 조각이 있다. 이는 범위/인터페이스 예시이며 정답 fixture가 아니다.

## 4. 공통 ADX schema — adx_schema_context.txt

모든 사용자는 6개 승인된 Sensor 함수의 같은 계약을 사용한다. 함수 인자는 `(TargetUserId, StartLocal, EndLocal)`이며 유한한 local event-time 범위 `[StartLocal, EndLocal)`다. 함수 안의 storage/parsing은 query model 소관이 아니다.

| 함수 | 관측 정의/주요 필드 |
|---|---|
| SensorActivityDaily | source/구간 배분을 처리한 daily activity. LocalDate, Type, Unit, ValueSumExact, interval 기여 수 |
| SensorActivityHourly | hour bucket 배분. HourStartLocal과 시간 경계 crossing 정보. 결측 일 채우기 용도가 아님 |
| SensorActivityInterval | DateFrom/DateTo, ValueNumber, LogicalEventKey 등 구간 근거 |
| SensorHealthSamples | query-facing Flat. 미리 daily/hourly 집계하지 않고 **중복 제거하지 않음** |
| SensorSleepNights | 정규화된 sleep session. OnsetLocal/WakeLocal, SleepDay, duration/stages/percentages/근거 flags |
| SensorWorkoutSessions | 정규화된 workout session. WorkoutId, StartLocal/EndLocal, ActivityName/Code, optional 지표·단위 |

Flat 공통 필드: UserId, DateFrom, DateTo, IngestionTime, RecordingMethod, SourceDeviceId, SourceId, SourceName, SourcePlatform, Type, Unit, Uuid, Value, ValueNumber, ValueType.

- DateFrom/DateTo는 관측 시작/끝, 업로드 시각이 아니다. point/구간 증가량/누적 snapshot/이미 집계된 daily·session은 집계 방법이 다르다. 함수가 local wall-clock을 제공하므로 UTC로 간주해 재변환하지 않는다.
- IngestionTime은 같은 기록의 최신 사본을 고르는 적재/payload metadata다. raw→flat은 CreatedAt에서 가져오며 import는 역사 값을 줄 수 있다. Kusto ingestion_time()과 같지 않다.
- Uuid는 사람/날짜가 아니라 기록 identity다. 같은 UUID가 다른 구간에 재사용될 수 있다. Type은 측정, Unit은 단위, ValueNumber는 유효한 0을 포함한 수치, ValueType은 표현, Value는 구조화 payload다.
- 비어 있지 않은 Uuid 중복키는 UserId+Type+Unit+Uuid+DateFrom+DateTo이며 arg_max(IngestionTime,*)를 선택한다. mutable source label과 ValueNumber를 키에 넣지 않는다. UUID만/날짜만으로 dedup하지 않는다. UUID 없으면 전체 source/type/unit/time identity를 고려하되 충돌은 불명확하게 남긴다.
- UUID dedup은 phone/watch의 겹친 source reconciliation이 아니다. 여러 UUID/source의 overlapping 값을 무조건 합하지 않는다. 기록창 중복 metadata는 겹침/모호성 신호이지 독립 증가량의 증거가 아니다.
- SLEEP_SESSION은 기록된 수면 session량, SLEEP_ASLEEP은 명시된 asleep량, SLEEP_IN_BED는 침대 시간이다. 한정 없는 수면시간은 **기록된 수면량**을 보존하고 awake를 임의 차감하지 않는다. stage 기록의 전체 관측창이 그 stage의 실제 길이가 아닐 수 있으므로 timestamp 차이로 ValueNumber를 대체하지 않는다.
- stage/session/percentage 통계는 요청된 관측 단위를 유지한다. 완전한 여러 session을 같은 날짜라고 합치지 않는다. 실제 stage segment이면 session별 합성이 필요하지만 이미 완전한 stage량이면 numeric 값을 쓴다. 기록 percentage는 저장된 비율이지 새 denominator로 만든 비율이 아니다.
- night_day는 `startofday(DateFrom - 12h)` 또는 정규화 onset으로 label을 만들고, wake_day/start_day는 각각 기상/시작 civil date다. 정규화 SleepDay는 wake-date key이므로 무조건 night_day로 대체하거나 1일 빼지 않는다. sleep만 label을 바꾸고 workout date는 바꾸지 않는다.
- 구버전 nightly의 zero stage/default flags는 실제 zero sleep의 증거가 아닐 수 있다. unknown flag는 false가 아니고 numeric SLEEP_SESSION 근거를 지우지 않는다.

### 운동 JSON 및 단위

WORKOUT은 scalar 증가량이 아닌 structured session이다. outer Unit=NO_UNIT 또는 ValueNumber=null이어도 내부 측정은 있을 수 있다. 알려진 adapter는 workoutActivityType→ActivityName, durationMinutes→DurationMin, averageHeartRate→AverageHeartRateBpm, totalDistance/Unit→Distance/Unit, totalEnergyBurned/Unit→CaloriesKcal, totalSteps→Steps, activeZoneMinutes→ActiveZoneMinutes, elevationGain, speed를 처리한다. 기록된 소수 duration을 timestamp 해상도 때문에 버리지 않는다. elapsed와 active duration은 다를 수 있다.

각 속성의 명시 단위 또는 검증된 producer 계약을 사용한다. 구버전 adapter의 고정 SpeedUnit(일부 meters/minute label)은 원본 단위의 증거가 아니다. 호출자가 선언한 `producer_measurement_contracts`는 해당 field의 단위만 명시하며 **기록 숫자를 바꾸지 않는다**. 실제 payload unit과 충돌하면 알린다. 단위가 없으면 unknown을 유지하며 정상적인 사람 속도로 보이도록 변환하지 않는다. 기록된 0을 파생 nonzero로 대체하지 않는다. 거리/시간 파생량은 독립 계산으로 명시하고 기록된 speed와 구분한다.

다른 wearable의 JSON 경로는 bounded inventory의 literal path/type/presence/unit/activity metadata를 확인한다. nested JSON 문자열은 다시 parse_json, array는 의도적으로 확장하고 session identity로 재집계한다. 표본/깊이 제한에서 관측되지 않은 field가 전체에 없다고 확정하지 않는다. 필요하면 SensorHealthSamples의 Value를 parse해 query-local `let Workouts`를 만들며 물리 table은 쓰지 않는다. Value_1 같은 import 전용 column은 query-facing contract가 아니므로 compatibility는 backend에서 Value로 제공해야 한다.

activity label은 관측된 이름과 의미를 맞추고 case-insensitive 정확 membership을 쓴다. 대문자/정규식 정리는 ontology나 동의어 추론이 아니다. 파싱 실패는 0회 운동이라는 증거가 아니다.

### Reactive 집계와 부분 구간

reactive 답은 보통 SensorHealthSamples를 직접 집계한다. daily/hourly는 답변 단위이지 필수 storage 경로가 아니다. normalized source/overlap 해결이나 완전하고 동등한 rollup이 필요할 때 해당 함수를 사용한다. daily를 택했는데 flat에 있는 metric/day가 빠졌으면 근거를 확인하고 같은 계획을 재조회한다. 같은 키의 daily와 flat을 더하지 않는다.

호환되는 겹치지 않는 증가 구간만 가산한다. running-total snapshot은 같은 누적의 이전 snapshot을 대체하는 값이지 더할 증가량이 아니다. 하루 총량과 그 상세 interval을 함께 합하지 않는다. 경계를 걸친 additive interval은 `ValueNumber × 겹친 시간 / 원래 구간 시간`으로 배분할 수 있지만 균등 증가를 가정한 **추정값**이며 IsEstimate를 남긴다. 실제 point·snapshot·심박·percentage·workout totals에 같은 배분을 하지 않는다. coarse 하루 총량은 정확한 subday 값을 제공하지 않는다. 정확값 요구라면 미해결 경계는 unavailable이다.

## 5. KQL 생성 — generate_sensor_query.txt

의미 계획과 공통 schema로 **읽기 전용 KQL 한 개만 반환**한다. 연산자는 직접 선택하고 실제 의미를 유지하는 가장 작은 query를 만든다. 단순 scalar는 보통 source·filter·aggregation 하나면 된다. 예시는 문법이며 질문 유형을 제한하는 template 목록이 아니다.

- 제공된 TargetUserId/StartLocal/EndLocal을 정확히 정의하고 승인 함수마다 사용한다. query_time_scope의 logical 기간과 cross-midnight scan padding을 구분한다. 수면은 먼저 올바른 label을 만든 뒤 logical 기간에 filter/join하고 non-sleep은 자신의 civil date로 filter한다. now()/예시/최신 event로 bound를 바꾸지 않는다.
- 지표의 documented 의미, 관측 Type·Unit·grain으로 source/column을 선택한다. 표면적인 단어나 nonnull 여부로 다른 측정을 대체하지 않는다. 계획을 먼저 관측 단위로 만들고 조건→선택→연산을 적용한다.
- 다른 stream 조건은 정확 equality key로 join한다. 날짜 membership과 측정 범위를 구분한다. 구간 inequality는 적절한 join 뒤 filter하며 on절에 쓰지 않는다. leftsemi는 existence join이고 semi는 유효한 KQL이 아니다. innerunique가 같은 날짜의 여러 왼쪽 session을 버리지 않도록 identity를 보존한다.
- day count는 distinct 날짜를 정확히 세며 dcount 근사를 쓰지 않는다. 이미 완전한 session numeric 통계 전에 추가 daily sum/mean/max를 넣지 않는다. 서로 다른 session과 중복 사본을 구분한다.
- 표본 표준편차는 `stdev`, 모집단은 `stdevp`다. stdevsamp는 없다. sample 표준편차의 유효 관측이 2개 미만이면 null과 근거 부족 상태다. 일반 중앙값은 even N의 두 가운데 평균이며 percentile/tdigest 근사와 다르다. exact median은 완전한 nonnull 자료를 정렬해 두 중앙 rank를 선택한다.
- 단위는 개별 측정의 명시 unit→검증된 producer/adapter 계약 순서로 확인한다. threshold와 측정을 같은 단위로 맞춘다. 크기/상식/정답 일치만으로 단위를 확정하거나 clipping/rescale하지 않는다. 파생 companion은 같은 선택·연산의 별도 DerivedValue/Unit/Basis이며 현재 recorded rate를 대체하지 않는다.
- reactive 합·비교·추세는 보통 deduplicated flat에서 계산한다. 없는 daily를 먼저 조회해 fallback을 유도하거나 첫 query 안에 daily/flat union/toscalar switch/가짜 empty row를 넣지 않는다. 실패한 discovery는 데이터 없음의 증거가 아니다.
- workout은 observed Value schema를 확인해 known adapter 또는 query-local extraction을 택한다. outer null/NO_UNIT으로 metric이 없다고 결론 내리지 않는다. 선택된 세션의 optional null을 남기고 다른 세션으로 backtrack하지 않는다. 조건에 해당하는 date key는 별도로 만들며 observation 전체의 조건과 날짜 합 조건을 구분한다.
- 비교의 두 기간을 따로 집계하고 matched-clock은 매일 같은 window를 반복한다. 일별 합을 먼저 만든 뒤 평균하고 실제 관측 일수/coverage를 보존한다. coarse daily summary에서 정확한 시각까지의 합을 복원하지 않는다. 부분 interval 배분은 추정·균등 가정을 명시한다.
- project/summarize가 남기는 column을 추적하고 삭제된 key로 정렬하지 않는다. dependent alias/JSON decoding은 successive extend에서 계산한다. 동적 array offset은 timespan 연산 전에 long으로 cast한다. datetime(...) literal과 todatetime(expression)을 구분한다. scalar iff는 table을 고르지 못한다. real(null)을 쓰며 알 수 없는 string unit은 빈 문자열로 표현한다. 입력 aggregation을 take로 자르지 않는다.
- typed output은 Metric/Aggregation/Value/Unit/EvidenceRows/ResultStatus, series는 Period를 반환한다. non-count의 빈 target은 null, 관측 가능한 entity의 빈 eligible count는 0이다. 누락 날짜를 0으로 채우지 않는다. 조건/선택 근거가 필요하면 관련 count를 별도로 남긴다.

### 현재 few-shot KQL 내용

생성 프롬프트에는 호흡수 표본의 exact median, scalar와 column 이름 충돌을 피한 첫 3세션 duration, 관측된 WORKOUT JSON 경로 추출, additive distance 구간의 시간 비례 배분을 보여 주는 코드가 있다. 제목은 “Three synthetic syntax demonstrations”지만 payload fragment까지 세면 **네 종류의 문법 시연**이다. 실제 사용자 ID·고정 평가 날짜·정답 값은 없으며 source/user/time은 placeholder다. 호흡수 Type, take 3, JSON key 등 실제 schema 문법은 포함한다. JSON key 예시는 모든 기기의 field를 고정하는 지시가 아니라 관측됐을 때만 적용하라는 조건부 예시다.

구간 배분식은 먼저 OriginalSeconds, OverlapStart/End를 계산하고 다음 extend에서 OverlapSeconds를 구한다. partial이면 IsEstimate=true/AllocationMethod=duration_proportional이다. median 예시는 Unit별 dedup·정렬·rank를 사용해 호환되지 않는 unit을 섞지 않는다. 이 예시를 평가 질문에 맞춘 정답 query로 복사하라고 지시하지 않는다.

## 6. 오류 수정 — repair_sensor_query.txt

검증/실행에서 실패한 KQL을 고친다. 보고된 오류를 해결하는 최소한의 의미 변경만 한다. 지표·집계·필터·선택·기간·alias·결과 shape를 보존하고, 문제가 그 항목 자체인 경우에만 바꾼다. 승인 함수·user/time·읽기 전용·결과 제한을 유지한다. backing table/문서에 없는 column/다른 지표로 우회하지 않는다. 빈 결과는 unavailable로 남기며 now()로 역사 window를 바꾸지 않는다.

원래 비교 기간·반복 window·dedup·추정 allocation·선택 identity를 유지한다. workout JSON을 잘못된 fixed adapter로 바꾸지 않는다. column scope, leftsemi/equality join, dynamic-timespan cast, stdev/stdevp, integer median rank, successive extend, numeric null, timestamp serialization 같은 일반 Kusto 문법을 점검한다. 출력은 설명 없는 수정 KQL이며 다시 guard를 통과해야 한다.

## 7. daily→flat 재조회 — recheck_daily_with_flat.txt

bounded coverage 확인에서 daily에 빠졌지만 flat에 존재하는 Type/day가 발견되었을 때 같은 원래 질문을 재조회한다. gap 표본 날짜만이 아니라 전체 원래 계획을 유한한 user/time 범위로 계산한다. 전체 daily가 실제 0개로 확인되었으면 단순 flat query를 쓰고 불필요한 union을 하지 않는다. mixed/미확인 coverage에서는 covered daily와 uncovered flat metric/day를 조정한다.

원래 최종 계산을 유지한다. 기존 일별 합계와 구간 자료의 중복/겹침을 고려하며 같은 키를 둘 다 더하지 않는다. 거의 하루 전체를 덮는 record는 midnight 직전까지여도 이미 daily total일 수 있다. 충돌/안전하게 조정 불가면 unavailable로 남긴다. 결측을 0으로 채우거나 원래 합을 max로 바꾸지 않는다. 결과 shape·precision·evidence/status를 유지하고 실제 Kusto 연산 이름과 successive extend를 사용한다. KQL만 반환한다.

## 8. 최종 문장 — common/generate_final_response/sensor_data.txt

지정된 언어로 결과부터 답한다. 사실은 Data JSON/Coverage에서만 가져오고 history는 의미 이해에만 쓴다. Metric/Aggregation/Value/Unit을 유지한다. status ok의 기록 0은 유효하고 unavailable은 문자 그대로 null이며 0이 아니다. 관측 없음·조건 해당 없음·실행 실패를 구분하고 제거한 조건을 말하려면 진단 근거가 있어야 한다. 실패/불완전 재조회로 완전한 집계를 주장하지 않는다.

주 기간은 padding이 아닌 logical 날짜로 설명하고 event/ingestion, in-bed/asleep, activity-night/civil/wake를 구분한다. 단일 집계·날짜별 series·비교를 요청대로 출력한다. 표시 수치는 기본 소수점 두 자리/count 정수이며 query 값 자체는 바꾸지 않는다. percentage의 합은 전체 비율이 아니라 기록 비율의 합이라고 말한다.

coverage가 없는 aggregate/top-k에서 누락 날짜를 추측하지 않는다. Period가 있지만 Value=null이면 선택 날짜는 알지만 그 측정은 없는 것이다. SelectedRows와 EvidenceRows를 구분한다. 파생량은 계산된 값·산식·duration 정의를 밝히며 companion을 native answer 대신 쓰지 않는다. 미검증 legacy unit을 사실로 말하지 않는다. 상식적인 크기/정답 일치로 unknown 단위를 확정하지 않는다.

가정·불완전 비교·추정 allocation을 공개한다. 진단·인과·제공자 추측을 하지 않는다. 간단한 plain text와 HH:MM AM/PM 형식을 쓰고 장식/상투적 추가 제안/앱에서 붙이는 footer를 중복 생성하지 않는다.

## 9. 오버피팅을 검토할 때 숨기면 안 되는 점

이 프롬프트는 **규칙이 전혀 없는 프롬프트가 아니다**. JSON interface, read-only 범위, dedup, Kusto syntax와 측정 정의뿐 아니라 완료 기간·주 시작일·수면 night label·표본 표준편차·모호한 운동 속성의 기본 범위를 명시한다. 특히 후자의 의미 기본값은 연구 가정으로 논문에 공개해야 한다.

정답 숫자/UserId/PHIA 전용 테이블을 runtime에 넣지 않는 것과 오버피팅이 완전히 없다는 증명은 다르다. 개발 평가 오류를 보고 이 공통 해석을 보완해 왔으므로 독립 hold-out, 새로운 질문 표현, 다른 장치/데이터셋, 예시 제거 ablation으로 일반화를 확인해야 한다. 미래 테스트 결과를 보고 사후적으로 주어진 단위/시간/threshold를 바꾸면 label leakage가 될 수 있다.

이번 문서화 작업은 운영 `.txt`와 LangGraph를 변경하지 않았다. 한글 문서를 수정해도 runtime prompt가 바뀌지 않는다.
