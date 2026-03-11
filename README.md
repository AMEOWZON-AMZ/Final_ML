# 냥냥 편지 ML (`Final_ML`)

이 저장소는 **사용자 상태(state)에 맞는 BGM 스템(vocal/background)**과 **사용자 음성 샘플(enroll)**을 조합해,
Seed-VC로 보컬을 변환한 뒤 최종 BGM을 생성/업로드하는 배치 파이프라인입니다.

핵심 기능은 다음 2가지입니다.

1. **단일 사용자 추론**: `scripts/music_infer.py`
2. **전체 사용자 배치 처리**: `scripts/run_music_batch.py`

---

## 1) 저장소 구조

```text
.
├─ config/
│  └─ config.yaml                # 로컬/기본 실행 설정
├─ scripts/
│  ├─ music_infer.py             # 1명 단위 Seed-VC 추론 + 믹싱 + S3 업로드
│  └─ run_music_batch.py         # DynamoDB 스캔 후 전체 사용자 배치 실행
├─ Dockerfile                    # CUDA 기반 실행 이미지 빌드
├─ buildspec.yml                 # AWS CodeBuild → ECR 푸시 파이프라인
├─ requirements.seedvc.notch.txt# Seed-VC 관련 Python 의존성
├─ run.ipynb                     # 수동 실행용 노트북
└─ docker_build.ipynb            # 수동 도커 빌드 노트북
```

---

## 2) 전체 동작 흐름

### A. 배치 엔트리 (`run_music_batch.py`)

1. `user_status` DynamoDB 테이블을 scan
2. 각 사용자에 대해:
   - `is_critical == True` 또는 허용되지 않은 상태면 스킵
   - `meow/enroll/{user_id}/enroll.*` 존재 확인
   - `meow/stem/{state}/vocal.*`, `.../background.*` 존재 확인
3. 조건을 통과한 사용자만 `music_infer.py` 서브프로세스 실행
4. 결과 파일 `meow/daily_outputs/dt={DT}/{user_id}/today_bgm.wav` URL 생성
5. User 서비스 `/api/v1/bgm/update`로 URL 전송

### B. 단일 추론 (`music_infer.py`)

1. `config/config.yaml` 로드 + ENV 오버라이드
2. S3에서 입력 3종 다운로드
   - 상태별 vocal stem
   - 상태별 background stem
   - 사용자 enroll
3. ffmpeg로 mono WAV(44.1kHz) 변환
4. vocal 최소 전처리(`highpass/lowpass/afftdn`)
5. Seed-VC `inference.py` 실행
6. 후처리 + 피치 업(`rubberband`)
7. background + 변환 vocal 믹싱
8. 최종 WAV를 S3 업로드

---

## 3) 주요 스크립트 설명

## `scripts/music_infer.py`

- 목적: **한 사용자 음성 기반 BGM 생성**
- 설정 우선순위: `ENV > config.yaml`
- 입력 확장자: `wav/mp3/m4a/aac/flac/ogg/opus`
- 출력 경로 규칙:
  - `s3://{bucket}/{output_prefix}/dt={dt}/{uuid}/{output_filename}`

### 중요한 ENV

- `UUID`: 사용자 ID
- `STATE`: 사용자 상태 (`chaos`, `stable` 등 소문자 권장)
- `DT`: 출력 날짜 파티션 (`YYYY-MM-DD`, 미지정 시 KST 기준 어제)
- `BGM_BUCKET`: S3 버킷명

> 참고: 코드에 `VOCAL_S3_KEY`, `BGM_S3_KEY`, `ENROLL_S3_KEY`를 넣어도,
현재 `music_infer.py`는 내부 규칙(prefix+state+uuid)으로 키를 다시 계산해 사용합니다.

---

## `scripts/run_music_batch.py`

- 목적: **DynamoDB 기준 전체 사용자 일괄 처리**
- 상태 정책:
  - 스킵: `NO_DATA`, `CRITICAL`
  - 허용: `STABLE`, `SLEEP`, `LETHARGY`, `CHAOS`, `TRAVEL`
- 외부 연동:
  - DynamoDB: `user_status`
  - S3: stem/enroll/output
  - 사용자 서비스: `/api/v1/bgm/update`

### 중요한 ENV

- 필수:
  - `DT` (예: `2026-02-12`)
  - `USER_POD_BASE` (유저 서비스 베이스 URL)
- 선택:
  - `MEOW_DIR` (기본 `/opt/ml/processing/meow-seedvc`)
  - `AWS_REGION`, `BGM_BUCKET`
  - `STEM_PREFIX`, `ENROLL_PREFIX`, `OUTPUT_PREFIX`, `OUTPUT_FILENAME`
  - `USER_STATUS_TABLE`

---

## 4) 설정 파일 (`config/config.yaml`)

기본 설정은 아래 범주로 구성됩니다.

- `run`: 로컬 테스트용 `uuid`, `state`
- `s3`: 버킷/프리픽스/파일명 규칙
- `audio`: `sample_rate`, `vocal_gain`, `pitch_up_semitone`
- `seed_vc`: `repo_dir`, `work_dir`, `fp16` 등

`music_infer.py`는 이 설정을 기반으로 동작하며, 일부 항목은 ENV로 덮어쓸 수 있습니다.

---

## 5) 실행 방법

### 5-1. 로컬 단일 추론

```bash
python -u scripts/music_infer.py
```

또는

```bash
UUID="<user_id>" STATE="chaos" DT="2026-02-12" python scripts/music_infer.py
```

### 5-2. 배치 실행

```bash
DT=2026-02-12 USER_POD_BASE="http://<user-service-host>" python scripts/run_music_batch.py
```

---

## 6) Docker/배포

## `Dockerfile`

- 베이스: `nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04`
- 시스템 패키지: `python3`, `ffmpeg`, `git`, 오디오 라이브러리
- PyTorch: CUDA 12.1 wheel 고정 설치
- `requirements.seedvc.notch.txt` 설치
- 필요 시 `vc/seed-vc` 자동 clone

## `buildspec.yml`

- CodeBuild 단계:
  1. ECR 로그인
  2. `docker build`
  3. 이미지 태깅
  4. ECR push
  5. `image_uri.txt` 아티팩트 생성

---

## 7) 노트북 파일 용도

- `run.ipynb`
  - `music_infer.py` 단건 실행
  - `run_music_batch.py` 실행 예시
- `docker_build.ipynb`
  - `docker build` 명령을 노트북에서 수행

---

## 8) 현재 코드 기준 체크 포인트

- `run_music_batch.py`는 `music_infer.py`에 S3 key를 ENV로 넘기지만,
  `music_infer.py`는 해당 키를 직접 사용하지 않고 자체 탐색합니다.
- `config.yaml`의 `seed_vc.diffusion_steps`, `f0_condition` 값은
  현재 `music_infer.py` 커맨드 생성 시 하드코딩된 값과 다를 수 있습니다.
  (실제 실행은 스크립트 하드코딩 인자 기준)

필요하면 다음 리팩토링이 가능합니다.

1. `music_infer.py`가 전달받은 `VOCAL_S3_KEY/BGM_S3_KEY/ENROLL_S3_KEY`를 우선 사용
2. Seed-VC 인자를 `config.yaml` 기반으로 완전 외부화
3. DynamoDB scan → pagination 최적화/필터링 강화

