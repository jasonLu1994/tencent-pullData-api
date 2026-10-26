"""轉碼明細、日誌歸因及五分鐘房間峰值；不讀憑證或呼叫 API。"""
import argparse
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from http.client import HTTPException
import hashlib
import json
import re
import urllib.error
import urllib.parse

TAIPEI = timezone(timedelta(hours=8))
TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
TEMPLATES = ("enhance003", "enhance002")
TEMPLATE_WARNING_SECONDS = 300
GROUPS = tuple(f"{template}_{resolution}p" for template in TEMPLATES for resolution in (720, 1080))
UUID_PATTERN = r"(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})"
STREAM_PATTERN = re.compile(
    r"^(?P<uuid>" + UUID_PATTERN + r")(?P<suffix>_enhance002|_enhance003|_h264)?$", re.I,
)
MINUTE_TIME_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)


def format_elapsed(seconds):
    """終端顯示用；JSON 仍保留原始秒數。"""
    seconds = round(seconds, 1)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(round(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m {seconds:02d}s" if hours else f"{minutes}m {seconds:02d}s"


def taipei_time(value):
    """CLI 僅接受台北時間 YYYY-MM-DD HH:MM:SS。"""
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value):
            raise ValueError
        return datetime.strptime(value, TIME_FORMAT).replace(tzinfo=TAIPEI)
    except ValueError:
        raise argparse.ArgumentTypeError("Expected Taipei time: YYYY-MM-DD HH:MM:SS (UTC+8)") from None


def csv_list(value):
    """域名去重並正規化；拒絕 URL、帳密、port 及空值。"""
    values = list(dict.fromkeys(item.strip().lower().rstrip(".") for item in value.split(",") if item.strip()))
    if not values or any(not re.fullmatch(r"[a-z0-9_-]+(?:\.[a-z0-9_-]+)*", item) for item in values):
        raise argparse.ArgumentTypeError("Provide comma-separated domain names, not URLs")
    return values


def month_start_limit(now):
    """近一個月採曆月回推，月底日期裁切到前月最後一天；服務端仍為最終依據。"""
    import calendar
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return now.date().replace(year=year, month=month, day=min(now.day, calendar.monthrange(year, month)[1]))


def validate_time_range(start, end, now=None):
    now = now if now is not None else datetime.now(TAIPEI)
    if end - start < timedelta(minutes=5):
        raise ValueError("Time range (--end-time - --start-time) must be at least five minutes")
    if end.date() >= now.date():
        raise ValueError("Transcode details are available only through yesterday (Taipei UTC+8)")
    if start.date() < month_start_limit(now):
        raise ValueError("Start time must be within the last calendar month")


def response_data(document, list_key="DataInfoList"):
    response = document.get("Response") if isinstance(document, dict) else None
    if not isinstance(response, dict):
        raise ValueError("API response is missing Response")
    request_id = response.get("RequestId")
    if "Error" in response:
        error = response["Error"]
        if not isinstance(error, dict):
            raise RuntimeError(f"Invalid API error response; RequestId={request_id or 'unavailable'}")
        raise RuntimeError(f"API error: {error.get('Code') or 'UnknownError'}: "
                           f"{error.get('Message', '')}; RequestId={request_id or 'unavailable'}")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
        raise ValueError("API response is missing a valid RequestId")
    rows = response.get(list_key)
    if not isinstance(rows, list):
        raise ValueError(f"API response is missing {list_key}")
    return rows, request_id


def iter_day_ranges(start, end):
    current = start
    while current <= end:
        stop = min(current.replace(hour=23, minute=59, second=59, microsecond=0), end)
        yield current, stop
        if stop == end:
            break
        current = stop + timedelta(seconds=1)


def parse_api_time(value):
    """明細支援分鐘精度；其餘 API／日誌支援秒、子秒及 ISO 時區。"""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T](?:[01][0-9]|2[0-3]):[0-5][0-9]"
        r"(?::[0-5][0-9](?:\.[0-9]+)?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])?", value,
    ):
        raise ValueError("Invalid timestamp")
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if point.tzinfo is None:
            point = point.replace(tzinfo=TAIPEI)
        return point.astimezone(TAIPEI)
    except (ValueError, OverflowError):
        raise ValueError("Invalid timestamp") from None

    
