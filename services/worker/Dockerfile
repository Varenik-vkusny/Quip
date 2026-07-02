# Quip worker — деплой на always-on CPU-контейнер (Railway / Render / Fly).
# Порт образа Modal (deploy/modal/worker.py): статик-ffmpeg >=7 + Deno + bgutil PO-token +
# рантайм-либы MediaPipe/opencv + CPU-torch. Стейт — Supabase, медиа — R2 (задаётся env'ом).
#
# ВАЖНО: MODAL_SPAWN НЕ задаём → app.dispatch.modal_spawn_enabled() = False → create_job идёт
# через FastAPI bg.add_task (джоба в процессе, в threadpool), а НЕ через Modal .spawn(). Модуль
# `modal` в рантайме не импортируется (ленивый импорт только в spawn-ветке), в образ не ставим.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/root

# 1) Рантайм-либы MediaPipe/opencv (GLES/EGL) + утилиты установки (wget/xz/unzip/git/ca-certs).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgl1 libglib2.0-0 libegl1 libgles2 libglvnd0 libsm6 libxext6 \
      wget xz-utils unzip ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# 2) Deno — JS-рантайм для yt-dlp (YouTube nsig/«n»-челлендж). yt-dlp ищет deno на PATH.
RUN wget -q https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -O /tmp/deno.zip \
    && unzip -o /tmp/deno.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/deno \
    && rm /tmp/deno.zip \
    && /usr/local/bin/deno --version

# 3) bgutil PO-token provider v1.3.1 (SCRIPT mode, Deno). Клон + resolve/cache deps на build-time
#    (рантайм генерит токен без сетевых фетчей). Пин тега = pip-плагин (см. ниже) той же версии.
RUN git clone --depth 1 --single-branch --branch 1.3.1 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas \
    && deno cache src/generate_once.ts \
    && rm -rf /opt/bgutil-ytdlp-pot-provider/.git \
    && ls /opt/bgutil-ytdlp-pot-provider/server/src/generate_once.ts

# 4) СТАТИК ffmpeg >=7 (John Van Sickle) в /usr/local/bin — НЕ apt-ffmpeg (apt 5/6 крашит
#    crop-рендер: «Parsed_crop_4: Failed to configure input pad»). /usr/local/bin в PATH раньше /usr/bin.
RUN wget -q https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -O /tmp/ffmpeg.tar.xz \
    && cd /tmp && tar xf ffmpeg.tar.xz \
    && cp /tmp/ffmpeg-*-static/ffmpeg /tmp/ffmpeg-*-static/ffprobe /usr/local/bin/ \
    && chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe \
    && rm -rf /tmp/ffmpeg* \
    && /usr/local/bin/ffmpeg -version | head -1

# 5) torch — CPU-колесо (ASD active-speaker). Отдельный индекс, отдельный слой.
RUN pip install "torch>=2.2" --index-url https://download.pytorch.org/whl/cpu

# 6) Рантайм-депы — ЗЕРКАЛО deploy/modal/worker.py (проверенный боевой набор; anthropic/deepgram-sdk
#    НЕ нужны: прод = Deepgram через httpx + Gemini через google-genai).
RUN pip install \
      "fastapi>=0.136.3" \
      "uvicorn[standard]>=0.49.0" \
      "python-multipart>=0.0.32" \
      "python-dotenv>=1.0.1" \
      "pyjwt[crypto]>=2.10.1" \
      "pydantic>=2.13.4" \
      "pydantic-settings>=2.14.1" \
      "httpx>=0.28.1" \
      "mediapipe>=0.10.35" \
      "scenedetect>=0.6.4" \
      "numpy>=2.4.6" \
      "scipy>=1.11" \
      "python_speech_features>=0.6" \
      "google-genai>=2.8.0" \
      "yt-dlp[default]>=2026.3.17" \
      "bgutil-ytdlp-pot-provider==1.3.1" \
      "boto3>=1.35" \
      "google-cloud-run>=0.10"

# 7) Код + шрифты в /root (PYTHONPATH=/root → import app.*). Копируем ПОСЛЕДНИМ, чтобы правки кода
#    не инвалидировали тяжёлые слои зависимостей выше. (Совпадает с layout Modal: /root/app, /root/fonts.)
COPY app /root/app
COPY fonts /root/fonts

# Рантайм-env (НЕ секреты — совпадает с .env() образа Modal). Секреты/ключи задаются в Railway env
# (Supabase / R2 / Deepgram / Gemini / Polar) — см. чек-лист в PR/сообщении. R2_PUBLIC_URL можно
# переопределить env'ом Railway при необходимости.
ENV YTDLP_COOKIES_BROWSER="" \
    YTDLP_POT_SERVER_HOME=/opt/bgutil-ytdlp-pot-provider/server \
    R2_PUBLIC_URL=https://cdn.quip.ink

# Railway/Render/Fly дают $PORT. Bind 0.0.0.0. Один uvicorn-воркер: фон-джоба (bg.add_task) идёт в
# threadpool + ffmpeg-subprocess, поэтому HTTP-эндпоинты (статус/новые джобы) не блокируются.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
