# 每日 liveprocessor H.264 轉碼時長

使用 Python 3.8+ 標準函式庫，透過 `DescribeLiveTranscodeTotalInfo` 查詢每日用量，
只加總 `ModuleCodec == "liveprocessor_H264"` 的 `Duration`（整數分鐘）。

**這是普通 H.264 轉碼累計時長，不是觀眾觀看時長。** 不同轉碼輸出的時間分別累加，
所以一天可以超過 1,440 分鐘；共用同一輸出的觀眾不會各自增加一份轉碼分鐘數。
API 不包含模板名稱／ID，報表代表此編碼分類，不保證等於名為 `h264` 的特定模板。

## 檔案職責

| 檔案 | 職責 |
| --- | --- |
| `api_client_h264.py` | 指定環境變數、TC3 簽章、單日總量 API 查詢 |
| `daily_report_h264.py` | CLI、逐日查詢、報表主流程及輸出 |
| `utils_tools_h264.py` | 日期與時間驗證、回應檢查、解析度分類與分鐘數累計 |

原日誌估算流程已移除，不保留舊 CLI、工具或轉接別名；此工具不下載播放日誌，
不使用 response time、Online、觀看人數或連線起訖相減來推算時長。
`DescribeLiveTranscodeDetailInfo` 不是統計來源，僅需追查來源流時才另行查詢，不能與總量相加。

## 使用方式

在專案根目錄執行：

```bash
export TENCENT_SECRETID='你的 SecretId'
export TENCENT_SECRETKEY='你的 SecretKey'

# 單日完整時段。
python3 daily_liveprocessor_duration/daily_report_h264.py \
  --start-time "2026-10-01 00:00:00" \
  --end-time "2026-10-01 23:59:59"

# 跨日部分時段：仍按天統計；stdout 可由執行者自行保存。
python3 daily_liveprocessor_duration/daily_report_h264.py \
  --start-time "2026-10-01 09:00:00" \
  --end-time "2026-10-07 12:00:00" \
  > daily_liveprocessor_duration/report.json
```

也支援 `python3 -m daily_liveprocessor_duration.daily_report_h264`，參數相同。
程式本身不建立或覆寫 JSON 檔案；shell 的 `>` 會覆寫指定檔案，請自行選擇檔名。
只讀取上述兩個憑證環境變數，沒有其他相容名稱。

### 選填條件

```bash
python3 daily_liveprocessor_duration/daily_report_h264.py \
  --start-time "2026-10-01 00:00:00" \
  --end-time "2026-10-07 23:59:59" \
  --push-domains 'tencent-global-push-rtmp.17app.co' \
  --mainland-or-oversea Oversea
```

- `--push-domains`：逗號分隔的**推流**域名，不是播放域名；省略查全部。
- `--mainland-or-oversea`：`Mainland` 境內、`Oversea` 境外；省略查全球。
- 起訖時間格式固定為 `YYYY-MM-DD HH:MM:SS`，按台北時間（UTC+8，與北京時間相同），
  含空白的參數須加引號。不接受只有日期、ISO `Z` 或其他時區格式。
- `--start-time` 不可晚於 `--end-time`，結束時間不可晚於現在；可以查今天已過去的時段，
  但近期資料可能尚未完整。原 `--start-date`、`--end-date` 已移除。
- 官方支援近三個月資料；保留範圍以騰訊端驗證為準，太舊的查詢會報錯，不當成零。

## 每日分類與輸出

每一天固定按以下順序列出五類，即使該解析度沒有資料也補 `0`：

```text
liveprocessor_H264_480P
liveprocessor_H264_720P
liveprocessor_H264_1080P
liveprocessor_H264_2K
liveprocessor_H264_4K
```

`Resolution` 以大寫分類，例如 `2k`／`2K` 合併到 `2K`。
`topspeed_H264`、`topspeed_H265`、`liveprocessor_H265` 等其他編碼不列入加總。
`Duration` 已是分鐘，不再乘以 5 或 60，也不依桶寬再次換算。

報表外層包含：

- `SourceAPI`、`Metric`、`Unit`、`DayTimezone`：來源、統計定義、單位及台北時區。
- `StartTime`、`EndTime`：使用者指定的起訖時間，格式與 CLI 相同，時區由 `DayTimezone` 標示。
- `ModuleCodec`、`PushDomains`、`MainlandOrOversea`：本次條件；`PushDomains: null` 代表全部，
  地域 `Global` 代表未向 API 指定地域。