def prepare_details(day_documents, start, end, push_domain=None):
    """合併逐日原始明細；完整相同列去重，保留跨日來源及明細原始身份。"""
    details, stats = {}, Counter()
    for day, documents in day_documents:
        for document in documents:
            rows, _ = response_data(document)
            for row in rows:
                stats["ReturnedRecords"] += 1
                if not isinstance(row, dict):
                    raise ValueError("Invalid transcode detail record")
                for field in ("StreamName", "PushDomain", "Type", "ModuleCodec"):
                    if not isinstance(row.get(field), str) or not row[field].strip():
                        raise ValueError(f"Transcode detail is missing {field}")
                left, right = parse_api_time(row.get("StartTime")), parse_api_time(row.get("EndTime"))
                if left > right:
                    raise ValueError("Transcode detail StartTime is after EndTime")
                if type(row.get("Duration")) is not int or row["Duration"] < 0:
                    raise ValueError("Invalid transcode Duration")
                if push_domain and row["PushDomain"].lower().rstrip(".") != push_domain:
                    raise ValueError("Transcode detail PushDomain does not match query")
                if row["Type"] != "Transcode":
                    stats["NonTranscodeRecords"] += 1
                    continue
                # 僅分鐘精度、起訖同時刻的明細展開為 [該分鐘, 下一分鐘)。
                # Duration 是累計分鐘數，不作為擴展條件，也不改寫原始欄位。
                expanded = left == right and all(MINUTE_TIME_PATTERN.fullmatch(row[key])
                                                 for key in ("StartTime", "EndTime"))
                if expanded:
                    right += timedelta(minutes=1)
                if right < start or (expanded and right == start) or left > end:
                    stats["OutsideQueryRangeRecords"] += 1
                    continue
                identity = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                                     allow_nan=False).encode("utf-8")).hexdigest()
                if identity in details:
                    stats["DuplicateRecords"] += 1
                    if day not in details[identity]["SourceDays"]:
                        details[identity]["SourceDays"].append(day)
                    continue
                uuid, _, _ = stream_identity(row["StreamName"])
                bucket = resolution_bucket(row.get("Resolution"))
                if uuid is None:
                    stats["UnknownStreamRecords"] += 1
                if bucket is None:
                    stats["MissingResolutionRecords"] += 1
                if expanded:
                    stats["ExpandedMinuteRecords"] += 1
                details[identity] = {
                    "DetailId": identity, 
                    "UUID": uuid, 
                    **{key: row.get(key) for key in (
                        "StreamName", "PushDomain", "StartTime", "EndTime", "Duration", "Resolution",
                        "Type", "ModuleCodec", "Bitrate", "MainlandOrOversea",
                    )}, 
                    "Bucket": bucket, 
                    "SourceDays": [day],
                    "EffectiveStartTime": max(left, start).strftime(TIME_FORMAT),
                    "EffectiveEndTime": min(right, end).strftime(TIME_FORMAT),
                    "EffectiveEndInclusive": not expanded or end < right,
                    "MinuteIntervalExpanded": expanded,
                    "_Start": max(left, start), 
                    "_End": min(right, end), 
                    "_Evidence": {}, 
                    "_Times": {},
                }
    records = sorted(details.values(), key=lambda row: (row["_Start"], row["StreamName"], row["DetailId"]))
    stats["UniqueRecords"] = len(records)
    return records, dict(stats)


def stream_identity(value, is_url=False):
    if not isinstance(value, str):
        return None, None, None
    try:
        name = urllib.parse.unquote(urllib.parse.urlsplit(value).path).rsplit("/", 1)[-1] if is_url else value
    except ValueError:
        return None, None, None
    if name.lower().endswith((".flv", ".m3u8")):
        name = name.rsplit(".", 1)[0]
    match = STREAM_PATTERN.fullmatch(name)
    if not match:
        return None, None, None
    source, uuid = match["uuid"], match["uuid"].lower()
    if "-" not in uuid:
        uuid = f"{uuid[:8]}-{uuid[8:12]}-{uuid[12:16]}-{uuid[16:20]}-{uuid[20:]}"
    return uuid, (match["suffix"] or "_original")[1:].lower(), source


