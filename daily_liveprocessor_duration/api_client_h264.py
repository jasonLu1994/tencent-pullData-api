"""騰訊雲直播總量 API：憑證、TC3 簽章與單日查詢，僅使用標準函式庫。

Doc: https://cloud.tencent.com/document/api/267/59864
不在此處進行分類、加總或寫入檔案。
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import time
import urllib.request

SERVICE = "live"
HOST = "live.tencentcloudapi.com"
ACTION = "DescribeLiveTranscodeTotalInfo"
VERSION = "2018-08-01"
ALGORITHM = "TC3-HMAC-SHA256"


def credentials():
    """只讀取指定的兩個環境變數，不提供其他名稱的相容別名。"""
    secret_id = os.environ.get("TENCENT_SECRETID")
    secret_key = os.environ.get("TENCENT_SECRETKEY")
    if not secret_id or not secret_key:
        raise ValueError("TENCENT_SECRETID and TENCENT_SECRETKEY must both be set")
    return secret_id, secret_key


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def sign_headers(secret_id: str, secret_key: str, payload: str) -> dict:
    """依實際送出的 JSON 與總量 API Action 計算 TC3 簽章。"""
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    canonical_headers = (
        f"content-type:{content_type}\n"
        f"host:{HOST}\n"
        f"x-tc-action:{ACTION.lower()}\n"
    )
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = "\n".join([
        "POST", "/", "", canonical_headers, signed_headers,
        hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    ])
    credential_scope = f"{date}/{SERVICE}/tc3_request"
    string_to_sign = "\n".join([
        ALGORITHM, str(timestamp), credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    secret_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, SERVICE)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return {
        "Authorization": (
            f"{ALGORITHM} Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": HOST,
        "X-TC-Action": ACTION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": VERSION,
    }


def describe_live_transcode_total_info(secret_id, secret_key, start_time, end_time,
                                      push_domains=None, mainland_or_oversea=None):
    """依指定的 UTC+8 起訖時間查詢原始回應，不自行擴大成全天。"""
    body = {
        "StartTime": start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "EndTime": end_time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if push_domains is not None:
        body["PushDomains"] = push_domains
    if mainland_or_oversea is not None:
        body["MainlandOrOversea"] = mainland_or_oversea
    payload = json.dumps(body, separators=(",", ":"))
    request = urllib.request.Request(
        f"https://{HOST}/", data=payload.encode("utf-8"),
        headers=sign_headers(secret_id, secret_key, payload), method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)
