# 介面規格（引擎 CLI、後端 HTTP API、平行辨識）

樂譜音名辨識服務的介面規格。UI 語言：繁體中文（台灣）。本 repo 不含網頁前端程式碼，前端相關章節已移除；後端 API 與引擎 CLI 以本文件為準。

## 目錄

```
omr/            OMR 引擎轉接層：omr/run_engine.py（譜號、臨時記號判讀與範本）
server/         FastAPI 後端：認證、額度、OMR 佇列、匯出
deploy/         Dockerfile、相依鎖定、模型雜湊、compose 範例、Cloud Run 部署腳本
web/fonts/      匯出用的升降記號字型子集（SIL OFL 1.1）
docs/           本規格
```

## 1. OMR 引擎 CLI

每個引擎可有自己的 venv，後端以子程序呼叫，避免依賴衝突：

```
<engine-python> omr/run_engine.py --engine <oemer|homr|mock> --image <page.png> --out <result.json>
```

- 成功 exit 0 並寫出 result.json；失敗 exit≠0，stderr 寫原因。
- `omr/engines.json`：`{"oemer": {"python": "<venv python 絕對路徑>", "dpi": 300}, ...}`，後端讀它決定 python 路徑與 PDF 點陣化 dpi。`mock` 引擎用任何 python 都能跑（無外部依賴），回傳幾個假音符，供開發。

單頁 result.json：

```json
{
  "engine": "oemer", "engine_version": "x.y.z",
  "width_px": 2480, "height_px": 3508,
  "staves": [{"id": "s0", "index": 0, "clef": "G2", "clef_name": "treble", "clef_confidence": 0.97,
              "clef_source": "both", "clef_changes": [], "key_fifths": -1, "bbox": [x, y, w, h]}],
  "notes": [{"id": "n0", "staff": "s0", "staff_index": 0, "bbox": [x, y, w, h],
             "step": "B", "alter": -1, "octave": 4, "confidence": 0.82,
             "clef": "G2", "staff_pos": 4,
             "stem": [x, y, w, h], "extent": [x, y, w, h]}],
  "elapsed_ms": 41234,
  "warnings": ["..."]
}
```

- bbox = 符頭在**輸入影像像素座標**的外框（不是整個音符含符桿）。只列有音高的音符；休止符不列。
- `alter`：-2..2，代表**實際音高**（已套用調號與臨時記號）。引擎（`omr/accidentals.py` 的 `AlterState`）的規則，已配對與 fallback 符頭共用同一份狀態：調號 → 符頭印出的臨時記號（升／降／還原／重升／重降；來源是 transformer 的 lift，且其音名須與幾何讀到的音名相同，否則忽略；transformer 沒給時才用符頭左側的字形偵測：還原 IoU ≥ 0.65，其他 ≥ 0.8）→ 同小節同音名同八度往後沿用 → 過小節線重置（連結線連到下一小節同音者保留）。還原記號會取消調號（E♮ 不會變 E♭）。環境變數 `OMR_HOMR_ALTER=old` 可切回修正前的行為（僅供基準比對）。`key_fifths`：調號（-7..7）。
- `clef`：`G2` `F4` `C3` `C4` 等；未知用 `null`。
- **譜號（四種必須支援）**：高音 treble = `G2`、低音 bass = `F4`、中音 alto = `C3`（第 3 線）、次中音 tenor = `C4`（第 4 線）；homr 詞彙另有 `G1 F3 F5 C1 C2 C5`，照實輸出，音高同樣由 `staff_pos` 算。
  - `staves` 依閱讀順序（由上而下、同列由左而右）排序，`id` = `s<index>`；`staves[].index` = 該頁第幾條譜表（0 起算）。大譜表的上、下譜表各是一條。
  - `staves[].clef` = 這條譜表**行首**的譜號；`clef_name` = `treble|bass|alto|tenor`（四種以外為 `null`）；`clef_confidence` 0..1（模型與譜號字形偵測器一致 ≥ 0.6；只有模型、字形無法佐證 0.4–0.5；沒讀到而用預設 0.2；沒有資料給 `null`）；`clef_source` = `both|model|detector|default`。
  - `staves[].clef_changes` = 行中換譜號：`[{"x": 像素, "clef": "C4"}]`，x 之後（含）的符頭用新譜號；沒有換為 `[]`。
  - `notes[].staff_index` = 所屬譜表的 `index`；`notes[].clef` = 該符頭生效的譜號（有行中換譜號時才與譜表行首不同）。
  - **`notes[].staff_pos`**（整數）= 符頭位置，以**半個譜線間距**為單位、從**最下面那條線**起算：0 = 第 1 線、1 = 第 1 間、2 = 第 2 線、…、8 = 第 5 線（頂線）、−1／−2 = 下加一線的間／線、9／10 = 上加一線的間／線。**它與譜號無關**，是引擎量到的幾何位置。
  - 音高 = 該譜號「底線音」往上數 `staff_pos` 個音級（音級 = 自然音階的一步，不是半音）。底線音：`G2` E4、`F4` G2、`C3` F3、`C4` D3（其餘：`G1` G4、`F3` B2、`F5` E2、`C1` C4、`C2` A3、`C5` B2）。`step/octave` 與 `clef`＋`staff_pos` 永遠互相一致（`step/octave` 是它們的結果）。
  - `alter` 不隨譜號重算：升降記號畫在符頭上，與譜號無關；調號的影響（`key_fifths`）以字母決定，換譜號後字母變了，要重新套調號。