def resolution_bucket(value):
    """延續兩級分桶：尺寸短邊 <=720 或 >720；亦接受 API 解析度級別。"""
    identity = resolution_identity(value)
    if identity is None:
        return None
    if identity[0] == "dimensions":
        side = min(identity[1:])
    elif isinstance(identity[1], str):
        return 1080
    else:
        side = identity[1]
    return 720 if side <= 720 else 1080


@contextmanager
def api_query(action, start, end):
    try:
        yield
    except (RuntimeError, OSError, HTTPException, ValueError) as error:
        detail = str(error) or type(error).__name__
        if isinstance(error, urllib.error.HTTPError):
            detail += f" | URL={error.url}"
            error.close()
        raise RuntimeError(f"API={action} | StartTime={start.strftime(TIME_FORMAT)} | "
                           f"EndTime={end.strftime(TIME_FORMAT)} | {detail}") from error


def midnight(day):
    return datetime.combine(day, time(), TAIPEI)


def attribute_templates(lines, details, domains, start, end):
    """逐行只歸入同 UUID 的明細區間；最短跨模板時間差不依賴日誌順序。"""
    by_uuid = defaultdict(list)
    for detail in details:
        if detail["UUID"]:
            by_uuid[detail["UUID"]].append(detail)
    indexes = {}
    for uuid, records in by_uuid.items():
        records.sort(key=lambda row: (row["_Start"], row["DetailId"]))
        latest, prefix_ends = records[0]["_End"], []
        for row in records:
            latest = max(latest, row["_End"])
            prefix_ends.append(latest)
        indexes[uuid] = ([row["_Start"] for row in records], prefix_ends, records)
        # indexes
        # 起點     終點
        # 明細A   10:00    10:30
        # 明細B   10:05    10:10
        # 明細C   10:15    10:20
        # 存進去的結構就是（時間簡寫）：
        # indexes[uuid] = (
        #     ["10:00", "10:05", "10:15"],
        #     ["10:30", "10:30", "10:30"],
        #     [明細A, 明細B, 明細C],
        # )
    stats, statuses, uuids = Counter(), Counter(), set()
    for source, line_number, line in lines:
        if not line.strip():
            continue
        stats["ReadLines"] += 1
        try:
            point, host, url, status = log_evidence(line) # sample.log
            # point = datetime(2026, 10, 1, 0, 0, 8, tzinfo=TAIPEI)
            # host = "tencent-global-pull-rtmp.17app.co"
            # url = "/live/a5150f5f-6f2b-42e4-bd8e-8c89d3fc26ed_enhance003.flv"
            # status = 200
        except ValueError:
            stats["InvalidLines"] += 1
            continue
        if not start <= point <= end:
            stats["OutsideQueryRangeLines"] += 1
            continue
        if host not in domains:
            stats["DomainFilteredLines"] += 1
            continue
        statuses[str(status)] += 1
        successful = 200 <= status < 300
        if not successful:
            stats["UnsuccessfulLines"] += 1
        uuid, category, _ = stream_identity(url, is_url=True)
        # uuid     = "a5150f5f-6f2b-42e4-bd8e-8c89d3fc26ed"
        # category = "enhance003"
        if uuid is None:
            stats["UnrecognizedURLs"] += 1
            continue
        if not successful:
            continue
        matched = []
        if uuid in indexes:
            starts, ends, records = indexes[uuid]
            index = bisect_right(starts, point) - 1
            while index >= 0 and ends[index] >= point:
                detail = records[index]
                if point < detail["_End"] or (point == detail["_End"] and detail["EffectiveEndInclusive"]):
                    matched.append(detail)
                index -= 1
        if not matched:
            stats["OutsideDetailIntervalsLines"] += 1
            continue
        record = {"Time": point.isoformat(sep=" ", timespec="microseconds"), "Host": host,
                  "URL": url, "HTTPStatus": status, "LogFile": source,
                  "LineNumber": line_number}
        stats["AcceptedLines"] += 1
        uuids.add(uuid)
        for detail in matched:
            evidence = detail["_Evidence"]
            if category not in evidence:
                evidence[category] = {"Count": 0, "First": record, "Last": record}
            entry = evidence[category]
            entry["Count"] += 1
            if record["Time"] < entry["First"]["Time"]:
                entry["First"] = record
            if record["Time"] > entry["Last"]["Time"]:
                entry["Last"] = record
            if category in TEMPLATES:
                # 同模板同時刻只保留一筆代表證據，避免重複請求膨脹暫存資料。
                detail["_Times"].setdefault(category, {}).setdefault(point, record)
            # record	模板／類別	時間
            # A	enhance003	10:00
            # B	enhance003	10:10
            # C	enhance003	10:10（同時間另一筆）
            # D	enhance003	10:20
            # E	enhance002	10:11
            # F	h264	10:15
            # 每個 record 都包含這些欄位，例如 B：
            # B = {
            #     "Time": "2026-10-14 10:10:00.000000+08:00",
            #     "Host": "play.example.com",
            #     "URL": "/live/c1381c3c-276f-4345-ab43-eb7494b8c981_enhance003.flv",
            #     "HTTPStatus": 200,
            #     "LogFile": "playback.log",
            #     "LineNumber": 102,
            # }
            # detail["_Evidence"] = {
            #     "enhance003": {
            #         "Count": 4,     # A、B、C、D 都計次
            #         "First": A,    # 10:00
            #         "Last": D,     # 10:20
            #     },
            #     "enhance002": {
            #         "Count": 1,
            #         "First": E,    # 只有一筆，所以 First、Last 相同
            #         "Last": E,
            #     },
            #     "h264": {
            #         "Count": 1,
            #         "First": F,
            #         "Last": F,
            #     },
            # }
            # detail["_Times"] = {
            #     "enhance003": {
            #         t_1000: A,
            #         t_1010: B,     # C 與 B 同模板、同時間，因此不存 C
            #         t_1020: D,
            #     },
            #     "enhance002": {
            #         t_1011: E,
            #     },
            #     # 沒有 h264，因為它不在 TEMPLATES 裡
            # }
    near, classifications = [], Counter()
    for detail in details:
        evidence, times = detail.pop("_Evidence"), detail.pop("_Times")
        templates = [template for template in TEMPLATES if template in evidence]
        classification = ("dual" if len(templates) == 2 else templates[0] if templates
                          else "excluded" if evidence else "missing")
        classifications[classification] += 1
        detail.update(Templates=templates, TemplateStatus=classification, TemplateEvidence=evidence,
                      MinimumTemplateGapSeconds=None, CloseTemplateWarning=False)
        # detail["Templates"] = templates               # 出现过的模板列表
        # detail["TemplateStatus"] = classification     # 分类结果
        # detail["TemplateEvidence"] = evidence         # 各模板／类别的日志证据
        # detail["MinimumTemplateGapSeconds"] = None    # 两种模板的最小时间差，先设为未计算
        # detail["CloseTemplateWarning"] = False        # 是否触发模板时间过近警告，默认否  
        if len(templates) == 2:
            first, second = (sorted(times[template]) for template in TEMPLATES)
            i = j = 0
            best = None
            while i < len(first) and j < len(second):
                candidate = (abs((first[i] - second[j]).total_seconds()), first[i], second[j])
                if best is None or candidate < best:
                    best = candidate
                if first[i] <= second[j]:
                    i += 1
                else:
                    j += 1
            gap, left, right = best
            detail["MinimumTemplateGapSeconds"] = gap
            detail["CloseTemplateWarning"] = gap <= TEMPLATE_WARNING_SECONDS
            if detail["CloseTemplateWarning"]:
                near.append({"DetailId": detail["DetailId"], "UUID": detail["UUID"],
                             "StartTime": detail["StartTime"], "EndTime": detail["EndTime"],
                             "GapSeconds": gap, "Evidence": {
                                 TEMPLATES[0]: times[TEMPLATES[0]][left],
                                 TEMPLATES[1]: times[TEMPLATES[1]][right],
                             }})
    stats["UUIDs"] = len(uuids)
    return {"Stats": dict(stats), "StatusCounts": dict(statuses),
            "ClassificationCounts": dict(classifications), "ClassificationUnit": "transcode_detail",
            "EvidenceRetention": "first/last/count per category/detail and closest dual-template pair; original URLs"}, near


