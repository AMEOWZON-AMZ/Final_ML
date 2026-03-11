# NYAN-MATE ML 파이프라인 (Final_ML)

사용자 모바일 이벤트(silver raw)를 기반으로 **일별 행동 피처를 생성**하고,
`IsolationForest + 규칙 기반 해석(Decoder)`으로 **고양이 상태(cat_state)와 알림 레벨(notify_level)**을 산출하는 ML 파이프라인입니다.

---

## 1) 프로젝트 목적

이 저장소는 아래 2가지 운영 흐름을 중심으로 구성되어 있습니다.

- **주 1회 학습 파이프라인**
  - 공개 feature 데이터 + 최근 사용자 ETL 데이터를 결합해 모델 재학습
  - SageMaker Training Job으로 실행
- **일 1회 추론 파이프라인**
  - 당일 silver raw → FE(daily-feature) 생성
  - 최근 윈도우 데이터를 활용해 상태 추론
  - 결과를 S3에 저장 (향후 RDS upsert 확장)

핵심 출력:

- `state_out.csv`: `uuid`, `date`, `cat_state`, `notify_level` 등
- `baseline.parquet`: 추론 기준 베이스라인 통계
- `isolation_forest.pkl`: 최신 학습 모델

---

## 2) 저장소 구조

```text
Final_ML/
├── src/
│   ├── runtime/
│   │   ├── train.py           # SageMaker 학습 엔트리
│   │   ├── batch_runner.py    # EKS 일 배치 엔트리
│   │   └── inference.py       # FE→MODEL→DECODER 오케스트레이션
│   └── steps/
│       ├── features.py        # 일별 피처 생성
│       ├── model.py           # baseline/Z/anomaly/risk/drift
│       └── decoder.py         # 상태/알림 디코딩
├── lambda/
│   └── trigger_training.py    # EventBridge→Lambda→SageMaker 트리거
├── scripts/
│   └── generate_dummy_silver.py  # 더미 silver + daily-feature 생성
├── train_entrypoint.py        # SageMaker hyperparameters 래퍼
├── config.yaml                # S3/학습/추론/파라미터 중앙 설정
├── Dockerfile.train           # 학습 컨테이너 이미지
├── buildspec.yml              # CodeBuild→ECR 빌드/푸시
├── requirements.txt
└── notebooks/
    ├── v3_FE.ipynb
    ├── v3_MODEL.ipynb
    └── v3_DECODER.ipynb
```

> 주의: `src/**/__pycache__`는 실행 산출물이며 코드 소스가 아닙니다.

---

## 3) 전체 아키텍처

### 3-1. 학습(주기)

1. Lambda(`lambda/trigger_training.py`)가 EventBridge 스케줄로 실행
2. SageMaker Training Job 생성 (컨테이너: `Dockerfile.train`)
3. 컨테이너 엔트리(`train_entrypoint.py`)가 하이퍼파라미터를 CLI 인자로 변환
4. `src/runtime/train.py` 실행
   - 공개 feature parquet 로드
   - 사용자 ETL parquet 로드 + FE 수행
   - 데이터 결합 후 `model.run(..., mode="train")`
   - `isolation_forest.pkl`을 S3에 최신본 overwrite 저장

### 3-2. 추론(일배치)

1. EKS CronJob이 `src/runtime/batch_runner.py` 실행
2. 당일 silver parquet 로드
3. `features.run()`으로 당일 daily-feature 생성 후 S3 저장
4. 최근 lookback window의 daily-feature를 로드
5. `inference.run(feat_df=..., model_uri=...)` 실행
   - `model.run(..., mode="infer")`
   - `decoder.run(...)`
6. `baseline.parquet` + `state_out.csv` S3 저장

---

## 4) 핵심 모듈 상세

## 4-1. Feature Engineering (`src/steps/features.py`)

raw 이벤트를 `(uuid, date)` 단위로 집계해 아래 특징을 생성합니다.

