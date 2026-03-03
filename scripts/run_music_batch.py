# scripts/run_music_batch.py
import os
import json
import subprocess
from typing import Optional, Tuple, List

import boto3
import botocore
import requests

# -----------------------------
# Env
# -----------------------------
DT = os.environ["DT"]  # e.g. 2026-02-12
MEOW_DIR = os.environ.get("MEOW_DIR", "/opt/ml/processing/meow-seedvc")

AWS_REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
BGM_BUCKET = os.environ.get("BGM_BUCKET", "amz-bgm")

STEM_PREFIX = os.environ.get("STEM_PREFIX", "meow/stem")
ENROLL_PREFIX = os.environ.get("ENROLL_PREFIX", "meow/enroll")

OUTPUT_PREFIX = os.environ.get("OUTPUT_PREFIX", "meow/daily_outputs")
OUTPUT_FILENAME = os.environ.get("OUTPUT_FILENAME", "today_bgm.wav")

USER_STATUS_TABLE = os.environ.get("USER_STATUS_TABLE", "user_status")

USER_POD_BASE = os.environ["USER_POD_BASE"]  # e.g. http://user-service...:8000

# 확장자 후보 (원하면 더 추가)
EXTS = [".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"]

# 상태 정책
SKIP_STATES = {"NO_DATA", "CRITICAL"}
ALLOW_STATES = {"STABLE", "SLEEP", "LETHARGY", "CHAOS", "TRAVEL"}

# -----------------------------
# AWS clients
# -----------------------------
s3 = boto3.client("s3", region_name=AWS_REGION)
ddb = boto3.resource("dynamodb", region_name=AWS_REGION)
user_status_table = ddb.Table(USER_STATUS_TABLE)


def s3_exists(bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False
        # 권한/기타 에러는 그대로 올려서 문제를 빨리 드러내자
        raise


def find_first_existing(bucket: str, base_key_no_ext: str, exts: List[str]) -> Optional[str]:
    """
    base_key_no_ext: 확장자 없는 key (예: meow/stem/chaos/vocal)
    return: 존재하는 첫 key (예: .../vocal.wav) or None
    """
    for ext in exts:
        key = f"{base_key_no_ext}{ext}"
        if s3_exists(bucket, key):
            return key
    return None


def iter_user_status_items():
    """
    user_status 전체 scan (MVP).
    Items: user_id, current_daily_status, is_critical
    """
    scan_kwargs = {
        "ProjectionExpression": "user_id, current_daily_status, is_critical",
    }
    resp = user_status_table.scan(**scan_kwargs)
    for item in resp.get("Items", []):
        yield item

    while "LastEvaluatedKey" in resp:
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        resp = user_status_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            yield item


def s3_public_url(bucket: str, region: str, key: str) -> str:
    # MVP: presigned 없이 퍼블릭 URL 형태로 저장(버킷 퍼블릭/CloudFront 등 정책에 따라 조정 가능)
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def bgm_update(user_id: str, bgm_url: str):
    payload = {"user_id": user_id, "bgm_url": bgm_url}
    r = requests.post(f"{USER_POD_BASE}/api/v1/bgm/update", json=payload, timeout=20)
    r.raise_for_status()


def run_music_infer(user_id: str, state: str, vocal_key: str, bgm_key: str, enroll_key: str) -> str:
    """
    music_infer.py를 1명에 대해 실행하고, 업로드된 output key를 반환.
    output key 규칙은 deterministic 하므로 여기서 계산해서 반환.
    """
    env = os.environ.copy()
    env["UUID"] = user_id
    env["STATE"] = state
    env["DT"] = DT
    env["VOCAL_S3_KEY"] = vocal_key
    env["BGM_S3_KEY"] = bgm_key
    env["ENROLL_S3_KEY"] = enroll_key

    subprocess.run(
        ["python", "scripts/music_infer.py"],
        cwd=MEOW_DIR,
        env=env,
        check=True,
    )

    output_key = f"{OUTPUT_PREFIX}/dt={DT}/{user_id}/{OUTPUT_FILENAME}"
    return output_key

def normalize_state(state: str) -> str:
    # user_status가 대문자로 저장돼도 stem 폴더는 소문자일 가능성 높아서 정규화
    return state.strip().lower()

def log_skip(user_id: str, state_raw: str, reason: str, extra: str = ""):
    msg = f"[SKIP] user_id={user_id} state={state_raw} reason={reason}"
    if extra:
        msg += f" extra={extra}"
    print(msg)

def main():
    total = 0
    skipped = 0
    success = 0
    failed = 0

    for item in iter_user_status_items():
        total += 1

        user_id = item.get("user_id")
        state_raw = (item.get("current_daily_status") or "").strip()
        is_critical = bool(item.get("is_critical", False))

        if not user_id or not state_raw:
            skipped += 1
            log_skip(str(user_id), str(state_raw), "MISSING_FIELD")
            continue

        # critical 플래그 우선
        if is_critical:
            skipped += 1
            log_skip(user_id, state_raw, "IS_CRITICAL_TRUE")
            continue

        # 상태 정책
        if state_raw in SKIP_STATES:
            skipped += 1
            log_skip(user_id, state_raw, "STATE_IN_SKIP_STATES")
            continue
        if state_raw not in ALLOW_STATES:
            skipped += 1
            log_skip(user_id, state_raw, "STATE_NOT_ALLOWED")
            continue

        state = normalize_state(state_raw)

        # 1) enroll 존재 확인 (여러 확장자 허용)
        enroll_base = f"{ENROLL_PREFIX}/{user_id}/enroll"
        enroll_key = find_first_existing(BGM_BUCKET, enroll_base, EXTS)
        if not enroll_key:
            # 더미 사용자(음성 없음) => 스킵
            skipped += 1
            log_skip(user_id, state_raw, "NO_ENROLL", extra=enroll_base)
            continue

        # 2) stem 존재 확인
        vocal_base = f"{STEM_PREFIX}/{state}/vocal"
        bgm_base = f"{STEM_PREFIX}/{state}/background"

        vocal_key = find_first_existing(BGM_BUCKET, vocal_base, EXTS)
        bgm_key = find_first_existing(BGM_BUCKET, bgm_base, EXTS)

        if not vocal_key or not bgm_key:
            skipped += 1
            log_skip(
                user_id,
                state_raw,
                "NO_STEM",
                extra=f"vocal_base={vocal_base}, bgm_base={bgm_base}",
            )
            continue

        # 3) 실행 + 업서트
        try:
            out_key = run_music_infer(user_id, state, vocal_key, bgm_key, enroll_key)
            url = s3_public_url(BGM_BUCKET, AWS_REGION, out_key)
            bgm_update(user_id, url)
            success += 1
            print(f"[OK] user_id={user_id} state={state_raw} out_key={out_key}")
        except Exception as e:
            failed += 1
            print(f"[FAIL] user_id={user_id} state={state_raw} err={e}")

    print(json.dumps({
        "dt": DT,
        "total_items": total,
        "skipped": skipped,
        "success": success,
        "failed": failed,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()