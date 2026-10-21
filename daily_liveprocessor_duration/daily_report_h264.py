#!/usr/bin/env python3
"""按台北起訖時間查詢普通 H.264 轉碼總量，依五種解析度輸出每日累計分鐘數。

Example:
  python3 daily_liveprocessor_duration/daily_report_h264.py \
    --start-time "2026-10-01 00:00:00" \
    --end-time "2026-10-01 23:59:59" \
    --push-domains "tencent-global-push-rtmp.17app.co"
"""
import argparse
from http.client import HTTPException
import json
import sys
import urllib.error

import api_client_h264 as api_client
import utils_tools_h264 as utils


def build_parser():
    """接受 UTC+8 起訖時間與總量 API 支援的篩選條件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-time", required=True, type=utils.taipei_time,
                        help="開始時間，台北時間 YYYY-MM-DD HH:MM:SS（UTC+8）")
    parser.add_argument("--end-time", required=True, type=utils.taipei_time,
                        help="結束時間，YYYY-MM-DD HH:MM:SS（UTC+8）；不可晚於現在")
    parser.add_argument("--push-domains", type=utils.csv_list,
                        help="選填：逗號分隔的推流域名；省略查全部域名")
    parser.add_argument("--mainland-or-oversea", choices=["Mainland", "Oversea"],
                        help="選填：境內／境外；省略查全球")
    return parser


def generate_report(args, secret_id, secret_key):
    """逐日查詢 → 驗證與累計 → 組合報表；全部成功後才讓呼叫端輸出。"""
    # 第一步：逐日查詢，首尾日保留指定時間，不讓相鄰查詢共用午夜端點。
    daily = []
    for start, end in utils.iter_day_ranges(args.start_time, args.end_time):
        start_text = start.strftime("%Y-%m-%d %H:%M:%S")
        end_text = end.strftime("%Y-%m-%d %H:%M:%S")
        print(f"Querying {start_text} to {end_text} (Taipei UTC+8)", file=sys.stderr)
        document = api_client.describe_live_transcode_total_info(
            secret_id, secret_key, start, end, args.push_domains, args.mainland_or_oversea,
        )
        # 第二步：只加總 API 已分桶的 Duration，不重建或切割跨日連線。
        row = utils.summarize_day(document, start.date())
        row.update({
            "StartTime": start_text,
            "EndTime": end_text,
            "PartialDay": (start.hour, start.minute, start.second) != (0, 0, 0)
                          or (end.hour, end.minute, end.second) != (23, 59, 59),
        })
        daily.append(row)

    # 第三步：保留資料來源、單位、篩選條件及完整性限制，不把用量當成觀眾時長。
    warnings = [
        "Duration is API-reported transcoding minutes, not viewer watch time or template identification.",
        "Data completeness and midnight bucket attribution have not been independently verified.",
    ]
    for row in daily:
        if row["PartialDay"]:
            warnings.append(
                f"{row['Date']}: partial-day query; API bucket boundaries may not match "
                "the requested times exactly; durations are not prorated."
            )
        if row["DataStatus"] == "no_data":
            warnings.append(f"{row['Date']}: empty API response; zeros are not confirmed zero usage.")
    return {
        "SourceAPI": api_client.ACTION,
        "Metric": "transcode_duration",
        "Unit": "minutes",
        "DayTimezone": "+08:00",
        "StartTime": args.start_time.strftime("%Y-%m-%d %H:%M:%S"),
        "EndTime": args.end_time.strftime("%Y-%m-%d %H:%M:%S"),
        "ModuleCodec": utils.MODULE_CODEC,
        "PushDomains": args.push_domains,
        "MainlandOrOversea": args.mainland_or_oversea or "Global",
        "Completeness": "not_verified",
        "Daily": daily,
        "Warnings": warnings,
    }


def main(argv=None):
    # 第一步：驗證起訖時間及選填條件；允許部分日期，不接受未來時間。
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        utils.validate_time_range(args.start_time, args.end_time)
    except ValueError as error:
        parser.error(str(error))

    # 第二步：交由 API 層讀取憑證，報表及進度均不包含憑證或簽章。
    try:
        secret_id, secret_key = api_client.credentials()
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        # 第三步：所有日期完成才輸出一次 JSON，不將部分成功結果偽裝成完整報表。
        report = generate_report(args, secret_id, secret_key)
    except urllib.error.HTTPError as error:
        print(f"HTTP {error.code}: request failed", file=sys.stderr)
        error.close()
        return 1
    except urllib.error.URLError:
        print("Network request failed; check connectivity and retry", file=sys.stderr)
        return 1
    except HTTPException:
        print("HTTP response failed; retry the query", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"Cannot build daily report: {error}", file=sys.stderr)
        return 1

    # 第四步：stdout 僅輸出報表，警告走 stderr；檔案保存方式由執行者決定。
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    for warning in report["Warnings"]:
        print(f"Warning: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
