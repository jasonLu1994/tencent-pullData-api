"""日誌取得與串流解壓；臨時下載自動清除，不永久保存 URL 或原始 API 資料。"""
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import gzip
from http.client import HTTPException
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import api_client_enhance as api_client
import utils_tools_enhance as utils


@contextmanager
def acquire_logs(args, client, now=None):
    """本地檔不修改；下載資料僅存於此次執行的臨時目錄，失敗也清理。"""
    # 第一步：若直接提供日誌檔，交給呼叫端讀取，不查清單也不下載。
    if args.log_files:
        yield [Path(path) for path in args.log_files], {
            "Source": "local_files", "RequestIds": [], "FileNames": [str(path) for path in args.log_files],
        }
        return
    # 第二步：只查指定起訖；單次最多七天，支援明細 API 的近一個月範圍。
    entries, request_ids, query_ranges = [], [], []
    if args.log_list:
        for path in args.log_list:
            with Path(path).open(encoding="utf-8") as stream:
                rows, request_id = log_entries(json.load(stream))
                # rows
                # [
                #     (
                #         "http://livelog-down.cdn.qcloud.com/20261001/00/2026100100-tencent-global-pull-rtmp.17app.co.gz?st=MUlsWh4Q5x4-B3oO4bKtKA&e=1792210489",
                #         datetime(2026, 10, 1, 0, 0, tzinfo=TAIPEI),
                #         "2026100100-tencent-global-pull-rtmp.17app.co.gz",
                #     ),
                #     (
                #         "http://livelog-down.cdn.qcloud.com/20261001/01/2026100101-tencent-global-pull-rtmp.17app.co.gz?st=Dytm_VcdsNZWM26pcPdohQ&e=1792210489",
                #         datetime(2026, 10, 1, 1, 0, tzinfo=TAIPEI),
                #         "2026100101-tencent-global-pull-rtmp.17app.co.gz",
                #     ),
                #     # ……其餘 23 筆
                # ]
            entries.extend(rows)
            request_ids.append(request_id)
        source = "local_log_lists"
    else:
        source = "api_log_lists"
        now = now if now is not None else datetime.now(utils.TAIPEI)
        start, end = args.start_time, min(args.end_time, now)
        while start < end:
            stop = min(start + timedelta(days=7), end)
            query_end = stop
            # 內部整點邊界的小時包留給下一段；只縮短清單查詢，不改統計起訖。
            if stop < end and stop.minute == stop.second == stop.microsecond == 0:
                query_end = stop - timedelta(minutes=1)
            with utils.api_query(api_client.LOG_ACTION, start, query_end):
                document = client.log_list(start, query_end, args.play_domains, args.is_fast_live)
                rows, request_id = log_entries(document)
            entries.extend(rows)
            request_ids.append(request_id)
            query_ranges.append({"StartTime": start.strftime(utils.TIME_FORMAT),
                                 "EndTime": query_end.strftime(utils.TIME_FORMAT)})
            start = stop
    # 第三步：相同 URL 不重複下載，但不任意去掉不同 URL 的日誌。 # here
    names = {}
    for url, point, name in entries:
        # 既有清單也只下載與指定區間相交的小時包；套件內較寬的行由解析層再次裁切。
        if point <= args.end_time and point + timedelta(hours=1) > args.start_time:
            names.setdefault(url, name)
    urls = list(names)
    with tempfile.TemporaryDirectory(prefix="daily-topspeed-logs-") as directory:
        paths = download_logs(urls, directory, args.log_workers)
        # 第四步：yield 期間暫存檔仍有效；呼叫端離開 with（含例外）後自動清除。
        # ListedFiles 是去重前的清單筆數，實際下載檔數由呼叫端另行記錄。
        yield paths, {"Source": source, "RequestIds": request_ids, "ListedFiles": len(entries),
                      "FileNames": list(names.values()), "SelectedFiles": len(names),
                      "QueryRanges": query_ranges,
                      "QueryStartTime": args.start_time.strftime(utils.TIME_FORMAT),
                      "QueryEndTime": args.end_time.strftime(utils.TIME_FORMAT)}

