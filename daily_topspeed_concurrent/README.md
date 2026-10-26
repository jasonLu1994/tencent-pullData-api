# enhance 房間五分鐘採樣並發峰值

Python 3.8+，僅使用標準函式庫。以 **`DescribeLiveTranscodeDetailInfo`＋播放日誌**
計算 `enhance002`／`enhance003` × `720p`／`1080p` 四組房間峰值。

**新版本不再呼叫 `DescribeAllStreamPlayInfoList` 或 `DescribeStreamPushInfoList`。**
這是依轉碼明細及業務假設計算的五分鐘房間採樣值，不是實測在線連線數、獨立觀眾數、
連續時間最高並發或計費用量，不能與舊版數字視為相同口徑，也不能用於計費對帳。

## 已確認的業務假設

1. 有轉碼明細，就視為有人拉流而啟動轉碼；不再用 `Online > 0` 篩選。
2. 每筆明細的 `StartTime`～`EndTime` 直接視為**連續有效區間**，含兩端。
   唯一例外是分鐘精度且起訖相同的明細，視為完整一分鐘（含開始、不含下一分鐘）。
   `Duration` 單位為分鐘、代表實際轉碼累加時長；即使小於起訖時間差，也不扣除中斷，
   不依 `Duration` 縮短或延長區間。
3. 明細 `Resolution` 直接視為來源尺寸，不查推流尺寸、Sequence 或開播首筆品質紀錄。
4. 模板只由下載／本地日誌 URL 的 `_enhance002`、`_enhance003` 判斷。
   `ModuleCodec` 保留供稽核，不用它猜模板，也不以 `topspeed_H264` 篩選。

這些是假設，不代表 API 保證；亦會寫入報表 `BusinessAssumptions`。

## 使用方式

在專案根目錄執行，時間均為台北時間（UTC+8）：

```bash
export TENCENT_SECRETID='你的 SecretId'
export TENCENT_SECRETKEY='你的 SecretKey'

python3 daily_topspeed_concurrent/daily_report_enhance.py \
  --start-time "2026-10-14 00:00:00" \
  --end-time "2026-10-14 23:59:59" \
  --play-domains tencent-global-pull-rtmp.17app.co \
  --output daily_topspeed_concurrent/reports/report.json
```

範例日期須換成執行當時可查的日期。

- `--start-time`／`--end-time` 必填，格式固定為 `YYYY-MM-DD HH:MM:SS`，含首尾。
- 起訖至少相隔五分鐘，**最晚只能查到昨天**，不支援今天。
- 明細官方支援近一個月；CLI 依曆月回推限制開始日期，實際保留範圍仍由騰訊驗證。
  本地日誌或明細快取不繞過此限制。
- `--play-domains` 必填，逗號分隔，供日誌清單及日誌證據篩選；不能傳 URL。
- `--push-domain` 是單一**推流**域名，預設 `tencent-global-push-rtmp.17app.co`。
- 只讀 `TENCENT_SECRETID`／`TENCENT_SECRETKEY`，無相容憑證別名。
- 需要轉碼明細及日誌清單權限；本地日誌／既有清單不需要呼叫日誌清單 API。

### 本地日誌或既有清單

```bash
python3 daily_topspeed_concurrent/daily_report_enhance.py \
  --start-time "2026-10-14 10:00:00" \
  --end-time "2026-10-14 12:00:00" \
  --play-domains tencent-global-pull-rtmp.17app.co \
  --log-files /path/to/playback.log.gz
```

也可將最後一行換成 `--log-list /path/to/log-list.json`。

- `--log-files`／`--log-list` 各可接多個路徑，不能同時指定。
- 清單須為含 `Response`、`RequestId`、`LogInfoList`、`TotalNum` 的原始成功 JSON。
  下載 URL 必須仍有效。
- 本地檔支援純文字、gzip、ZIP、CLS JSONL，依內容而非副檔名辨識。
- 純文字沿用第 4 欄 URL，不用可能已去掉模板後綴的 stream ID 欄位。
  CLS JSONL 使用 `time`、`host`、`url`、`http_code`。
- 無時區日誌當作 UTC+8；有時區則轉換。ZIP 逐成員串流讀取，不解壓路徑到磁碟。
- 未指定來源時，直接呼叫 `DescribeLogDownloadList`；每次區間最多七天，較長區間自動切分。
  內部分段邊界為整點時，該次清單查詢提早一分鐘結束，下一段仍從原整點開始，
  例如 `10/01 00:00～10/07 23:59`、`10/08 00:00～…`，避免重複取得交界小時包。
  非整點邊界及最後一段不縮短；統計起訖不變，`QueryRanges` 記錄實際請求時間。
  同 URL 仍去重下載，不發送零長度請求。
