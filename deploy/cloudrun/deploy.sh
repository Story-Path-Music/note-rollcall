#!/usr/bin/env bash
# 建置並部署 Cloud Run 單頁辨識 worker（同一個映像、WORKER_MODE=1）。
#   bash deploy/cloudrun/deploy.sh [image-tag]
# 前提：gcloud 已登入且有專案權限；Secret Manager 已有 worker 金鑰（secret 名稱見 WORKER_SECRET_NAME，建立方式見 README.md）。
# 不需要本機 Docker：用 Cloud Build 建置。此腳本不會印出任何密鑰值。
set -euo pipefail

PROJECT="${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}"
REGION="${GCP_REGION:-asia-east1}"
SERVICE="${CR_SERVICE:-notes-worker}"
REPO="${AR_REPO:-notes}"
SECRET="${WORKER_SECRET_NAME:-notes-worker-key}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
TAG="${1:-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/${SERVICE}:${TAG}"

echo "== project=${PROJECT} region=${REGION} service=${SERVICE} image=${IMAGE}"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com --project "$PROJECT" >/dev/null

if ! gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION" \
    --project "$PROJECT" --description "OMR worker images" >/dev/null
fi

# Secret 必須已存在（provision-worker-key.sh 建立）；只檢查存在，不讀值
gcloud secrets describe "$SECRET" --project "$PROJECT" >/dev/null \
  || { echo "Secret ${SECRET} not found: create it first (see README.md)" >&2; exit 1; }

# Cloud Run 預設執行身分需要讀 secret
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
gcloud secrets add-iam-policy-binding "$SECRET" --project "$PROJECT" \
  --member "serviceAccount:${RUN_SA}" --role roles/secretmanager.secretAccessor >/dev/null

# Cloud Build：context = repo 根目錄，使用 deploy/Dockerfile（同一個映像；WORKER_MODE=1 只在執行時決定）
CFG="$(mktemp)"
trap 'rm -f "$CFG"' EXIT
cat >"$CFG" <<YAML
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-f", "deploy/Dockerfile", "-t", "${IMAGE}", "."]
images: ["${IMAGE}"]
timeout: 1800s
YAML
gcloud builds submit "$ROOT" --config "$CFG" --project "$PROJECT" --region "$REGION"

# 請求制計費（刻意不加 --no-cpu-throttling）：CPU 只在請求進行中計費，所以辨識必須在請求內完成（worker 是同步的）。
# 對外允許未驗證呼叫（dispatcher 用自己的金鑰，不是 Google IAM）；真正的驗證是 X-SPM-Worker-Key（WORKER_KEY 來自 Secret Manager）。
gcloud run deploy "$SERVICE" \
  --project "$PROJECT" --region "$REGION" --image "$IMAGE" \
  --cpu 2 --memory 2Gi --concurrency 1 --min-instances 0 --max-instances 20 --timeout 300 \
  --ingress all --allow-unauthenticated \
  --set-env-vars "WORKER_MODE=1,OMR_ENGINE=homr,OMR_THREADS=2,OMR_TIMEOUT_PER_PAGE_S=270,DATA_DIR=/tmp/data" \
  --set-secrets "WORKER_KEY=${SECRET}:latest" \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')"
echo "== deployed: ${URL}"
# Cloud Run 前端保留結尾為 z 的路徑（/healthz 會被 Google 回 404），改用「沒帶金鑰應回 401」確認服務活著
echo "== auth check (expect 401): $(curl -s -o /dev/null -w "%{http_code}" -X POST -H "X-SPM-Worker-Key: invalid-key-for-health-check-000000" -F "file=@/dev/null" "${URL}/api/page")"
echo "== dispatcher 端需要設定 REMOTE_WORKER_URL=${URL} 與同一把 WORKER_KEY，然後重啟服務"