def log_entries(document):
    """驗證日誌清單，回傳 (URL, 時間, 原始檔名) 清單與請求 ID。"""
    # 第一步：檢查 API 外層與宣告筆數，不將筆數不符的清單當成完整輸入。
    rows, request_id = utils.response_data(document, "LogInfoList")
    total = document["Response"].get("TotalNum")
    if type(total) is not int or total != len(rows):
        raise ValueError("Log list TotalNum does not match LogInfoList")
    # 第二步：逐筆驗證必要欄位；下載只接受不含帳密及 fragment 的 HTTP(S) 網址。
    entries = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid log list record")
        url, name, size = row.get("LogUrl"), row.get("LogName"), row.get("FileSize")
        if not isinstance(url, str) or not isinstance(name, str) or not name or type(size) is not int or size < 0:
            raise ValueError("Log list record is missing URL, name or file size")
        try:
            parsed = urllib.parse.urlsplit(url)
            valid_url = (parsed.scheme in ("http", "https") and parsed.hostname
                         and not parsed.username and not parsed.password and not parsed.fragment)
            parsed.port  # 存取 port 會觸發格式及範圍驗證；不合法時拋出 ValueError。
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ValueError("Log list contains an invalid HTTP(S) download URL")
        point = utils.parse_api_time(row.get("LogTime"))
        entries.append((url, point, name))
    return entries, request_id


def download(url, path):
    """串流寫入指定暫存檔；失敗時保留下載網址及原始錯誤。"""
    # 與 API 的 Request 完全分開：只交付下載 URL，不附帶 TC3 或其他憑證。
    request = urllib.request.Request(url, headers={"User-Agent": "daily-topspeed-concurrent/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with Path(path).open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
    except (OSError, HTTPException) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        raise RuntimeError(f"Download failed: URL={url} | {error}") from error


def download_logs(urls, directory, workers):
    """有限並行下載，仍按原始清單順序解析；失敗先停 worker 再清理暫存檔。"""
    paths = [Path(directory) / f"{index:06d}.logdata" for index in range(len(urls))]
    started = last_progress = time.monotonic()
    completed = 0

    def progress():
        print(f"Log download progress: {completed}/{len(paths)} files | workers={workers} | "
              f"elapsed={utils.format_elapsed(time.monotonic() - started)}", file=sys.stderr)

    progress()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        try:
            for url, path in zip(urls, paths):
                futures.append(executor.submit(download, url, path))
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    future.result()
                    completed += 1
                if not pending or time.monotonic() - last_progress >= 5:
                    progress()
                    last_progress = time.monotonic()
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return paths


def iter_records(path, source=None):
    """串流回傳 (檔名, 行號, 文字)；ZIP 保留成員名稱與各成員的行號。"""
    source = str(path) if source is None else source
    # 第一步：檢查檔案內容而非副檔名，讀完檔頭後回到起點以免遺漏資料。
    with Path(path).open("rb") as raw:
        magic = raw.read(4)
        raw.seek(0)
        # 第二步：逐行解碼 UTF-8（可含 BOM）；ZIP 逐一讀取非目錄成員，不整包載入。
        if magic[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=raw) as compressed:
                with io.TextIOWrapper(compressed, encoding="utf-8-sig") as text:
                    for number, line in enumerate(text, 1):
                        yield source, number, line
        elif zipfile.is_zipfile(raw):
            raw.seek(0)
            with zipfile.ZipFile(raw) as archive:
                for entry in archive.infolist():
                    if not entry.is_dir():
                        with archive.open(entry) as member:
                            with io.TextIOWrapper(member, encoding="utf-8-sig") as text:
                                for number, line in enumerate(text, 1):
                                    yield f"{source}!{entry.filename}", number, line
        else:
            raw.seek(0)
            with io.TextIOWrapper(raw, encoding="utf-8-sig") as text:
                for number, line in enumerate(text, 1):
                    yield source, number, line