- 日誌清單請求不向指定起訖外擴張；只下載與指定範圍相交的小時包，且只使用範圍內的行。
  若起點不是整點，API 可能漏回該小時套件，可自行提供既有清單或本地檔。
- 下載使用臨時檔，成功／失敗都清理，不傳 API Authorization，也不永久快取日誌或下載 URL。

## 統計與模板規則

### 1. 轉碼明細

- API 按台北日期逐日查詢，使用 `DayTime=yyyymmdd`，而非秒級 `StartTime`／`EndTime`。
  首尾日仍取得整日明細，再將計數區間及日誌證據裁切至使用者指定範圍。
- 僅採用 `Type == "Transcode"`；`MixStream`、`WaterMark`、`Webrtc` 等其他服務類型不計。
- 每頁 1000 筆，逐頁驗證頁碼、總數及總頁數。總量超過官方 100 頁上限時停止，
  不把截斷資料當作完整成功，也不輸出部分報表。
- 頁碼／筆數不符、查詢途中總數變動、重複整頁均停止。
- 完全相同的原始明細跨日去重，保留 `SourceDays`；不同地域或碼率的明細保留供稽核，
  最終房間數仍按 UUID／模板／採樣點去重。
- 明細時間可為 `YYYY-MM-DD HH:MM`，一般將秒數按 `00` 處理。
- **僅當起訖皆為分鐘精度且表示相同時間**時，將有效區間擴展為完整一分鐘：
  `12:05～12:05` 視為 `12:05:00 <= 時間 < 12:06:00`，包括小數秒但不含下一分鐘整點。
  同一有效區間用於日誌歸因與採樣，再裁切至使用者查詢範圍。
  不用 `Duration <= 1` 觸發擴展；秒級起訖相同或分鐘級起訖不同都維持原規則。
- 原始起訖及原始明細 ID 不變。報表以 `MinuteIntervalExpanded` 記錄是否擴展，
  `EffectiveStartTime`／`EffectiveEndTime` 記錄裁切後邊界，`EffectiveEndInclusive` 表示是否包含結束邊界。
  若查詢結束時間落在擴展分鐘內，裁切後仍包含使用者指定的結束時間。

### 2. 整筆明細模板歸因

使用**該筆明細起訖區間與指定查詢區間的交集內**，同 UUID、指定播放域名的 URL 證據。
不再使用每個採樣點前後五分鐘的視窗，不按最近一筆請求切換模板。

| 日誌證據 | 整筆明細的歸因 |
| --- | --- |
| 只有 enhance002 | 整段 enhance002 |
| 只有 enhance003 | 整段 enhance003 |
| 兩者都有 | 整段同時歸入兩種模板，兩組各計一次 |
| 只有原畫／h264 成功證據 | 排除該明細 |
| 無可用證據 | 缺模板，不猜測、不計數 |

- enhance／原畫／h264 一律只接受 HTTP **2xx** 作證據。
- 403／404 表示有請求但未觸發轉碼，不作模板歸因、原畫／h264 排除證據或雙模板告警依據；
  所有非 2xx 請求仍保留於 `StatusCounts` 及 `UnsuccessfulLines` 統計。
- 有轉碼明細但只有失敗請求、沒有可用成功證據時，維持 `missing`（缺模板），
  不計入 enhance002／enhance003 房間數，並依既有規則標記受影響採樣不完整；不刪除轉碼明細。
- 日誌重複請求只增加證據 `Count`，不增加房間數。
- 同 UUID 的不同明細各自歸因，不把不相交明細的模板互相延用。
- 分鐘擴展後仍無有效證據的明細維持 `missing`，不借用其他重疊明細的模板。
- 明細跨越查詢起訖時，只使用範圍內證據；因此縮小查詢範圍可能改變模板歸因。

### 3. 固定五分鐘告警

每筆雙模板明細計算 enhance002 與 enhance003 日誌之間的**最小時間差**：