- `confidence`：0–1；引擎沒有就給 `null`。
- `stem`（選填，向下相容）：這個符頭的符桿外框 `[x, y_top, w, h]`，輸入影像像素。符尾／連結線的末端近似為符桿末端（外框含到連結線厚度，不含符尾橫向外伸）。和弦：同一根共用符桿，每個符頭都給**整根**符桿外框（含其他符頭那段）。兩聲部各有自己的符桿。量過但沒有符桿（全音符等）給 `null`；**欄位不存在**＝引擎沒量，前端退回估計。
- `extent`（選填）：符頭＋符桿的聯集外框 `[x, y, w, h]`；有 `stem` 才有。
- **譜號判定**（homr）：模型的行首譜號 token＋行首字形偵測器（`omr/clefs.py`：把譜表行首裁成與譜線間距無關的二值圖、去掉譜線，與高音／低音／中音／次中音樣板做正規化互相關；信心 = 0.6×最高分 + 0.8×與第二名差距，最高分 < 0.35 減半）。一致 → `clef_source=both`；不一致且偵測信心 ≥ 0.5 → 用偵測器（`detector`，並加 warning）；否則維持模型（`model`，信心 0.4）。行中換譜號用模型 token，x 邊界取「舊譜號下最後一個與模型音符完全吻合的符頭」與下一個符頭的中點。oemer 只有 G／F，另跑同一偵測器並由 `staff_pos` 重算。
- **進度標記**（stderr，向下相容）：引擎在 stderr 另寫獨立一行 `@@progress {"stage":"detect","p":0.4}`。`stage` ∈ `detect`（讀圖、分割、找譜表）｜`transcribe`（transformer 逐譜表讀音，每讀完一個譜表一行）｜`post`（比對、符桿量測）｜`done`（該頁引擎結束）；`p` = 0..1，是**該 stage 內**的進度。stdout、result.json、exit code 不變；其他行照舊是一般 log，不認得這種行的消費者直接忽略即可（失敗原因取最後一行非標記行）。`mock`（`run_engine.py` 與 `server/app/mock_engine.py`）會以小睡眠送同樣的標記（`OMR_MOCK_DELAY_MS`／`MOCK_ENGINE_DELAY_MS` 控制總時長）；oemer 只有粗略標記。
- 量測方法（`omr/run_engine.py detect_stems`，homr／oemer 通用）：在輸入影像（Otsu，髒底圖再與局部門檻取交集）上，從符頭左右邊緣附近數個欄位出發，沿垂直方向追暗像素（每列可偏 ±1 px、總偏移 ≤ 0.25 間距、容許 1 px 斷裂），超出符頭 ≥ 1.5 間距且 ≤ 7.5 間距者為符桿；寬度取符桿段上最窄的水平暗段。環境變數 `OMR_STEMS=0` 關閉。失敗只加 warning，不讓整頁失敗。


## 2. 後端 HTTP API

