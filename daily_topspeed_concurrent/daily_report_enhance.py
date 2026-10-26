#!/usr/bin/env python3
"""
以轉碼明細與日誌計算 enhance002／enhance003 每五分鐘房間採樣峰值。

python3 ./daily_topspeed_concurrent/daily_report_enhance.py \
    --start-time "2026-10-20 00:00:00" \
    --end-time "2026-10-20 23:59:59" \
    --play-domains "tencent-global-pull-rtmp.17app.co" \
    --output ./reports/result_2026-10-20.json
"""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import urllib.error
import uuid
import zipfile
import zlib

import api_client_enhance as api_client
import log_source_enhance as log_source
import transcode_source_enhance as transcode_source
import utils_tools_enhance as utils

DEFAULT_REPORT_DIR = Path(__file__).resolve().parent / "reports"


@contextmanager
def timed_stage(timings, name):
    print(f"Stage started: {name}", file=sys.stderr)
    started, status = time.monotonic(), "failed"
    try:
        yield
        status = "completed"
    finally:
        seconds = time.monotonic() - started
        timings.append({"Stage": name, "Seconds": round(seconds, 3), "Status": status})
        print(f"Stage {status}: {name} | elapsed={utils.format_elapsed(seconds)}", file=sys.stderr)


def incomplete_reasons(day, samples, log_stats):
    """僅從既有稽核資料整理 CLI 原因，不改動完整性判定。"""
    reasons = []
    if day["ProvisionalLogs"]:
        reasons.append("provisional logs (<24h after day end)")
    if not log_stats["Files"]:
        reasons.append("no playback log files (report-wide)")
    if log_stats.get("InvalidLines", 0):
        reasons.append(f"invalid log lines={log_stats['InvalidLines']} (report-wide)")
    if not day["ExpectedSamples"]:
        reasons.append("no aligned five-minute samples")
    if any(sample["DataStatus"] == "no_data" for sample in samples):
        reasons.append("empty transcode snapshot")
    for field, label in (
        ("MissingTemplateUUIDs", "missing template"),
        ("MissingResolutionUUIDs", "missing resolution"),
        ("UnknownStreamDetailIds", "unknown stream name"),
    ):
        count = sum(bool(sample[field]) for sample in samples)
        if count:
            reasons.append(f"{label} at {count} samples")
    if not reasons:
        reasons.append("incomplete sample coverage; inspect JSON")
    return reasons


def warning_summary(document):
    """前兩類有結構化明細，其餘按既有診斷訊息分組；不改 JSON。"""
    close = len(document["CloseTemplateWarnings"])
    resolutions = len(document["ResolutionWarnings"])
    groups = []
    if close:
        groups.append(("Close enhance templates; both templates remain counted", close))
    if resolutions:
        groups.append(("Overlapping resolutions; maximum selected; rooms remain counted", resolutions))
    groups.extend(Counter(document["Warnings"][close + resolutions:]).items())
    return groups


def print_summary(document, path, elapsed=None):
    samples_by_day = defaultdict(list)
    for sample in document["Samples"]:
        samples_by_day[sample["Time"][:10]].append(sample)
    print("Observed sampled peaks (rooms; Taipei UTC+8; groups peak independently)")
    print("E003=enhance003, E002=enhance002; '-' means no sampled peak.")
    print(f"{'Date':<10}  {'Samples':>7}  {'Status':<10}  "
          f"{'E003/720p':>9}  {'E003/1080p':>10}  {'E002/720p':>9}  {'E002/1080p':>10}  Notes")
    for day in document["Daily"]:
        peaks = [day["Peaks"][group]["ObservedPeakRooms"] for group in utils.GROUPS]
        peaks = [str(peak) if peak is not None else "-" for peak in peaks]
        notes = []
        if day["PartialDay"]:
            notes.append(f"partial range {day['StartTime'][11:]}–{day['EndTime'][11:]}")
        if not day["Complete"]:
            notes.extend(incomplete_reasons(day, samples_by_day[day["Date"]], document["LogAudit"]["Stats"]))
        status = "COMPLETE" if day["Complete"] else "INCOMPLETE"
        coverage = f"{day['CompleteSamples']}/{day['ExpectedSamples']}"
        print(f"{day['Date']}  {coverage:>7}  {status:<10}  "
              f"{peaks[0]:>9}  {peaks[1]:>10}  {peaks[2]:>9}  {peaks[3]:>10}  {'; '.join(notes)}".rstrip())
    complete_days = sum(day["Complete"] for day in document["Daily"])
    print(f"Days: {complete_days} complete / {len(document['Daily']) - complete_days} incomplete")
    print(f"Warnings: {len(document['Warnings'])} total; full details are in the JSON report.")
    for label, count in warning_summary(document):
        print(f"  {count}: {label}")
    if not document["Complete"]:
        print("Inputs incomplete/provisional: observed peaks may be lower bounds; inspect JSON. Not for billing.")
    else:
        print("Sampled estimates under report assumptions; not for billing.")
    if elapsed is not None:
        print(f"Total elapsed: {utils.format_elapsed(elapsed)}")
    print(f"Full JSON report: {path}")


