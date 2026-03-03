import os
import yaml
import boto3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from botocore.exceptions import ClientError

# -------------------------
# Load config
# -------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"

with open(CONFIG_PATH, "r") as f:
    cfg = yaml.safe_load(f)

# -------------------------
# Runtime override (ENV > config)
# -------------------------
uuid = os.getenv("UUID", str(cfg["run"]["uuid"])).strip()
state = os.getenv("STATE", str(cfg["run"]["state"])).strip().lower()

bucket = os.getenv("BGM_BUCKET", cfg["s3"]["bucket"])
stem_prefix = cfg["s3"]["stem_prefix"]
enroll_prefix = cfg["s3"]["enroll_prefix"]
output_prefix = cfg["s3"]["output_prefix"]

enroll_filename = cfg["s3"]["enroll_filename"]
output_filename = cfg["s3"]["output_filename"]

repo_dir = (BASE_DIR / cfg["seed_vc"]["repo_dir"]).resolve() if not str(cfg["seed_vc"]["repo_dir"]).startswith("/") else Path(cfg["seed_vc"]["repo_dir"]).resolve()
work_dir = (BASE_DIR / cfg["seed_vc"]["work_dir"]).resolve() if not str(cfg["seed_vc"]["work_dir"]).startswith("/") else Path(cfg["seed_vc"]["work_dir"]).resolve()
work_dir.mkdir(parents=True, exist_ok=True)

sample_rate = int(cfg["audio"]["sample_rate"])   # 44100
vocal_gain = float(cfg["audio"]["vocal_gain"])
fp16 = bool(cfg["seed_vc"]["fp16"])

# -------------------------
# Pitch shift: "살짝" 올리기 (VC 이후에만 적용)
# - 1: 반~한키 느낌(세미톤 1)
# - 2: 한키(세미톤 2)
# 로봇화 방지 목적이라 기본 1 추천
# -------------------------
PITCH_UP_SEMITONE = float(cfg.get("audio", {}).get("pitch_up_semitone", 1))