- **활동량 계열**: `Screen`, `UserAct`, `Notif`, `Unlock`, `daily_event_cnt`
- **리듬 계열**: `night_ratio`, `hour_entropy`, `day_ratio`, `peak_hour` 등
- **공백(gap) 계열**: `gap_max`, `gap_p95`, `gap_cnt_2h`, `gap_cnt_6h`, `overnight_gap`
- **세션 계열**: `session_cnt`, `session_total_sec`, `long_session_cnt`
- **이동성 계열**: `cell_change_cnt`, `wifi_change_cnt_est`, `unique_wifi_cnt`, `step_sum`
- **메타/품질 계열**: heartbeat 기반 `retry_max`, `queue_max`, `tz_changed`, QC 플래그
- **컨텍스트 신호**: `partial_signal_raw`, `travel_signal_raw`, `tz_change_signal`
- **증감(delta) 피처**: 전일 대비 차분(`*_d1`) 및 일부 비율(`*_r1`)

특징:

- 다양한 입력 컬럼 후보(`event_name`, `sensor_id`, `timestamp` 등)를 자동 매핑
- `tz_offset_minutes`를 반영한 로컬 날짜/시간 계산
- 커버리지 낮은 날은 `LOW_CONF`로 이어질 수 있게 QC 플래그 유지

## 4-2. Model (`src/steps/model.py`)

`features.py` 결과를 기반으로 이상도와 리스크를 계산합니다.

- **Baseline 계산**
  - LT(30일), ST(14일), weekday/weekend split, Early(7일) baseline
  - `baseline_fit_mask`로 학습 가능 구간 필터링
- **Z-score 계산**
  - baseline 대비 편차 산출 후 tanh clip 적용
- **IsolationForest**
  - 학습 시 `mode="train"`에서 IF 학습
  - 추론 시 전달된 모델로 `anomaly_score` 계산
  - 사용자별 quantile 기반 스케일링(Q80~Q99)
- **Risk 계산**
  - `risk_pre = 0.85*Z + 0.15*Anomaly`
  - `context_mode`(PARTIAL/TRAVEL) discount 반영
  - EMA smoothing으로 `risk_score` 산출
  - cold-start 구간은 `early_risk` fallback
- **Recovery/Drift**
  - bad history 이후 안정화 hold
  - anchor 구간 대비 drift 감지 및 top feature 기록

## 4-3. Decoder (`src/steps/decoder.py`)

`model_out`을 사용자 상태로 변환합니다.

- `risk_used` 기반 risk band: `SAFE/WATCH/ALERT/SEVERE`
- `analysis_ready` gate 실패 시 `NO_DATA`
- `travel_flag=True`면 `TRAVEL` 우선
- 정상 분석 가능 시
  - 그룹 대표 Z(`z_rhythm_rep`, `z_core_rep`, `z_gap_rep`) 우선
  - top-Z fallback으로 `SLEEP/LETHARGY/CHAOS/STABLE` 판정
- 알림 정책
  - 기본 알림 + 컨텍스트 다운시프트 + cold-start 정책 반영
  - 최종 출력 `notify_level`

---

## 5) 런타임 엔트리포인트

### `src/runtime/train.py`

- 공개 feature와 사용자 ETL feature를 결합해 학습
- MLflow 로깅(Tracking URI/Experiment 하드코딩)
- 최종 모델을 `s3://.../isolation_forest.pkl`에 저장

주요 인자:

- `--s3-public-feature-uri`
- `--s3-etl-uri`
- `--lookback-days`
- `--s3-model-prefix`
- `--as-of-date`

### `src/runtime/batch_runner.py`

- 당일 silver 로드 → FE → daily-feature 저장
- 최근 윈도우 daily-feature 로드 후 추론
- baseline/state_out 저장
- RDS upsert는 TODO(stub)

주요 인자:

- `--target-date`
- `--s3-silver-uri`
- `--s3-daily-feature-uri`
- `--s3-baseline-uri`
- `--model-uri`
- `--s3-output-uri`

### `src/runtime/inference.py`

- S3 또는 로컬에서 모델 로드
- `raw_df` 또는 `feat_df` 입력을 받아
  `features → model(infer) → decoder` 순서 실행

---

## 6) 배포/인프라 구성

### 6-1. 학습 컨테이너

- 파일: `Dockerfile.train`
- 베이스: `python:3.11-slim`
- `sagemaker-training` 설치 후 저장소 코드 복사
- 엔트리: `python -u train_entrypoint.py`

### 6-2. 이미지 빌드/푸시