- **≤ 300 秒**：發出一次 warning，含 UUID、DetailId、明細起訖、最近兩筆證據及時間差。
- **> 300 秒**：兩種模板仍套用整段，但不產生時間太近告警。
- 門檻固定五分鐘，**沒有 CLI 參數**。
- 日誌不需按時間排序；計算所有有效證據的最小差，不只比較首末請求。
- 每筆明細最多告警一次，不因跨日或多個採樣點重複告警。
- 告警不影響計數，也不單獨使 `Complete` 變成 false。

### 4. 五分鐘採樣與分桶

- 只取指定範圍內整五分鐘點：`00:00、00:05……23:55`。
  完整一天 288 點；`10:00–12:00` 含首尾為 25 點。
- 採樣點落在明細有效區間即有效；一般含兩端，擴展分鐘不含下一分鐘整點，
  依裁切後的 `EffectiveEndInclusive` 判定，不以 `Duration` 修正。
- 同 UUID、同模板、同採樣點最多一間，跨地域、碼率或重疊明細不重複計數。
- 尺寸字串支援 `寬*高`、`寬x高`、`寬×高`：短邊 `<=720` 歸 720p，所有 `>720` 歸 1080p。
- API 解析度級別亦可分組：正整數 `NP` 依 N 是否 `<=720` 分桶，例如 `360P／720P` 歸 720p，
  `900P／1080P／1440P` 歸 1080p；`2K／4K／8K` 亦歸 1080p，忽略大小寫。
- 同一 StreamName（房間仍依正規化 UUID 去重）在同一採樣點有不同解析度的有效 enhance 明細時，
  **發出 warning，取最大解析度分組，不再排除房間**。`480P＋720P` 計入 720p，
  `720P＋1080P` 計入 1080p；雙模板都採同一個最大解析度，各計一間。
- 最大值比較使用尺寸短邊或 `NP` 級別的 N；`2K／4K／8K` 以名義高度 `1440／2160／4320` 排序。
  尺寸短邊相同時再比較長邊。同桶但解析度不同仍警告，等價尺寸的格式差異不警告。
- 只比較已有 enhance 模板歸因的有效明細；缺模板明細不參與最大值選擇，仍標記缺模板。
- 無效／未知解析度維持缺失：若選中的 enhance 明細包含無效尺寸，該 UUID 仍不計數，
  不以其他已知解析度掩蓋缺失，並在採樣診斷中標示。
- 解析度衝突警告只作稽核，不單獨使 `Complete` 變成 false；相同重疊明細組合的警告彙整採樣時間，
  不在終端逐採樣點重複列印。明細組合改變時會產生另一筆警告。
- 各日四組獨立取最大值，保留所有並列峰值時間；各組不必同時達峰。
  雙模板可能各計一間，因此四組數字不能直接相加當作去重房間總數。

## 快取、並行與參數

| 參數 | 說明 |
| --- | --- |
| `--request-interval 0.1` | 全體 API worker 共用最短發送間隔，0.025～60 秒，重試亦計入 |
| `--transcode-workers 3` | 不同日期明細並行數，1～10；同日期按頁順序查詢 |
| `--log-workers 3` | 日誌下載並行數，1～10；解析順序不變 |
| `--transcode-cache-dir PATH` | 預設腳本 `.cache/transcode_details/` |
| `--no-transcode-cache` | 本次不讀寫明細快取，既有檔案保留 |
| `--refresh-transcode-cache` | 忽略本次日期的快取，成功取得完整快照後原子更新 |
| `--is-fast-live 0` | 日誌類型：0 標準直播、1 快直播；與 enhance 模板無關 |
| `--output PATH` | 完整 JSON 輸出位置；指定時成功才覆寫 |
| `--verbose` | 額外列印逐筆 warning（UUID／DetailId／WarningId 等）與轉碼明細分頁進度 |

- 明細快取**每個日期保存整組原始分頁回應**，不混用不同時間下載的頁面。
  當日所有頁完整且驗證成功才寫檔，失敗不保存半組快取。
- 快取鍵含 SecretId 的 SHA-256 指紋、主機、API 版本、Action、推流域名、日期及分頁大小。
  不保存明文 SecretId、SecretKey、Authorization 或簽章，不同帳號不共用。
- 日結未滿 24 小時或空回應保留五分鐘；其他歷史非空快照可持續沿用。
  近期快取到期後必須重查，不會自動變成永久有效。
