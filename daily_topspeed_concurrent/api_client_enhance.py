"""
騰訊直播 API:憑證、依 Action 簽章、共用限速與有限次重試；不做統計或快取。

Example (日期需換成近一個月內、最晚昨天):
python3 daily_topspeed_concurrent/daily_report_enhance.py \
  --start-time "2026-10-16 00:00:00" \
  --end-time "2026-10-16 23:59:59" \
  --play-domains tencent-global-pull-rtmp.17app.co \
  --transcode-workers 3 \
  --request-interval 0.1 \
  --log-workers 3 \
  --output daily_topspeed_concurrent/reports/report_2026-10-16.json
"""

from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPSConnection
import hashlib
import hmac
import json
import math
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request

SERVICE = "live"
HOST = "live.tencentcloudapi.com"
VERSION = "2018-08-01"
ALGORITHM = "TC3-HMAC-SHA256"

LOG_ACTION = "DescribeLogDownloadList"
TRANSCODE_ACTION = "DescribeLiveTranscodeDetailInfo"

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_RETRIES = 3
MIN_REQUEST_INTERVAL = 1 / 40


def credentials():
    """只接受這一組名稱，不混用或回退至其他環境變數。"""
    secret_id = os.environ.get("TENCENT_SECRETID")
    secret_key = os.environ.get("TENCENT_SECRETKEY")
    if not secret_id or not secret_key:
        raise ValueError("TENCENT_SECRETID and TENCENT_SECRETKEY must both be set")
    return secret_id, secret_key

def _hmac_sha256(key, message):
    """以位元組金鑰簽署 UTF-8 文字，回傳下一層金鑰推導所需的原始位元組。"""
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()

