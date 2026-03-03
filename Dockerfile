FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    python3 python3-pip python-is-python3 \
    git ffmpeg ca-certificates \
    libsndfile1 libportaudio2 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ml/processing/meow-seedvc

# 1) meow-seedvc 코드 복사
COPY . .

# 2) seed-vc 레포 clone (없을 때만)
#    ✅ 운영 안정성을 위해 커밋 핀 추천(선택)
ARG SEED_VC_REPO="https://github.com/Plachtaa/seed-vc"
ARG SEED_VC_COMMIT=""
RUN if [ ! -f "vc/seed-vc/inference.py" ]; then \
      mkdir -p vc && git clone --depth 1 ${SEED_VC_REPO} vc/seed-vc; \
    fi && \
    if [ -n "${SEED_VC_COMMIT}" ]; then \
      cd vc/seed-vc && git fetch --depth 1 origin ${SEED_VC_COMMIT} && git checkout ${SEED_VC_COMMIT}; \
    fi

# 3) 파이썬 의존성 설치
# torch는 여기서 한 번만 (CUDA 12.1에 맞는 wheel 인덱스)
RUN python -m pip install --no-cache-dir -U pip setuptools wheel && \
    python -m pip install --no-cache-dir \
      torch==2.4.0 torchvision==0.19.0 torchaudio==2.4.0 \
      --index-url https://download.pytorch.org/whl/cu121 && \
    python -m pip install --no-cache-dir -r requirements.seedvc.notch.txt