- 損壞、過期或無效快照會重新查詢。快取與報表用同資料夾暫存檔原子替換，檔案權限 0600。
- `--no-transcode-cache` 與 `--refresh-transcode-cache` 互斥。
- 既有 `.cache/push_quality/` 及歷史 `reports/` 不刪除，也不讀取或遷移舊尺寸快取。
- HTTP 429／`RequestLimitExceeded` 會讓全體 API worker 降速及冷卻；每 100 個冷卻後成功回應
  才緩慢恢復。不保證設定速率符合其他程式共用帳號後的剩餘額度。
- HTTP 429／500／502／503／504、限流／內部錯誤及暫時性連線錯誤最多重試三次；
  認證、參數及資料格式錯誤不重試。日誌檔下載不新增重試。
- 直連時保留各執行緒 HTTPS keep-alive；代理環境沿用 urllib 代理路由。

## 報表與完整性

stdout 以表格顯示每日完整採樣點數、完成狀態、四組觀察峰值及不完整原因，
結尾彙總完整／不完整天數、按類型計數的 warning、總耗時及報表路徑。
`E003`／`E002` 分別代表 `enhance003`／`enhance002`；`-` 表示沒有採樣峰值，不是零。
非整日查詢會在該列註明實際時間範圍。各組獨立取峰值，不能直接相加。

stderr 預設保留日期查詢／下載進度、階段計時、API 重試／限流及錯誤，
不逐筆列出模板時間太近或解析度衝突告警。加上 `--verbose` 才列印逐筆 warning
及明細分頁進度；無論是否使用此參數，JSON 的所有 warning 明細及統計結果均不變。
CLI 長耗時使用 `2h 34m 12s`／`2m 48s`，JSON 仍保留秒數。不把完整 JSON 印到終端。

例如（數字僅供示意）：

```text
Observed sampled peaks (rooms; Taipei UTC+8; groups peak independently)
E003=enhance003, E002=enhance002; '-' means no sampled peak.
Date        Samples  Status      E003/720p  E003/1080p  E002/720p  E002/1080p  Notes
2026-10-19  288/288  COMPLETE          355          22        277           3
2026-10-20  288/288  INCOMPLETE        345          29        290           6  provisional logs (<24h after day end)
Days: 1 complete / 1 incomplete
Warnings: 12 total; full details are in the JSON report.
  7: Close enhance templates; both templates remain counted
  3: Overlapping resolutions; maximum selected; rooms remain counted
  1: Recent dates have provisional logs; PeakRooms is withheld until 24 hours after day end.
  1: Report inputs are incomplete; inspect Daily, Samples and TranscodeDetails.
Inputs incomplete/provisional: observed peaks may be lower bounds; inspect JSON. Not for billing.
Total elapsed: 2h 34m 12s
Full JSON report: .../reports/report.json
```

`288/288` 只代表每個採樣點的資料及分類可用，不代表日誌已過暫定期。
若 `Complete=False`，Notes 會列出暫定日誌、缺模板／尺寸、未知流名、空明細、
無日誌檔或解析失敗等適用原因。缺模板等數量以受影響的**採樣點數**計，
不是 UUID 數量；日誌檔缺失與解析失敗沿用既有的整份報告完整性判定。

未指定 `--output` 時，寫入腳本 `reports/topspeed_<起訖>_<唯一識別碼>.json`，不覆寫歷史報表。
指定輸出路徑時，只有完整產生 JSON 才原子替換；失敗保留原檔，不輸出前面日期的部分報表。

新報表為 `SchemaVersion: 2`，`Metric: "sampled_transcode_room_peaks"`，主要包含：

- `SourceAPIs`、`BusinessAssumptions`、`SamplingRule`、`DetailIntervalRule`、`ResolutionAttribution`、
  `ResolutionConflictRule`、`ResolutionRankingRule`、
  `TemplateAttribution`、`TemplateWarningThresholdSeconds: 300`：新資料來源及口徑。
- `TranscodeQueryAudit`：逐日完整頁數、原始筆數、RequestIds、快取命中、查詢耗時；
  `APIRequests` 包含重試。快取命中保留原始 RequestId。
- `TranscodeStats`：原始／相同明細去重／其他服務類型／未知流名及缺尺寸數量，
  `ExpandedMinuteRecords` 為去重後擴展分鐘區間的明細數（無擴展時可省略）。
- `TranscodeDetails`：明細原始身份欄位、`DetailId`、UUID、有效起訖、解析度分桶、
  `EffectiveEndInclusive`、`MinuteIntervalExpanded`、`SourceDays`、模板集合、`TemplateStatus`、
  首末及計數證據、最小模板時間差。