本機：`http://localhost:8787`。所有 `/api/*` 需登入（session cookie），否則 401 `{"error":"unauthenticated"}`。

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/api/me` | `{"user":{"id":12,"name":"王小明"},"quota":{"limit":3,"used":1,"remaining":2,"date":"2026-11-06"},"limits":{"max_upload_mb":30,"max_pages":10,"retention_hours":24,"est_seconds_per_page":60},"dev":true}`。`limits` 讓前端顯示空狀態說明、上傳前檢查大小、估算等待時間（缺欄位時前端用預設值）；`est_seconds_per_page` 來自環境變數 `OMR_EST_SECONDS_PER_PAGE`（預設 60） |
| GET | `/auth/login?next=/` | 轉到上游登入交接網址 |
| GET | `/auth/callback?token=…` | 驗證簽章 token，設 session cookie，轉回 next |
| POST | `/auth/logout` | 清 session |
| GET | `/auth/dev-login?name=測試者&quota=3` | 僅 `DEV_AUTH=1` 時存在：不經上游直接登入 |
| POST | `/api/omr` | multipart `file`（pdf/png/jpg/jpeg/webp），選填 `engine`。回 202 `{"job_id":"…","cached":false}`；額度用完 429 `{"error":"quota_exceeded","quota":{…}}`；檔案過大 413；格式不支援 415；頁數超過 422 |
| GET | `/api/omr/{job_id}` | `{"status":"queued|running|done|error","progress":{"page":1,"pages":3,"stage":"transcribe","fraction":0.42,"queue_position":0,"eta_seconds":35},"error":null,"result":{…}}`（`progress` 欄位見下） |
| GET | `/api/omr/{job_id}/page/{i}.png` | 該頁點陣圖（前端預覽底圖） |
| POST | `/api/export` | 見下 |

`progress`（輪詢用，欄位一律存在）：

- `page`、`pages`：目前處理第幾頁（1 起算）／總頁數。
- `stage`：`queued`｜`render`（PDF 點陣化／圖片正規化）｜`detect`｜`transcribe`｜`post`｜`done`。
- `fraction`：0..1 浮點，整份工作的總進度，**單調不減**；執行中上限 0.99，只有 `done` 才是 1.0。由頁序加頁內權重算出：render 0.03、detect 0.50、transcribe 0.38、post 0.09（homr 實測 piano_grand 300dpi：載入＋分割約 6.4 s、逐譜表讀音約 4.5 s、後處理約 0.3–2.5 s），每頁等權。
- `queue_position`：前面還有幾個工作（執行中＋較早排入的 queued）；status=running／done／error 為 0。
- `eta_seconds`：整數秒或 `null`。fraction ≥ 0.1 時用 已耗時×(1−f)/f，否則用 `est_seconds_per_page`×頁數×(1−f)；queued 時是 `est_seconds_per_page`×(本工作頁數＋前面工作頁數)；done 為 0，error 為 `null`。
- 進度存在後端記憶體；後端重啟後的 error 工作 `stage`＝`queued`、`fraction`＝0。

`result`（status=done）：

```json
{
  "source": {"type": "pdf", "filename": "x.pdf", "page_count": 2},
  "engine": "oemer",
  "pages": [{
    "index": 0, "image_url": "/api/omr/<job>/page/0.png",
    "width_px": 2480, "height_px": 3508,
    "pdf_width_pt": 595.3, "pdf_height_pt": 841.9,
    "staves": [...], "notes": [...], "elapsed_ms": 41234, "warnings": []
  }],
  "warnings": []
}
```

`notes[].id` 在整份 result 內唯一（後端加前綴 `p{頁}`，例如 `p0n12`、`p0s0`；`staff` 欄位同步加前綴）。圖片來源時 `pdf_*_pt` 為 null。

`POST /api/export`（JSON）：前端算好最終文字與位置，後端只負責畫，PDF 來源保留原 PDF 向量內容與頁面尺寸：

```json
{
  "job_id": "…", "format": "pdf", "page": null,
  "style": {"font_size_px": 28, "color": "#d0021b"},
  "labels": [{"page": 0, "x": 1203.5, "y": 880.0, "text": "Do", "dots_above": 0, "dots_below": 1, "font_size_px": 19.6}]
}
```

- x,y：該頁像素座標（與 width_px/height_px 同一座標系），代表文字**中心**；後端換算成 PDF pt。
- `labels[].font_size_px`（選填，> 0、≤ 2000）：這個標記自己的字級（前端為了不壓到鄰近譜表而局部縮小的列）；缺省用 `style.font_size_px`。
- text 只用 ASCII（`#`、`b`、字母、數字）。`dots_above/below`：簡譜高低八度點數。
- `labels[].parts`（選填，最多 4 段）：含升降記號的標記的分段排版。`{kind:"text", text, x}`（x = 文字段**中心**）或 `{kind:"acc", name:"flat|natural|sharp|doubleSharp|doubleFlat", x, size, dy}`（x = 字形**左緣**、dy = 字形基線相對標記中心 y；x／size／dy 一律以該標記字級為單位，所以與字級無關）。有 `parts` 時後端用 `web/fonts/SPMAccidentals.ttf`（Bravura 子集，SIL OFL 1.1，SMuFL 碼位 U+E260–E264）畫升降記號、Helvetica 畫文字段；沒有 `parts` 就照舊畫整串 `text`。`text` 仍是 ASCII，供無障礙與備援。
- `format=png` 時必須給 `page`，回單頁 PNG；`format=pdf` 回整份。


