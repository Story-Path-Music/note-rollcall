# Cloud Run 單頁辨識 worker（選用的突發擴充）

主服務（dispatcher）設了 `REMOTE_WORKER_URL` 後，會把每份工作的每一頁平行送到 Cloud Run 上的 worker 辨識，頁面完成就更新進度；單頁連兩次失敗才退回本機辨識（本機退回一次只跑 1 頁）。協定見 `docs/CONTRACT.md` 第 5 節。不設 `REMOTE_WORKER_URL` 時，整個功能停用，行為等同純本機辨識。

## 部署

```
gcloud auth login
export GCP_PROJECT=<your-project-id>

# 1) 產生 worker 金鑰（至少 32 字元）並存進 Secret Manager；金鑰不要寫進 git、命令列或日誌
#    （secret 名稱預設 notes-worker-key，可用 WORKER_SECRET_NAME 覆寫）
# 2) 建置並部署（Cloud Build，不需本機 Docker）；印出服務 URL
bash deploy/cloudrun/deploy.sh
# 3) 在 dispatcher 的環境設定 REMOTE_WORKER_URL=<上一步印出的 URL> 與同一把 WORKER_KEY，重啟服務
```

可用環境變數覆寫：`GCP_PROJECT`（必填）、`GCP_REGION`（預設 `asia-east1`）、`CR_SERVICE`、`AR_REPO`、`WORKER_SECRET_NAME`。

Cloud Run 設定：`--cpu 2 --memory 2Gi --concurrency 1 --min-instances 0 --max-instances 20 --timeout 300`，請求制計費（CPU 只在請求進行時計費，所以辨識在請求內同步完成），`--ingress all --allow-unauthenticated`（dispatcher 沒有 Google 身分，驗證靠 `X-SPM-Worker-Key` 標頭，值來自 Secret Manager）。

## 免費額度保護

dispatcher 自己記帳，不依賴雲端帳單 API：每次 worker 請求量測牆鐘秒數（含冷啟動）× `REMOTE_VCPU` 加進當月 vCPU-秒、× `REMOTE_MEM_GIB` 加進 GiB-秒，存在本機 sqlite（月份以 America/Los_Angeles 計，與 GCP 帳單月一致）。送每一頁前先預留估計值（`REMOTE_EST_PAGE_SECONDS` × 規格），用量加估計超過 `REMOTE_CAP_RATIO` × 免費額度（`REMOTE_FREE_VCPU_SECONDS`／`REMOTE_FREE_GIB_SECONDS`）就不呼叫 worker，改本機辨識；下個帳單月自動恢復。目前用量看服務模式 `GET /api/me` 的 `remote` 欄位。這是 dispatcher 端的估算，仍建議在雲端帳號設預算警示。Cloud Run 規格若改，`REMOTE_VCPU`／`REMOTE_MEM_GIB` 要同步。實際費用與免費額度以 Google 官方公布為準。

## 回滾

1. dispatcher 環境移除（或清空）`REMOTE_WORKER_URL=` 並重啟，立即回到純本機單頁序列。
2. Cloud Run 服務可保留（沒有流量就不計費，min-instances 0），或用 `gcloud run services delete` 刪除。
3. 金鑰外洩：在 Secret Manager 新增版本、重新部署 worker、更新 dispatcher 的 `WORKER_KEY`。

## 每頁開銷與已知限制

- worker 每頁起一個 `omr/run_engine.py` 子程序（與本機相同路徑，沒有常駐載入模型）。
- 冷啟動（新實例拉映像＋第一次載入）會讓首批突發的頭幾頁較久；`REMOTE_TIMEOUT_S`（預設 240）逾時就重試一次，再退回本機。
- `REMOTE_TIMEOUT_S` 是 socket 逾時（連線／兩次資料之間），不是整體牆鐘；worker 端 `OMR_TIMEOUT_PER_PAGE_S=270` 加 Cloud Run `--timeout 300` 才是上限。
- worker 不存任何東西：頁面圖只在請求的暫存資料夾，回應後即刪；日誌不含影像。