def write_report(document, path):
    """0600 暫存檔原子替換；失敗不破壞既有報表。"""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".pending-", suffix=".json", delete=False) as output:
            temporary = Path(output.name)
            json.dump(document, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def generate_report(args, client, now=None, cache=None):
    now = now if now is not None else datetime.now(utils.TAIPEI)
    timings, started = [], time.monotonic()
    with timed_stage(timings, "Transcode details"):
        documents, query_audit = transcode_source.query_details(client, args, cache)
        # documents
        # [
        #     ("2026-10-01", [第一頁回應, 第二頁回應]),
        #     ("2026-10-02", [第一頁回應]),
        # ]
        details, detail_stats = utils.prepare_details(documents, args.start_time, args.end_time, args.push_domain)
        # details = [
        # {
        #     "DetailId": "2b4828ae9aa348706aa91a8d180b68ec269aa916bcccc08860c7f4bbef9205ea",
        #     "UUID": "50da4d31-de76-4a98-8cd1-b1ac663b173f",  # 從 StreamName 解析、正規化
        #     "StreamName": "50da4d31-de76-4a98-8cd1-b1ac663b173f",
        #     "PushDomain": "tencent-global-push-rtmp.17app.co",
        #     "StartTime": "2026-10-01 23:59",  # 保留 API 原始值
        #     "EndTime": "2026-10-01 23:59",
        #     "Duration": 1,                  # 原始累計分鐘數，不用來推算有效終點
        #     "Resolution": "480P",
        #     "Type": "Transcode",
        #     "ModuleCodec": "liveprocessor_H264",
        #     "Bitrate": 800,
        #     "MainlandOrOversea": "Oversea",
        #     "Bucket": 720,                  # 480P 歸入 720p 桶
        #     "SourceDays": ["2026-10-01"],    # 這筆資料出現在哪些日期的查詢回應中
        #     "EffectiveStartTime": "2026-10-01 23:59:00",
        #     "EffectiveEndTime": "2026-10-01 23:59:59",
        #     "EffectiveEndInclusive": True,  # 裁切後包含查詢終點 23:59:59
        #     "MinuteIntervalExpanded": True,
        #     "_Start": datetime(2026, 10, 1, 23, 59, tzinfo=utils.TAIPEI),
        #     "_End": datetime(2026, 10, 1, 23, 59, 59, tzinfo=utils.TAIPEI),
        #     "_Evidence": {},                # 待日誌歸因時填入證據摘要
        #     "_Times": {},                   # 待日誌歸因時填入分類時間點與證據
        # }
        #     {
        #         # 第二筆轉碼明細……
        #     },
        # ]
    with ExitStack() as stack:
        with timed_stage(timings, "Log acquisition"):
            paths, acquisition = stack.enter_context(log_source.acquire_logs(args, client, now))
        with timed_stage(timings, "Log parsing and detail template attribution"):
            lines = (record for path, name in zip(paths, acquisition["FileNames"]) # here
                     for record in log_source.iter_records(path, name))
            # lines # 每次 yield 一組 tuple (source, number, line)
            # ("test.log.gz", 1, "第一行\n")
            # ("test.log.gz", 2, "第二行\n")
            log_audit, near_warnings = utils.attribute_templates(
                lines, details, set(args.play_domains), args.start_time, args.end_time,
            )
            # log_audit, near_warnings
            # (
            #     {
            #         "Stats": {
            #             "ReadLines": 2,
            #             "AcceptedLines": 2,
            #             "UUIDs": 1,
            #         },
            #         "StatusCounts": {"200": 2},
            #         "ClassificationCounts": {"dual": 1},
            #         "ClassificationUnit": "transcode_detail",
            #         "EvidenceRetention": "first/last/count per category/detail and closest dual-template pair; original URLs",
            #     },
            #     [
            #         {
            #             "DetailId": "detail-001",
            #             "UUID": "c1381c3c-276f-4345-ab43-eb7494b8c981",
            #             "StartTime": "2026-10-14 10:00:00",
            #             "EndTime": "2026-10-14 10:30:00",
            #             "GapSeconds": 60.0,
            #             "Evidence": {
            #                 "enhance003": B,  # 10:10 的完整日誌 record
            #                 "enhance002": E,  # 10:11 的完整日誌 record
            #             },
            #         },
            #     ],
            # )
            log_audit["Stats"]["Files"] = len(paths)
            log_audit.update(acquisition)
    invalid_logs = log_audit["Stats"].get("InvalidLines", 0) > 0 or not log_audit["Stats"]["Files"]
    no_data_days = {day["Date"] for day in query_audit["Daily"] if day["ReturnedRecords"] == 0}
    with timed_stage(timings, "Five-minute sampling and daily peaks"):
        resolution_warnings = []
        samples = utils.aggregate_samples(details, args.start_time, args.end_time, invalid_logs, no_data_days,
                                          resolution_warnings=resolution_warnings)
        by_day = defaultdict(list)
        for sample in samples:
            by_day[sample["Time"][:10]].append(sample)
        daily = []
        for start, end in utils.iter_day_ranges(args.start_time, args.end_time):
            provisional = utils.midnight(start.date()) + timedelta(days=2) > now
            daily.append(utils.summarize_day(start, end, by_day[start.date().isoformat()],
                                             invalid_logs, provisional))
    warnings = []
    for warning in near_warnings:
        message = (f"Close enhance templates: UUID={warning['UUID']}, DetailId={warning['DetailId']}, "
                   f"gap={warning['GapSeconds']:g}s <= {utils.TEMPLATE_WARNING_SECONDS}s; "
                   "both templates remain counted for the entire detail interval.")
        warnings.append(message)
    for warning in resolution_warnings:
        resolutions = sorted({row["Resolution"] for row in warning["Details"]})
        message = (f"Overlapping transcode resolutions: UUID={warning['UUID']}, "
                   f"StreamNames={','.join(warning['StreamNames'])}, "
                   f"resolutions={','.join(resolutions)}; selected maximum={warning['SelectedResolution']} "
                   f"(bucket={warning['SelectedBucket']}p) at {len(warning['SampleTimes'])} samples; "
                   f"WarningId={warning['WarningId']}. Warning only; room remains counted.")
        warnings.append(message)
    if any(day["ProvisionalLogs"] for day in daily):
        warnings.append("Recent dates have provisional logs; PeakRooms is withheld until 24 hours after day end.")
    if any(day["PartialDay"] for day in daily):
        warnings.append("Partial-day peaks cover only the requested range; templates use only in-range log evidence.")
    if any(day["ExpectedSamples"] == 0 for day in daily):
        warnings.append("Some daily ranges contain no aligned five-minute samples; their peaks are null, not zero.")
    if no_data_days:
        warnings.append("Empty transcode detail snapshots are not confirmed zero-room days: " + ", ".join(sorted(no_data_days)))
    if not log_audit["Stats"]["Files"]:
        warnings.append("No playback log files were supplied or downloaded; template coverage is unverified.")
    if log_audit["Stats"].get("InvalidLines", 0):
        warnings.append("Some playback log lines could not be parsed; inspect LogAudit.Stats.InvalidLines.")
    for field, message in (
        ("MissingTemplateUUIDs", "Some active details have no usable template evidence."),
        ("MissingResolutionUUIDs", "Some active enhance rooms have no usable transcode resolution."),
        ("UnknownStreamDetailIds", "Some active transcode stream names cannot be mapped to a UUID."),
    ):
        if any(sample[field] for sample in samples):
            warnings.append(message)
    complete = all(day["Complete"] for day in daily)
    if not complete:
        warnings.append("Report inputs are incomplete; inspect Daily, Samples and TranscodeDetails.")
    if getattr(args, "verbose", False):
        for message in warnings:
            print(f"WARNING: {message}", file=sys.stderr)
    for detail in details:
        detail.pop("_Start")
        detail.pop("_End")
    return {
        "SchemaVersion": 2,
        "SourceAPIs": [api_client.TRANSCODE_ACTION, api_client.LOG_ACTION],
        "Metric": "sampled_transcode_room_peaks", "Unit": "rooms", "DayTimezone": "+08:00",
        "StartTime": args.start_time.strftime(utils.TIME_FORMAT),
        "EndTime": args.end_time.strftime(utils.TIME_FORMAT),
        "PlayDomains": args.play_domains, "PushDomain": args.push_domain, "IsFastLive": args.is_fast_live,
        "SampleIntervalMinutes": 5,
        "SamplingRule": "aligned five-minute points within effective detail intervals; EffectiveEndInclusive controls the end boundary",
        "DetailIntervalRule": "inclusive raw start/end, except equal minute-precision start/end expand to [minute, next minute); clipped to inclusive query range; Duration is not used",
        "TranscodeTypeFilter": "Transcode", "ModuleCodecFilter": None,
        "ResolutionRule": {"720p": "short side <= 720 or resolution tier <= 720P",
                           "1080p": "short side > 720 or resolution tier > 720P"},
        "ResolutionAttribution": "transcode detail Resolution is treated as source resolution",
        "ResolutionConflictRule": "at each sample, warn on differing resolutions among active enhance-attributed details of the same UUID; count all attributed templates using the maximum resolution; warning alone does not affect completeness",
        "ResolutionRankingRule": "short side or numeric P tier; nominal heights 2K=1440, 4K=2160, 8K=4320; dimension ties use long side",
        "TemplateAttribution": "all accepted log templates within each detail interval apply to its entire interval; dual templates both count",
        "TemplateEvidenceHTTPStatuses": "2xx only for all categories",
        "TemplateWarningThresholdSeconds": utils.TEMPLATE_WARNING_SECONDS,
        "TemplateWarningRule": "minimum cross-template log time difference <= threshold; warning only, no exclusion",
        "QueryBoundaryRule": "detail API queries whole Taipei dates; active intervals and log evidence clipped to inclusive requested range",
        "BusinessAssumptions": [
            "A transcode detail record is evidence of pulling/viewing activity.",
            "Each detail StartTime/EndTime is treated as a continuous interval, ignoring interruptions and Duration gaps.",
            "Transcode detail Resolution is treated as source resolution, not verified push opening resolution.",
            "Equal minute-precision start/end represent the full minute, not a single instant.",
            "Overlapping enhance-attributed resolutions use their maximum for room grouping, with warnings only.",
        ],
        "Completeness": "not_verified", "Complete": complete,
        "TranscodeWorkers": args.transcode_workers, "TranscodeQueryAudit": query_audit,
        "TranscodeStats": detail_stats, "TranscodeDetails": details,
        "CloseTemplateWarnings": near_warnings, "ResolutionWarnings": resolution_warnings, "LogAudit": log_audit,
        "APIRateLimit": client.rate_limit_status(), "LogWorkers": args.log_workers,
        "StageTimings": timings, "GenerationSeconds": round(time.monotonic() - started, 3),
        "Daily": daily, "Samples": samples, "Warnings": warnings,
    }


def report_path(args):
    path = args.output
    if path is None:
        name = (f"topspeed_{args.start_time.strftime('%Y%m%d_%H%M%S')}_"
                f"{args.end_time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex}.json")
        path = DEFAULT_REPORT_DIR / name
    path = path.expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_dir():
        raise ValueError("Output path is a directory")
    with tempfile.TemporaryFile(dir=path.parent):
        pass
    return path


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-time", required=True, type=utils.taipei_time,
                        help="開始時間（含），台北時間 YYYY-MM-DD HH:MM:SS；限近一個月")
    parser.add_argument("--end-time", required=True, type=utils.taipei_time,
                        help="結束時間（含），至少相隔五分鐘；最晚昨天 23:59:59")
    parser.add_argument("--play-domains", required=True, type=utils.csv_list,
                        help="逗號分隔的播放域名；用於日誌清單與日誌證據篩選")
    parser.add_argument("--push-domain", default="tencent-global-push-rtmp.17app.co",
                        help="轉碼明細的單一來源推流域名")
    parser.add_argument("--request-interval", type=float, default=0.1,
                        help="全體 API worker 共用的最短間隔秒數，0.025～60；預設 0.1")
    parser.add_argument("--is-fast-live", type=int, choices=[0, 1], default=0,
                        help="日誌類型：0 標準直播、1 快直播；不是 enhance 模板")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--log-files", nargs="+", help="本地 plain／gzip／ZIP／CLS JSONL 日誌")
    source.add_argument("--log-list", nargs="+", help="既有 DescribeLogDownloadList 原始 JSON；URL 須有效")
    parser.add_argument("--log-workers", type=int, default=3, help="下載並行數，1～10；預設 3")
    parser.add_argument("--transcode-workers", type=int, default=3, help="不同日期明細並行數，1～10；預設 3")
    parser.add_argument("--transcode-cache-dir", type=Path, default=transcode_source.DEFAULT_CACHE_DIR,
                        help="轉碼明細完整分頁快照，預設腳本 .cache/transcode_details")
    cache = parser.add_mutually_exclusive_group()
    cache.add_argument("--no-transcode-cache", action="store_true", help="不讀寫明細快取")
    cache.add_argument("--refresh-transcode-cache", action="store_true", help="重查並更新本次日期的明細快取")
    parser.add_argument("--output", type=Path, help="完整 JSON 路徑；省略則使用 reports/ 下唯一檔名")
    parser.add_argument("--verbose", action="store_true", help="列印逐筆 warning 與明細分頁進度；預設只顯示彙總")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not api_client.MIN_REQUEST_INTERVAL <= args.request_interval <= 60:
        parser.error("--request-interval must be in [0.025, 60]")
    for name in ("log_workers", "transcode_workers"):
        if not 1 <= getattr(args, name) <= 10:
            parser.error(f"--{name.replace('_', '-')} must be in [1, 10]")
    try:
        domains = utils.csv_list(args.push_domain)
        if len(domains) != 1:
            raise ValueError("--push-domain accepts exactly one source domain")
        args.push_domain = domains[0]
        now = datetime.now(utils.TAIPEI)
        utils.validate_time_range(args.start_time, args.end_time, now)
    except (ValueError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    try:
        secret_id, secret_key = api_client.credentials()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2
    started, client = time.monotonic(), None
    report_written = False
    try:
        destination = report_path(args)
        print(f"Full JSON output: {destination}", file=sys.stderr)
        client = api_client.LiveClient(secret_id, secret_key, args.request_interval)
        cache = None if args.no_transcode_cache else transcode_source.TranscodeCache(
            args.transcode_cache_dir, secret_id, args.refresh_transcode_cache,
        )
        report = generate_report(args, client, now, cache) # here
        with timed_stage([], "Report write"):
            write_report(report, destination)
        report_written = True
    except urllib.error.HTTPError as error:
        print(f"{error} | URL={error.url}", file=sys.stderr)
        error.close()
        return 1
    except (OSError, HTTPException, ValueError, OverflowError, EOFError,
            zipfile.BadZipFile, zlib.error, RuntimeError) as error:
        print(str(error) or type(error).__name__, file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
        elapsed = time.monotonic() - started
        if not report_written:
            print(f"Total elapsed: {utils.format_elapsed(elapsed)}", file=sys.stderr)
    print_summary(report, destination, elapsed)
    return 0 if report["Complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