- `CloseTemplateWarnings`：每筆過近雙模板明細的最近證據對與時間差。
- `ResolutionWarnings`：按 UUID／重疊明細組合彙整的解析度警告，包含 `WarningId`、`StreamNames`、
  各筆明細及原始／有效起訖、候選解析度、採用的解析度／分桶／明細 ID、模板及所有 `SampleTimes`。
- `LogAudit`：日誌來源、檔案及行數、狀態碼、篩選、解析失敗、非 2xx 請求行數，
  分類單位是 **transcode_detail**，不再是舊版的 UUID／採樣點。
- `Samples`：每點四組房間數、活躍明細／UUID 數、缺模板、缺尺寸、未知流名、雙模板 UUID 及過近明細 ID。
  `ResolutionConflictUUIDs`／`ResolutionWarningIds` 指向採最大值的解析度警告；細節只在外層保存一份。
  舊欄位 `AmbiguousResolutionUUIDs` 保留為空陣列，已知解析度衝突不再作為排除原因。
- `Daily`：每日實際起訖、`PartialDay`、預期／完整採樣點數、完整性及四組峰值。
- `StageTimings`、`GenerationSeconds`、`APIRateLimit`、`Warnings`：耗時與本次診斷。

完整性規則：

- `ObservedPeakRooms` 始終保留已分類觀察值；缺資料時可能只是下界。
- `PeakRooms` 只有所有預期採樣點資料及分類可用、日誌無解析錯誤，且日結已滿 24 小時才填值，
  否則 `null`。這不是獨立完整性驗證；外層固定 `Completeness: "not_verified"`。
- 空明細回應不是已確認零房間；保留觀察值並標為 `no_data`。沒有任何日誌檔也不宣稱完整。
- 同日非空明細中，某採樣點無有效區間可以觀察為零；原畫／h264 成功證據可正常排除。
- 某日期沒有整五分鐘點時，峰值為 `null`，不是零。
- 只有過近雙模板告警不影響完整性；兩種模板仍計入。
- 只有解析度衝突告警不影響完整性；採最大解析度計入，未知解析度及缺模板仍影響完整性。
- 日誌／API 缺漏、明細假設、分鐘級起訖精度及未知重推不會因 `Complete: true` 而消失。
- 日誌不重建觀看時長，不使用 response time 或請求間距推算並發。

結束碼：完整輸入報表 `0`；不完整診斷報表或執行失敗 `1`；CLI／時間／憑證錯誤 `2`。
非零不一定沒有 JSON。近期日期可能因日誌仍暫定而產生診斷 JSON、結束碼 `1`。

日誌證據 URL 原樣保留，不移除 query、fragment、帳密，也不重組網址，可能含簽名或敏感資訊。
檔名、UUID、推流域名、RequestId 仍屬營運資料。
原始快取及報表請限制存取，不要提交版本庫。錯誤及下載失敗 URL 保留原始訊息，可能含簽名資訊。

## 舊版遷移

- 移除 `resolution_source_enhance.py`，新增 `transcode_source_enhance.py`。
- 移除 `--app-name`、`--resolution-lookback-minutes`、`--resolution-workers`、
  `--resolution-cache-dir`、`--no-resolution-cache`、`--refresh-resolution-cache`。
- 不再輸出播放 RequestId、推流 RequestId、Sequence、開播首筆尺寸或逐點模板視窗欄位。
- 舊腳本參數及 JSON 讀取端須更新；不做靜默轉換。
- 門檻固定五分鐘，不提供時間太近告警的 CLI 設定。

## 離線回歸測試

```bash
python3 -B -m unittest discover -s daily_topspeed_concurrent/tests -v
```

測試不讀真實憑證、不呼叫騰訊 API、不修改既有快取或歷史報表。涵蓋預設／verbose
輸出、warning 分類計數、每日不完整原因、非整日範圍與空採樣峰值、耗時格式、
成功／不完整／寫檔失敗結束碼，以及使用離線 API 回應與本地日誌的完整報表流程。
比對兩種輸出模式的 JSON（排除實測耗時），確認統計、完整性與 warning 明細不變。

欄位定義參考：[騰訊官方 Python SDK](https://github.com/TencentCloud/tencentcloud-sdk-python/blob/master/tencentcloud/live/v20180801/models.py)
的 `DescribeLiveTranscodeDetailInfoRequest`、`TranscodeDetailInfo`、`DescribeLogDownloadListRequest`。