def log_evidence(line):
    """純文字沿用第 4 欄 URL；CLS JSONL 使用 time／host／url／http_code。"""
    try:
        if line.lstrip().startswith("{"):
            row = json.loads(line)
            stamp, host, url, status = row["time"], row["host"], row["url"], row["http_code"]
        else:
            text = line.strip()
            if text.startswith("["):
                stamp, text = text[1:].split("]", 1)
            else:
                stamp, text = text.split(None, 1)
                if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", stamp):
                    clock, text = text.split(None, 1)
                    stamp += " " + clock
            fields = text.split()
            host, url, status = fields[1], fields[2], fields[7]
            # fields[0]	"58.89.73.214"
            # fields[1]	"tencent-global-pull-rtmp.17app.co"
            # fields[2]	"/live/a5150f5f-6f2b-42e4-bd8e-8c89d3fc26ed_enhance003.flv"
            # fields[3]	"3564474"
            # fields[4]	"24"
            # fields[5]	"0"
            # fields[6]	"0"
            # fields[7]	"200"

            # host = "tencent-global-pull-rtmp.17app.co"
            # url = "/live/a5150f5f-6f2b-42e4-bd8e-8c89d3fc26ed_enhance003.flv"
            # status = "200"
        point = (datetime.strptime(str(stamp), "%Y%m%d%H%M%S").replace(tzinfo=TAIPEI)
                 if re.fullmatch(r"[0-9]{14}", str(stamp)) else parse_api_time(stamp))
        if not isinstance(host, str) or not host or not isinstance(url, str) or not url:
            raise ValueError
        if isinstance(status, bool) or not re.fullmatch(r"[0-9]{3}", str(status)) or not 100 <= int(status) <= 599:
            raise ValueError
        return point, host.lower().rstrip("."), url, int(status)
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("Invalid playback log identity/status/time") from None