- 파일: `buildspec.yml`
- CodeBuild 단계
  - ECR 로그인
  - Docker build/tag
  - `${IMAGE_TAG}` + `latest` 푸시

### 6-3. 학습 트리거 Lambda

- 파일: `lambda/trigger_training.py`
- EventBridge cron으로 실행
- `create_training_job` 호출
- 필수 환경변수:
  - `SAGEMAKER_ROLE_ARN`
  - `ECR_IMAGE_URI`
  - `S3_PUBLIC_URI`
  - `S3_ETL_URI`
  - `S3_OUTPUT_MODEL_URI`

---

## 7) 설정 (`config.yaml`)

중앙 설정 파일로 다음을 관리합니다.

- `s3`: 입력/출력/model/baseline 경로
- `training`: lookback, SageMaker 스펙/스케줄, IF 파라미터
- `inference`: lookback/timezone/schedule
- `feature_engineering`, `model`, `decoder`: 각 step 상수 동기화 참고값
- `aws`, `local`: 리전/클러스터/로컬 테스트 경로

> 실제 코드 상수와 `config.yaml` 값은 **수동 동기화** 구조입니다.

---

## 8) 로컬 실행 예시

### 8-1. 의존성 설치

```bash
pip install -r requirements.txt
```

### 8-2. 학습 실행 (직접)

```bash
python -u src/runtime/train.py \
  --s3-public-feature-uri s3://.../daily_feature_public.parquet \
  --s3-etl-uri s3://.../silver_events/ \
  --lookback-days 30 \
  --s3-model-prefix s3://.../ml/artifacts/models/
```

### 8-3. 일 배치 실행 (직접)

```bash
python -u src/runtime/batch_runner.py \
  --target-date 2026-02-19 \
  --s3-silver-uri s3://.../silver_events/ \
  --s3-daily-feature-uri s3://.../daily-feature/ \
  --s3-baseline-uri s3://.../baseline/ \
  --model-uri s3://.../models/isolation_forest.pkl \
  --s3-output-uri s3://.../outputs/
```

### 8-4. 더미 데이터 생성

```bash
python scripts/generate_dummy_silver.py \
  --source-parquet notebooks/part-0000.parquet \
  --s3-silver-uri s3://silver-dummy/silver_events/ \
  --s3-daily-feature-uri s3://nyang-ml-apne2-dev/ml/daily-feature/ \
  --start-date 2026-01-20 \
  --end-date 2026-02-19
```

---

## 9) 입력/출력 데이터 포맷

### 입력(raw silver) 예상 핵심 컬럼

- 필수에 가까움: `uuid`, 이벤트명(`event_name` 등), timestamp(`timestamp` 등)
- 선택: `tz_offset_minutes`, `step_count`, `wifi_ssid`, `cell_lac`, `retry_count`, `queue_size` 등

### 출력(state_out)

- `uuid`, `date`
- `cat_state` (`NO_DATA`, `TRAVEL`, `STABLE`, `SLEEP`, `LETHARGY`, `CHAOS`)
- `notify_level` (`NONE`, `LOW`, `HIGH`)
- 품질/근거: `decoder_quality`, `risk_band`, `top_z_feature`, `top_z_value` 등

---

## 10) 현재 코드 기준 체크 포인트

- `batch_runner.py`에서 최근 로드 일수 상수는 `LT_WINDOW = 31`인데, 주석에는 56일 언급이 일부 남아 있습니다. (주석 정합화 필요)
- `decoder.py`의 `apply_context_downshift`에 `return "NONE"` 중복 라인이 1개 있습니다. (동작 영향은 거의 없지만 정리 권장)
- MLflow Tracking URI/Experiment가 `train.py`에 하드코딩되어 있어 환경별 분리가 필요할 수 있습니다.

---

## 11) 원본 노트북과의 관계

`src/steps/*.py`는 아래 노트북 로직을 운영 코드로 포팅한 형태입니다.

- `notebooks/v3_FE.ipynb` → `src/steps/features.py`
- `notebooks/v3_MODEL.ipynb` → `src/steps/model.py`
- `notebooks/v3_DECODER.ipynb` → `src/steps/decoder.py`

즉, 실서비스에서는 `src/` 코드가 실행 경로이며, 노트북은 실험/검증/문서화 목적의 원형으로 보는 것이 맞습니다.