# -------------------------
# Date (KST 기준 어제)
# -------------------------
KST = timezone(timedelta(hours=9))
dt = os.getenv(
    "DT",
    (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")
)

# -------------------------
# Local fixed WAV paths
# -------------------------
vocal_wav  = work_dir / "vocal.wav"
bgm_wav    = work_dir / "bgm.wav"
enroll_wav = work_dir / "enroll.wav"
final_mix  = work_dir / "final_mix.wav"

# -------------------------
# helpers
# -------------------------
s3 = boto3.client("s3")
AUDIO_EXTS = ["wav", "mp3", "m4a", "aac", "flac", "ogg", "opus"]

def build_key_candidates(prefix_no_ext: str, exts=AUDIO_EXTS) -> list[str]:
    return [f"{prefix_no_ext}.{ext}" for ext in exts]

def s3_exists(bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise

def first_existing_key(bucket: str, key_candidates: list[str]) -> str:
    for key in key_candidates:
        if s3_exists(bucket, key):
            return key
    raise FileNotFoundError(f"S3 object not found. Tried: {key_candidates}")

def suffix_from_key(key: str) -> str:
    s = Path(key).suffix.lower()
    return s if s else ".bin"

def run_cmd(cmd: list[str], *, cwd: str | None = None, env: dict | None = None):
    print("🧾 CMD:", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True, env=env)

def to_wav_mono(inp: Path, out_wav: Path):
    tmp = out_wav.with_suffix(".tmp.wav")
    run_cmd([
        "ffmpeg", "-y",
        "-i", str(inp),
        "-ar", str(sample_rate),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(tmp)
    ])
    tmp.replace(out_wav)

def pick_latest_vc_wav(work_dir: Path) -> Path:
    outs = sorted(work_dir.glob("vc_*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    if outs:
        return outs[0]
    candidates = [
        p for p in work_dir.glob("*.wav")
        if p.name not in {
            "vocal.wav", "bgm.wav", "enroll.wav", "final_mix.wav",
            "vocal_pre.wav", "enroll_pre.wav", "vc_clean.wav",
            "vc_pitch.wav"
        }
    ]
    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    if candidates:
        return candidates[0]
    wavs = sorted(work_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
    raise FileNotFoundError(f"Seed-VC output not found. Recent wavs: {[p.name for p in wavs[:10]]}")

def pitch_up_rubberband(inp_wav: Path, out_wav: Path, semitone: float):
    """
    템포 유지 + 피치만 올리기.
    ffmpeg가 --enable-librubberband면 동작.
    """
    if semitone == 0:
        inp_wav.replace(out_wav)
        return
    ratio = 2 ** (semitone / 12.0)
    run_cmd([
        "ffmpeg", "-y",
        "-i", str(inp_wav),
        "-ar", str(sample_rate),
        "-ac", "1",
        "-c:a", "pcm_s16le",
        "-af", f"rubberband=pitch={ratio}",
        str(out_wav)
    ])

# -------------------------
# S3 keys (확장자 유연)
# -------------------------
vocal_keys = build_key_candidates(f"{stem_prefix}/{state}/vocal")
bgm_keys   = build_key_candidates(f"{stem_prefix}/{state}/background")

enroll_keys = [f"{enroll_prefix}/{uuid}/{enroll_filename}"]
enroll_base = Path(enroll_filename).stem
for ext in AUDIO_EXTS:
    cand = f"{enroll_prefix}/{uuid}/{enroll_base}.{ext}"
    if cand not in enroll_keys:
        enroll_keys.append(cand)

output_key = f"{output_prefix}/dt={dt}/{uuid}/{output_filename}"

# -------------------------
# Download raw inputs
# -------------------------
print("⬇ Downloading from S3...")
vocal_used_key  = first_existing_key(bucket, vocal_keys)
bgm_used_key    = first_existing_key(bucket, bgm_keys)
enroll_used_key = first_existing_key(bucket, enroll_keys)

vocal_raw  = work_dir / f"vocal_input{suffix_from_key(vocal_used_key)}"
bgm_raw    = work_dir / f"bgm_input{suffix_from_key(bgm_used_key)}"
enroll_raw = work_dir / f"enroll_input{suffix_from_key(enroll_used_key)}"

s3.download_file(bucket, vocal_used_key,  str(vocal_raw))
s3.download_file(bucket, bgm_used_key,    str(bgm_raw))
s3.download_file(bucket, enroll_used_key, str(enroll_raw))

print(f"✅ vocal  from s3://{bucket}/{vocal_used_key}")
print(f"✅ bgm    from s3://{bucket}/{bgm_used_key}")
print(f"✅ enroll from s3://{bucket}/{enroll_used_key}")

# -------------------------
# Convert inputs -> wav mono
# -------------------------
print("🎧 Converting inputs to wav (mono)...")
to_wav_mono(vocal_raw, vocal_wav)
to_wav_mono(bgm_raw, bgm_wav)
to_wav_mono(enroll_raw, enroll_wav)

# -------------------------
# Preprocess (VERY MINIMAL) - 뭉개짐 줄이되 로봇화 방지
# - enroll: 건드리지 않음
# - vocal: afftdn 약화 (-28 -> -26)
# -------------------------
print("🎚 Preprocess vocal (minimal)...")
vocal_pre = work_dir / "vocal_pre.wav"
run_cmd([
    "ffmpeg", "-y",
    "-i", str(vocal_wav),
    "-ar", str(sample_rate),
    "-ac", "1",
    "-c:a", "pcm_s16le",
    "-af", "highpass=f=70,lowpass=f=16000,afftdn=nf=-26",
    str(vocal_pre)
])
vocal_wav = vocal_pre

# -------------------------
# Seed-VC inference (아까 너가 괜찮다고 한 세팅 유지)
# - steps=40
# - cfg=0.6
# - auto-f0-adjust=true
# - semi-tone-shift=0 (여기서 올리면 로봇화 날 수 있어서 금지)
# -------------------------
print("🐱 Running Seed-VC...")

for p in work_dir.glob("vc_*.wav"):
    try:
        p.unlink()
    except Exception:
        pass

cmd = [
    "python", "inference.py",
    "--source", str(vocal_wav),
    "--target", str(enroll_wav),
    "--output", str(work_dir),

    "--diffusion-steps", "40",
    "--inference-cfg-rate", "0.6",
    "--fp16", "true" if fp16 else "false",

    "--f0-condition", "true",
    "--auto-f0-adjust", "true",
    "--semi-tone-shift", "0",
]

env = os.environ.copy()
env["TRANSFORMERS_NO_TF"] = "1"
env["USE_TF"] = "0"
env["TF_CPP_MIN_LOG_LEVEL"] = "3"

run_cmd(cmd, cwd=str(repo_dir), env=env)

generated = pick_latest_vc_wav(work_dir)
print(f"✅ Seed-VC output: {generated.name}")

# -------------------------
# Post-process (MINIMAL) + Pitch up (VC 이후에만)
# -------------------------
print("🧼 Post-process VC vocal (minimal)...")
vc_clean = work_dir / "vc_clean.wav"
run_cmd([
    "ffmpeg", "-y",
    "-i", str(generated),
    "-ar", str(sample_rate),
    "-ac", "1",
    "-c:a", "pcm_s16le",
    "-af", "highpass=f=70,aecho=0.8:0.75:30:0.3,alimiter=limit=0.95",
    str(vc_clean)
])

print(f"🎼 Pitch up after VC: +{PITCH_UP_SEMITONE} semitone(s)")
vc_pitch = work_dir / "vc_pitch.wav"
pitch_up_rubberband(vc_clean, vc_pitch, PITCH_UP_SEMITONE)
generated = vc_pitch

# -------------------------
# Mix vocal + bgm
# -------------------------
print("🎼 Mixing audio...")

mix_script = work_dir / "mix.filter"
mix_filter = (
    f"[1:a]volume={vocal_gain}[v0];"
    f"[0:a][v0]amix=inputs=2:duration=first:dropout_transition=0,"
    f"aformat=channel_layouts=stereo[o0]\n"
)

try:
    mix_script.unlink()
except FileNotFoundError:
    pass

with open(mix_script, "wb") as f:
    f.write(mix_filter.encode("ascii", errors="ignore"))

run_cmd([
    "ffmpeg", "-y",
    "-i", str(bgm_wav),
    "-i", str(generated),
    "-filter_complex_script", str(mix_script),
    "-map", "[o0]",
    "-c:a", "pcm_s16le",
    "-ar", str(sample_rate),
    str(final_mix)
])

# -------------------------
# Upload result
# -------------------------
print("⬆ Uploading today_bgm...")
s3.upload_file(str(final_mix), bucket, output_key)

print(f"✅ Done: s3://{bucket}/{output_key}")
print(f"📍 Local: {final_mix}")