## 3. 認證與額度

- 兩種模式：
  - **一般模式**：以 `/auth/login` 轉到上游網站的登入交接，callback 驗證簽章 token（HMAC-SHA256，`server/app/tokens.py`；secret 由環境變數 `SPM_TOOLS_AUTH_SECRET` 提供，**不進 git、不寫 log**）後設定 session cookie。`DEV_AUTH=1` 時另有 `/auth/dev-login` 供本機開發。
  - **服務模式**（設了 `SERVICE_KEY`）：只信任帶正確 `X-SPM-Service-Key`、`X-SPM-User-Id`、`X-SPM-User-Name` 標頭的請求（由上游網站代理），不提供登入、session 與靜態檔；額度由上游負責。
- 每個使用者每日可提交的辨識工作數由上游傳入（token 的 `quota`）或開發模式的 `DEV_DAILY_QUOTA` 決定。一份 = 一次成功的 PDF／圖片辨識工作（多頁 PDF 算 1 份；頁數上限 `MAX_PAGES`）。
- 同一使用者、同一天、同一檔案（sha256）重送 → 回快取結果，不重複扣。辨識失敗不扣。
- 日界線：Asia/Taipei。

## 5. Cloud Run 平行辨識（dispatcher + 單頁 worker）

同一個映像（`deploy/Dockerfile`）兩種模式；對前端的 API（第 2 節）完全不變。部署見 `deploy/cloudrun/README.md`。