- `Completeness: "not_verified"`、`Warnings`：未獨立證實全天資料完整性，不等同計費對帳。
- `Daily`：按日期遞增排列的每日列，各列包含五類分鐘數及以下欄位：
  - `Date`、`TotalDurationMinutes`：日期與五類總和。
  - `StartTime`、`EndTime`：該日實際送給 API 的查詢時段。
  - `PartialDay`：不是 `00:00:00–23:59:59` 完整日期時為 `true`，不能當成全天用量。
  - `DataStatus`：非空回應為 `data_returned`；空清單為 `no_data`。
  - `ReturnedDataPoints`、`MatchedDataPoints`：回傳／符合編碼的**統計桶筆數**，不是流數或觀眾數。
  - `RequestId`：該日查詢的騰訊 RequestId。

查詢回應為空時五類仍列零，但明確標示 `no_data` 並警告，不能解讀為已確認無用量。
若有其他編碼資料、但沒有普通 H.264，則為 `data_returned` 且五類為零。
非空回應也不代表資料已完整或不會被騰訊修正；程式不自行以桶數補出缺失時長。

## 跨日與時間桶

每天獨立送出一次查詢，首尾日保留指定時間，中間日期查詢 `00:00:00–23:59:59`。
例如 `2026-10-01 09:00:00` 到 `2026-10-02 12:00:00` 會查兩次：

- 10 月 1 日：`09:00:00–23:59:59`。
- 10 月 2 日：`00:00:00–12:00:00`。

起訖時間都會送給 API；若結束時間恰為隔日 `00:00:00`，該日也會查詢該時間點。
不拆成小時查詢，也不讓相鄰日期共用隔日 `00:00:00` 的查詢端點。
無域名條件的同日查詢通常為五分鐘桶；指定推流域名為一小時桶，累計方式相同。
部分時段的邊界如何對齊統計桶由騰訊決定，程式不依分鐘或秒數按比例切割 `Duration`，
也不宣稱任意秒級查詢能得到秒級精度；部分日期會在報表中加上警告。

轉碼跨午夜時，使用騰訊已分桶的 `Duration`，不把整條連線歸入開始日，也不自行切割連線。
依 API `Time` 的台北日期檢查歸屬；有 ISO 時區的回應先轉成 UTC+8。
回應不需要依時間排序；若包含其他日期的時間點則停止，不默默丟掉或挪到另一日。

**實際午夜桶代表區間起點或終點仍待真實查詢驗證。** 上述是查詢與加總規則，
不宣稱已驗證跨午夜計費分配。正式核對時應比對相鄰兩日與相同範圍的跨日總量；
跨日查詢的桶寬可能不同，因此以相同編碼／解析度總量核對，不直接比較每個時間點。

## 錯誤處理

- stdout 僅輸出完整報表 JSON，進度與警告放 stderr。
- 日期／CLI 或憑證缺失：結束碼 `2`；網路、HTTP、API 或資料錯誤：結束碼 `1`。
- 成功查詢含 `no_data` 的報表：結束碼 `0`，仍需檢查狀態及警告。
- 任一天失敗即停止，stdout 不輸出先前日期的部分報表；不自動重試或使用舊快取。
- 缺欄位、未知的普通 H.264 解析度、無效時間、負數／非整數時長及重複的
  `Time + ModuleCodec + Resolution` 桶皆報錯，不自行猜測、去重或捨棄。
- 錯誤不列出 SecretId、SecretKey、簽章或原始 HTTP 回應內容。

## 離線測試

```bash
python3 -B -m unittest discover -s daily_liveprocessor_duration/tests -v
```

測試使用模擬 HTTP 回應，不使用真實憑證、不呼叫騰訊 API，也不改寫樣本。
現有 `sample/DescribeLiveTranscodeTotalInfo/result_total.json` 的五類總和為
`692 / 13129 / 1749 / 10 / 36`，合計 `15616` 分鐘。
此樣本僅涵蓋 `00:00–01:00` 的回傳時間點，只用於累計回歸測試，**不是全天報表**。

參考：[DescribeLiveTranscodeTotalInfo](https://cloud.tencent.com/document/api/267/59864)。