def sign_headers(secret_id, secret_key, action, payload):
    """依實際送出的 JSON 與指定 Action 計算 TC3 簽章，不固定綁定單一 API。"""
    # 第一步：以 UTC 日期建立簽章範圍，正規化參與簽章的標頭與請求內容。
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    canonical_headers = (
        f"content-type:{content_type}\nhost:{HOST}\nx-tc-action:{action.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, signed_headers,
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    ])
    # 第二步：將正規化請求的雜湊組成待簽字串。
    scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join([
        ALGORITHM, str(timestamp), scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    # 第三步：依日期、服務、tc3_request 逐層推導金鑰，再計算最終簽章。
    date_key = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    service_key = _hmac_sha256(date_key, SERVICE)
    signing_key = _hmac_sha256(service_key, "tc3_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    # 第四步：回傳 HTTP 標頭；Action 標頭保留原名，簽章中的 Action 使用小寫。
    return {
        "Authorization": (
            f"{ALGORITHM} Credential={secret_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": VERSION,
    }

def retry_after_seconds(headers):
    """尊重伺服器的 Retry-After，僅接受有限秒數或 HTTP 日期，上限 60 秒。"""
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return 0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0
    return min(60, max(0, seconds)) if math.isfinite(seconds) else 0


def transcode_body(day, push_domain=None, page=1):
    """明細 API 以北京日期查詢，每頁使用官方上限 1000 筆。"""
    body = {"DayTime": day.strftime("%Y%m%d"), "PageNum": page, "PageSize": 1000}
    if push_domain is not None:
        body["PushDomain"] = push_domain
    return body

# ReusableHTTPS
class ReusableHTTPS:
    """每個執行緒獨立持有連線；代理環境沿用 urllib 的既有路由與驗證。"""

    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._connections = set()
        proxies = urllib.request.getproxies()
        self._use_proxy = bool(proxies.get("https") or proxies.get("all")) and not urllib.request.proxy_bypass(HOST)

    def _discard(self, connection):
        with self._lock:
            self._connections.discard(connection)
        self._local.connection = None
        connection.close()

    def post(self, payload, headers):
        if self._use_proxy:
            request = urllib.request.Request(f"https://{HOST}/", data=payload.encode("utf-8"),
                                             headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = HTTPSConnection(HOST, timeout=30)
            self._local.connection = connection
            with self._lock:
                self._connections.add(connection)
        try:
            connection.request("POST", "/", body=payload.encode("utf-8"), headers=headers)
            with connection.getresponse() as response:
                # 必須讀完整個 body 才能安全重用 keep-alive 連線；不跟隨重新導向傳送憑證。
                body = response.read()
                status, reason, response_headers = response.status, response.reason, response.headers
                if response.will_close:
                    self._discard(connection)
            if status >= 300:
                raise urllib.error.HTTPError(f"https://{HOST}/", status, reason,
                                             response_headers, None)
            return json.loads(body)
        except BaseException:
            if getattr(self._local, "connection", None) is connection:
                self._discard(connection)
            raise

    def close(self):
        """呼叫端須先等待 worker 結束，再關閉所有執行緒的閒置連線。"""
        with self._lock:
            connections, self._connections = self._connections, set()
        for connection in connections:
            connection.close()
        self._local.connection = None
        

# LiveClient
class LiveClient:
    """可共用於多執行緒；全體發送共用限速，網路等待不持鎖，不讀寫檔案。"""

    def __init__(self, secret_id, secret_key, request_interval=0.1):
        """保存憑證與請求間隔；同一實例的兩種 API 共用限速狀態。"""
        if not MIN_REQUEST_INTERVAL <= request_interval <= 60:
            raise ValueError("request_interval must be in [0.025, 60]")
        self.secret_id = secret_id
        self.secret_key = secret_key
        self.request_interval = request_interval
        self._last_request = None
        self._request_lock = threading.Condition()
        self._counts_lock = threading.Lock()
        self._request_counts = Counter()
        self._effective_interval = request_interval
        self._blocked_until = 0
        self._rate_limit_events = 0
        self._successes = 0
        self._transport = ReusableHTTPS()


    def request(self, action, body):
        """僅重試限流／內部錯誤與暫時性網路錯誤，最多重試三次；每次重新簽章。"""
        payload = json.dumps(body, separators=(",", ":"), allow_nan=False)
        for attempt in range(MAX_RETRIES + 1):
            delay = 2 ** attempt + random.uniform(0, 0.5)
            try:
                document = self._send_once(action, payload)
            except urllib.error.HTTPError as error:
                limited = error.code == 429
                if limited:
                    delay = max(delay, retry_after_seconds(error.headers))
                    self._rate_limited(delay)
                if error.code not in (429, 500, 502, 503, 504) or attempt == MAX_RETRIES:
                    raise
                error.close()
            except (OSError, HTTPException):
                if attempt == MAX_RETRIES:
                    raise
            else:
                response = document.get("Response") if isinstance(document, dict) else None
                error = response.get("Error") if isinstance(response, dict) else None
                code = error.get("Code") if isinstance(error, dict) else None
                retryable = isinstance(code, str) and code.split(".", 1)[0] in (
                    "RequestLimitExceeded", "InternalError",
                )
                limited = isinstance(code, str) and code.split(".", 1)[0] == "RequestLimitExceeded"
                if limited:
                    self._rate_limited(delay)
                if not error:
                    self._succeeded()
                if not retryable or attempt == MAX_RETRIES:
                    return document
            print(f"Retrying {action}: {attempt + 1}/{MAX_RETRIES}, waiting {delay:.1f}s",
                  file=sys.stderr)
            time.sleep(delay)

    def _send_once(self, action, payload):
        """鎖內等待與簽章；鎖外讀取回應，允許多筆網路請求同時進行。"""
        with self._request_lock:
            while True:
                now = time.monotonic()
                ready = self._blocked_until
                if self._last_request is not None:
                    ready = max(ready, self._last_request + self._effective_interval)
                if ready <= now:
                    break
                # 等待時釋放鎖，讓其他 worker 的限流回應可立即延長全域冷卻。
                self._request_lock.wait(timeout=ready - now)
            headers = sign_headers(self.secret_id, self.secret_key, action, payload)
            self._last_request = time.monotonic()
            with self._counts_lock:
                self._request_counts[action] += 1
        return self._transport.post(payload, headers)


    def transcode_details(self, day, push_domain=None, page=1):
        """單頁轉碼明細；分頁完整性及快取由來源層負責。"""
        return self.request(TRANSCODE_ACTION, transcode_body(day, push_domain, page))

    
    def close(self):
        self._transport.close()

    def rate_limit_status(self):
        with self._request_lock:
            return {"ConfiguredQPS": 1 / self.request_interval,
                    "EffectiveQPS": 1 / self._effective_interval,
                    "RateLimitEvents": self._rate_limit_events}

    def _rate_limited(self, cooldown):
        # 全體 worker 共用降速與冷卻；不只讓收到 429 的單一 worker 退避。
        with self._request_lock:
            self._effective_interval = min(60, self._effective_interval * 2)
            self._blocked_until = max(self._blocked_until, time.monotonic() + cooldown)
            self._rate_limit_events += 1
            self._successes = 0
            qps = 1 / self._effective_interval
            self._request_lock.notify_all()
        print(f"API rate limited: shared limit reduced to ~{qps:.2f}/s, cooldown={cooldown:.1f}s",
              file=sys.stderr)

    def _succeeded(self):
        with self._request_lock:
            if time.monotonic() < self._blocked_until:
                return
            self._successes += 1
            if self._successes >= 100:
                # 每 100 個成功回應才緩慢恢復，永遠不超過使用者設定。
                self._effective_interval = max(self.request_interval, self._effective_interval * 0.9)
                self._successes = 0
                self._request_lock.notify_all()

    def request_count(self, action):
        """回傳已嘗試發送的次數（含重試），供進度顯示使用。"""
        with self._counts_lock:
            return self._request_counts[action]


    def log_list(self, start, end, play_domains, is_fast_live=0):
        """依台北起訖時間取得播放日誌下載清單；此處不下載，也不解析日誌。"""
        return self.request(LOG_ACTION, {
            "StartTime": start.strftime(TIME_FORMAT),
            "EndTime": end.strftime(TIME_FORMAT),
            "PlayDomains": play_domains,
            "IsFastLive": is_fast_live,
        })