**Worker**（`WORKER_MODE=1`，`server/app/worker.py`）：只有兩個端點，無 job store、無登入、無靜態檔。

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/healthz` | `{"ok":true,"mode":"worker"}`，不需金鑰 |
| POST | `/api/page` | 同步：multipart `file`（**只收 PNG**，單頁；上限 `WORKER_MAX_IMAGE_MB` 預設 25、像素 ≤ 40M）＋選填 `engine`、`index`（頁序，預設 0，用來加 `p{index}` id 前綴）、`pt_w`／`pt_h`（PDF 頁寬高 pt）。辨識在請求內完成後回 200，內容就是第 2 節 `result.pages[]` 的單頁物件（`image_url` 為 null，由 dispatcher 補）。 |

驗證：標頭 `X-SPM-Worker-Key` 與環境 `WORKER_KEY` 常數時間比對，缺或錯 → 401 `{"error":"unauthorized"}`。錯誤：400 `unknown_engine`／`bad_index`、413 `file_too_large`／`image_too_large`、415 `png_only`、422 `empty_file`／`invalid_image`／`engine_failed: …`（引擎對這頁確定失敗，重試無用）、504 `engine_timeout`、500 `internal_error`。每個實例一次只處理一頁。

**Dispatcher**（dispatcher 服務，設了 `REMOTE_WORKER_URL`）：工作把頁面點陣化後，每頁一出圖就丟給 worker（全域同時最多 `REMOTE_MAX_PARALLEL` 個請求），依頁序組回 `result`，格式與本機路徑相同。每頁：網路錯誤／逾時／5xx／429 重試一次，仍失敗（或 4xx 例如金鑰不符）改在 本機辨識（`local_sem`，本機同時只跑 1 頁）；worker 回 `engine_failed` 則整份工作失敗（與本機失敗同訊息，不退回）。未設 `REMOTE_WORKER_URL` 時行為與第 2 節原本完全一致。

進度（遠端路徑）：stage 先 `render`（逐頁點陣化）再 `transcribe`；`fraction = 0.03×已點陣化頁數/總頁數 + 0.96×已完成頁數/總頁數`，單調不減、執行中 ≤ 0.99；`page` = 已完成頁數 + 1（上限總頁數）。`queue_position` = max(0, 前面的工作數 − `OMR_CONCURRENCY` + 1)（並行 1 時與原定義相同）；`eta_seconds` 在遠端模式以 ceil(頁數/`REMOTE_MAX_PARALLEL`) 頁計。

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `WORKER_MODE` | off | `1` = worker；需要 `WORKER_KEY`，且不可同時設 `REMOTE_WORKER_URL` |
| `WORKER_KEY` | — | ≥ 32 字元；worker 與 dispatcher 共用，只放環境變數／機密管理服務 |
| `REMOTE_WORKER_URL` | 空 = 停用 | worker 根網址；設了就需要 `WORKER_KEY` |
| `REMOTE_MAX_PARALLEL` | 20 | 全域同時送出的頁數上限 |
| `REMOTE_TIMEOUT_S` | 240 | 單次 worker 請求的 socket 逾時（秒） |
| `OMR_CONCURRENCY` | 1；設了 `REMOTE_WORKER_URL` 時 8 | 同時執行的工作數 |
| `WORKER_MAX_IMAGE_MB` | 25 | worker 單頁上傳上限 |

**每月免費額度保護**（dispatcher，`server/app/jobs.py`、`store.py` 的 `remote_usage` 表）：

- 記帳：每次 worker 請求（成功、失敗、重試都算）的 dispatcher 端牆鐘秒數（含冷啟動，偏保守）× `REMOTE_VCPU` 加進當月 vCPU-秒、× `REMOTE_MEM_GIB` 加進 GiB-秒。月份 = `America/Los_Angeles` 時區的 `YYYY-MM`（GCP 帳單月）。存在 sqlite，重啟不歸零。
- 上限規則：`cap_v = REMOTE_CAP_RATIO × REMOTE_FREE_VCPU_SECONDS`、`cap_g = REMOTE_CAP_RATIO × REMOTE_FREE_GIB_SECONDS`。送出每一頁（含重試）前先原子預留估計值 `REMOTE_EST_PAGE_SECONDS`（預設 60）× 規格，**只有 `用量 + 估計 ≤ cap` 且 vCPU、GiB 兩者都成立時才送**；請求結束後以實測值取代估計（補差額）。因此 20 頁同時在途也不會讓已預留總量超過 cap。不能預留的頁面直接在 本機辨識（受 `local_sem` 限 1 頁），當月第一次觸頂記一行 `remote cap reached`，下個帳單月自動恢復。
- 觸頂後新工作一次只跑 1 份（`capped_gate`，仍是 `queued` 直到輪到）；`queue_position`、`eta_seconds` 以並行 1、整份工作本機序列計算。
- 服務模式的 `GET /api/me` 多一個 `remote`：`{"month":"2026-11","vcpu_seconds":123.4,"gib_seconds":123.4,"cap_ratio":0.8,"capped":false}`；未設 `REMOTE_WORKER_URL` 為 `null`。`capped` = 再多一頁的估計值就放不下。

| 環境變數 | 預設 | 說明 |
|---|---|---|
| `REMOTE_VCPU` / `REMOTE_MEM_GIB` | 2 / 2 | worker 規格（要和 `deploy.sh` 的 `--cpu`／`--memory` 一致） |
| `REMOTE_FREE_VCPU_SECONDS` / `REMOTE_FREE_GIB_SECONDS` | 180000 / 360000 | 每月免費額度 |
| `REMOTE_CAP_RATIO` | 0.8 | 用到免費額度的幾成就停用雲端 |
| `REMOTE_EST_PAGE_SECONDS` | 60 | 預留用的單頁估計秒數 |
