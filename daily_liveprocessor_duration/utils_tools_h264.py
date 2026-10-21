"""每日普通 H.264 轉碼統計工具；不讀憑證、不呼叫 API、不輸出報表。"""
import argparse
from datetime import datetime, timedelta, timezone
import re

TAIPEI = timezone(timedelta(hours=8))
MODULE_CODEC = "liveprocessor_H264"
RESOLUTIONS = ("480P", "720P", "1080P", "2K", "4K")
CATEGORIES = tuple(f"{MODULE_CODEC}_{resolution}" for resolution in RESOLUTIONS)


def taipei_time(value):
    """嚴格檢查 YYYY-MM-DD HH:MM:SS，固定為台北時間（UTC+8）。"""
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value):
            raise ValueError
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected Taipei time: YYYY-MM-DD HH:MM:SS (UTC+8)") from None


def csv_list(value):
    """逗號分隔的域名清單；移除空白、空項及重複值，維持指定順序。"""
    values = list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not values:
        raise argparse.ArgumentTypeError("Provide at least one push domain")
    return values


def validate_time_range(start, end, now=None):
    """拒絕反向及未來區間；今天已過去的時段可查，但不保證資料完整。"""
    if start > end:
        raise ValueError("--start-time must not be later than --end-time")
    now = now if now is not None else datetime.now(TAIPEI)
    if end > now:
        raise ValueError("--end-time must not be in the future in Taipei time (UTC+8)")


def iter_day_ranges(start, end):
    """逐日產出查詢時段，首尾依輸入時間裁切，不共用隔日午夜端點。"""
    current = start
    while current <= end:
        day_end = current.replace(hour=23, minute=59, second=59, microsecond=0)
        yield current, min(day_end, end)
        if day_end >= end:
            break
        current = day_end + timedelta(seconds=1)


def parse_api_time(value):
    """支援 API 的 UTC+8 字串及 ISO 時間；有時區者先轉成台北時間。"""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T](?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]"
        r"(?:\.[0-9]+)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?", value
    ):
        raise ValueError("API row contains an invalid Time")
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if point.tzinfo is None:
            point = point.replace(tzinfo=TAIPEI)
        return point.astimezone(TAIPEI)
    except (ValueError, OverflowError):
        raise ValueError("API row contains an invalid Time") from None


def response_data(document):
    """驗證總量回應外層；API 錯誤僅顯示安全的錯誤代碼與 RequestId。"""
    result = document.get("Response") if isinstance(document, dict) else None
    if not isinstance(result, dict):
        raise ValueError("API response is missing Response")
    request_id = result.get("RequestId")
    valid_id = isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
    if "Error" in result:
        error = result["Error"]
        code = error.get("Code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,128}", code):
            code = "UnknownError"
        raise ValueError(f"API error: {code}; RequestId={request_id if valid_id else 'unavailable'}")
    if not valid_id:
        raise ValueError("API response is missing a valid RequestId")
    rows = result.get("DataInfoList")
    if not isinstance(rows, list):
        raise ValueError("API response is missing DataInfoList")
    return rows, request_id


def summarize_day(document, day):
    """加總單日回應的五種解析度；不推算連線、不乘上時間桶的分鐘數。

    此函式無法從回應本身證明資料完整性；呼叫端須標示實際查詢時段。
    """
    # 第一步：驗證 API 回應，建立新的五類計數器，沒有資料的分類維持零。
    rows, request_id = response_data(document)
    totals = dict.fromkeys(CATEGORIES, 0)
    seen = set()
    matched = 0

    # 第二步：檢查所有回傳列；不默默略過損壞資料、跨日資料或重複統計桶。
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ValueError(f"API row {index} must be an object")
        point = parse_api_time(row.get("Time"))
        if point.date() != day:
            raise ValueError(f"API row {index} has a Time outside {day.isoformat()}")
        codec, resolution = row.get("ModuleCodec"), row.get("Resolution")
        if not isinstance(codec, str) or not codec or not isinstance(resolution, str) or not resolution:
            raise ValueError(f"API row {index} is missing ModuleCodec or Resolution")
        duration = row.get("Duration")
        # 官方 Duration 為 Integer，單位分鐘；拒絕布林、字串、小數、NaN 與負數。
        if type(duration) is not int or duration < 0:
            raise ValueError(f"API row {index} Duration must be non-negative integer minutes")
        resolution = resolution.upper()
        key = (point, codec, resolution)
        if key in seen:
            raise ValueError(f"API row {index} duplicates a Time/ModuleCodec/Resolution bucket")
        seen.add(key)

        # 第三步：精確篩選普通 H.264；2k／4k 等大小寫統一，不猜測未知解析度。
        if codec != MODULE_CODEC:
            continue
        if resolution not in RESOLUTIONS:
            raise ValueError(f"API row {index} has an unsupported liveprocessor_H264 Resolution")
        totals[f"{MODULE_CODEC}_{resolution}"] += duration
        matched += 1

    # 第四步：回傳每日列；空清單仍列五個零，但明確標示不是已確認的零用量。
    return {
        "Date": day.isoformat(),
        **totals,
        "TotalDurationMinutes": sum(totals.values()),
        "DataStatus": "data_returned" if rows else "no_data",
        "ReturnedDataPoints": len(rows),    
        "MatchedDataPoints": matched,
        "RequestId": request_id,
    }
