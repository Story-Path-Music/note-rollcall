# syntax=docker/dockerfile:1
# 樂譜音名辨識：OMR 後端（FastAPI 服務模式 + homr）。建置 context = repo 根目錄：
#   docker build -f deploy/Dockerfile -t notes-omr:1 .
# homr 模型在建置時下載並以 deploy/models.sha256 驗證（模型來源是 GitHub release 的可變標籤，檔名含版本雜湊；雜湊不符建置就失敗）。
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OMR_THREADS=4 DATA_DIR=/data PORT=8000

RUN apt-get update \
 && apt-get install -y --no-install-recommends libglib2.0-0 libgomp1 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY deploy/constraints.txt /tmp/constraints.txt
COPY server/requirements.txt /tmp/server-requirements.txt
RUN pip install -c /tmp/constraints.txt -r /tmp/server-requirements.txt homr psutil \
 && (pip uninstall -y pytest httpx || true)

# 模型：下載後驗證雜湊
COPY deploy/models.sha256 /tmp/models.sha256
RUN python -c "import homr.main as m; m.download_weights(False, False, False)" \
 && cd "$(python -c 'import site; print(site.getsitepackages()[0])')" \
 && sha256sum -c /tmp/models.sha256

# 程式：server/app、omr 轉接層（不含 venv、報告）、匯出用的升降記號字型
COPY server/app /app/server/app
COPY omr/run_engine.py omr/accidentals.py omr/clefs.py omr/accidental_templates.npz omr/clef_templates.npz /app/omr/
COPY deploy/engines.container.json /app/omr/engines.json
COPY web/fonts/SPMAccidentals.ttf /app/web/fonts/SPMAccidentals.ttf

RUN useradd --system --uid 10001 --home-dir /app app \
 && mkdir -p /data && chown app /data
USER app
VOLUME ["/data"]
WORKDIR /app/server
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','8000'), timeout=4)" || exit 1

# 同一個映像兩種模式：預設是主服務（需要 SERVICE_KEY）；WORKER_MODE=1 是 Cloud Run 單頁 worker（需要 WORKER_KEY，
# 只開 /healthz 與 /api/page，見 docs/CONTRACT.md 第 5 節）。啟動金鑰必須由環境提供；沒有就拒絕啟動。
# 模型已在建置時下載進映像；每頁一個 run_engine 子程序（載入模型約數秒，見 deploy/cloudrun/README.md）。
CMD ["sh", "-c", "if [ \"$WORKER_MODE\" = 1 ]; then test -n \"$WORKER_KEY\" || { echo 'worker key env is required' >&2; exit 1; }; else test -n \"$SERVICE_KEY\" || { echo 'service key env is required' >&2; exit 1; }; fi; exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port \"$PORT\" --no-server-header"]