def resolution_rank(value):
    """依短邊／級別選最大；同短邊尺寸以長邊排序，K 級別採名義高度。"""
    identity = resolution_identity(value)
    if identity is None:
        return None
    if identity[0] == "dimensions":
        return min(identity[1:]), max(identity[1:])
    side = identity[1]
    if isinstance(side, str):
        side = {"2K": 1440, "4K": 2160, "8K": 4320}[side]
    return side, 0


def sample_times(start, end):
    step = timedelta(minutes=5)
    point = start.replace(minute=start.minute // 5 * 5, second=0, microsecond=0)
    if point < start:
        point += step
    while point <= end:
        yield point
        if end - point < step:
            break
        point += step


def aggregate_samples(details, start, end, invalid_logs=False, no_data_days=(), resolution_warnings=None):
    """整段模板歸因後，以 UUID／模板去重；解析度衝突警告並採最大值。"""
    resolution_warnings = resolution_warnings if resolution_warnings is not None else []
    conflict_audit = {}
    points = list(sample_times(start, end))
    starts, stops = defaultdict(list), defaultdict(list)
    for detail in details:
        first = bisect_left(points, detail["_Start"])
        stop = (bisect_right if detail["EffectiveEndInclusive"] else bisect_left)(points, detail["_End"])
        if first < stop:
            starts[first].append(detail)
            stops[stop].append(detail["DetailId"])
    active, samples = {}, []
    for index, point in enumerate(points): # here
        for identity in stops[index]:
            active.pop(identity, None)
        for detail in starts[index]:
            active[detail["DetailId"]] = detail
        rooms = defaultdict(list)
        unknown = []
        for detail in active.values():
            if detail["UUID"] is None:
                unknown.append(detail["DetailId"])
            else:
                rooms[detail["UUID"]].append(detail)
        counts = dict.fromkeys(GROUPS, 0) # 統計
        missing_templates, missing_resolution, excluded, dual, close = [], [], [], [], []
        conflicts, warning_ids = [], []
        for uuid, records in sorted(rooms.items()):
            if any(row["TemplateStatus"] == "missing" for row in records):
                missing_templates.append(uuid)
            selected = [row for row in records if row["Templates"]]
            if not selected:
                if all(row["TemplateStatus"] == "excluded" for row in records):
                    excluded.append(uuid)
                continue
            templates = {template for row in selected for template in row["Templates"]}
            if len(templates) == 2:
                dual.append(uuid)
            close.extend(row["DetailId"] for row in selected if row["CloseTemplateWarning"])
            buckets = {row["Bucket"] for row in selected}
            resolutions = {resolution_identity(row["Resolution"]) for row in selected}
            if None in buckets:
                missing_resolution.append(uuid)
                continue
            chosen = max(selected, key=lambda row: (resolution_rank(row["Resolution"]), row["DetailId"]))
            bucket = chosen["Bucket"]
            if len(resolutions) != 1:
                conflicts.append(uuid)
                identities = sorted(row["DetailId"] for row in selected)
                warning_id = hashlib.sha256(json.dumps([uuid, identities]).encode()).hexdigest()
                warning_ids.append(warning_id)
                if warning_id not in conflict_audit:
                    warning = {
                        "WarningId": warning_id, "UUID": uuid,
                        "StreamNames": sorted({row["StreamName"] for row in selected}),
                        "Details": [{key: row[key] for key in (
                            "DetailId", "StreamName", "StartTime", "EndTime", "Resolution", "Bucket",
                            "EffectiveStartTime", "EffectiveEndTime", "EffectiveEndInclusive",
                        )} for row in sorted(selected, key=lambda row: row["DetailId"])],
                        "SelectedDetailId": chosen["DetailId"], "SelectedResolution": chosen["Resolution"],
                        "SelectedBucket": bucket, "Templates": sorted(templates), "SampleTimes": [],
                    }
                    conflict_audit[warning_id] = warning
                    resolution_warnings.append(warning)
                conflict_audit[warning_id]["SampleTimes"].append(point.strftime(TIME_FORMAT))
            for template in templates:
                counts[f"{template}_{bucket}p"] += 1
        no_data = point.date().isoformat() in no_data_days
        samples.append({"Time": point.strftime(TIME_FORMAT), "Counts": counts,
                        "ActiveDetails": len(active), "ActiveUUIDs": len(rooms),
                        "DataStatus": "no_data" if no_data else "data_returned",
                        "MissingTemplateUUIDs": missing_templates, "MissingResolutionUUIDs": missing_resolution,
                        "AmbiguousResolutionUUIDs": [], "ResolutionConflictUUIDs": conflicts,
                        "ResolutionWarningIds": warning_ids, "UnknownStreamDetailIds": sorted(unknown),
                        "ExcludedUUIDs": excluded, "DualTemplateUUIDs": dual,
                        "CloseTemplateDetailIds": sorted(set(close)),
                        "Complete": not (invalid_logs or no_data or unknown or missing_templates
                                         or missing_resolution)})
    return samples


def resolution_identity(value):
    """相同數值尺寸不因分隔符、空白、大小寫或前置零而製造歧義。"""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*([0-9]+)\s*[*xX×]\s*([0-9]+)\s*", value)
    if match:
        width, height = map(int, match.groups())
        return ("dimensions", width, height) if min(width, height) > 0 else None
    label = value.strip().upper()
    match = re.fullmatch(r"([0-9]+)P", label)
    if match:
        side = int(match[1])
        return ("tier", side) if side > 0 else None
    if label in ("2K", "4K", "8K"):
        return ("tier", label)
    return None


def summarize_day(start, end, samples, invalid_logs=False, provisional_logs=False):
    expected = [point.strftime(TIME_FORMAT) for point in sample_times(start, end)]
    if [row["Time"] for row in samples] != expected:
        raise ValueError("Samples must match the ordered five-minute points in the requested time range")
    coverage = bool(expected) and not invalid_logs and all(row["Complete"] for row in samples)
    complete = coverage and not provisional_logs
    peaks = {}
    for group in GROUPS:
        peak = max((row["Counts"][group] for row in samples), default=None)
        peaks[group] = {"PeakRooms": peak if complete else None, "ObservedPeakRooms": peak,
                        "ObservedPeakTimes": [row["Time"] for row in samples if row["Counts"][group] == peak]}
    return {"Date": start.date().isoformat(), "StartTime": start.strftime(TIME_FORMAT),
            "EndTime": end.strftime(TIME_FORMAT),
            "PartialDay": start != midnight(start.date()) or end.time() != time(23, 59, 59),
            "PeakScope": "requested_time_range", "ExpectedSamples": len(expected),
            "CompleteSamples": sum(row["Complete"] for row in samples),
            "CoverageComplete": coverage, "ProvisionalLogs": provisional_logs,
            "Complete": complete, "Peaks": peaks}

