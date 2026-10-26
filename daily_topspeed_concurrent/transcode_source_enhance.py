"""逐日轉碼明細、完整分頁快照快取與有限並行。"""
from concurrent.futures import CancelledError, FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

import api_client_enhance as api_client
import utils_tools_enhance as utils

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "transcode_details"
CACHE_VERSION = 1
MAX_PAGES = 100


def validate_pages(documents, body):
    """只接受完整、連續、總數一致的快照；不能將截斷的 100 頁當成功。"""
    if not isinstance(documents, list) or not documents:
        raise ValueError("Missing transcode detail pages")
    total, pages, fingerprints = None, None, set()
    count = 0
    for page, document in enumerate(documents, 1):
        rows, _ = utils.response_data(document)
        response = document["Response"]
        metadata = [response.get(field) for field in ("PageNum", "PageSize", "TotalNum", "TotalPage")]
        if any(type(value) is not int for value in metadata):
            raise ValueError("Invalid transcode pagination metadata")
        number, size, current_total, current_pages = metadata
        expected_pages = max(1, (current_total + body["PageSize"] - 1) // body["PageSize"])
        if (number != page or size != body["PageSize"] or current_total < 0
                or current_pages not in ({0, 1} if current_total == 0 else {expected_pages})):
            raise ValueError("Inconsistent transcode pagination metadata")
        if expected_pages > MAX_PAGES:
            raise ValueError("Transcode details exceed the 100-page API limit; refusing truncated results")
        if total is None:
            total, pages = current_total, expected_pages
        if current_total != total or expected_pages != pages:
            raise ValueError("Transcode pagination changed during query; retry with --refresh-transcode-cache")
        expected_rows = min(size, max(0, total - (page - 1) * size))
        if len(rows) != expected_rows:
            raise ValueError("Transcode page length does not match TotalNum")
        if rows:
            fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if fingerprint in fingerprints:
                raise ValueError("Repeated transcode page; refusing incomplete results")
            fingerprints.add(fingerprint)
        # 同樣驗證原始欄位，損壞明細不能成為有效快取。
        day = datetime.strptime(body["DayTime"], "%Y%m%d").replace(tzinfo=utils.TAIPEI)
        utils.prepare_details([(day.date().isoformat(), [document])], day, day + timedelta(days=1),
                              body.get("PushDomain"))
        count += len(rows)
    if len(documents) != pages or count != total:
        raise ValueError("Incomplete transcode pagination snapshot")
    return total


class TranscodeCache:
    """每個日期儲存整組原始成功回應，避免重跑時混用不同版本的分頁。"""
    def __init__(self, directory, secret_id, refresh=False):
        self.directory = Path(directory).expanduser()
        self.account_hash = hashlib.sha256(secret_id.encode("utf-8")).hexdigest()
        self.refresh = refresh

    def _identity(self, body):
        return {"AccountHash": self.account_hash, "Host": api_client.HOST,
                "APIVersion": api_client.VERSION, "Action": api_client.TRANSCODE_ACTION, "Body": body}

    def _path(self, identity):
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return self.directory / digest[:2] / f"{digest}.json"

    def load(self, body):
        if self.refresh:
            return None
        identity = self._identity(body)
        try:
            with self._path(identity).open(encoding="utf-8") as stream:
                record = json.load(stream)
            if record["Version"] != CACHE_VERSION or record["Query"] != identity:
                return None
            saved, expires, now = record["SavedAt"], record["ExpiresAt"], time.time()
            if type(saved) not in (int, float) or not math.isfinite(saved) or saved > now + 60:
                return None
            if expires is not None and (type(expires) not in (int, float)
                                        or not math.isfinite(expires) or now >= expires):
                return None
            validate_pages(record["Documents"], body)
            return record["Documents"]
        except (FileNotFoundError, ValueError, UnicodeError, KeyError, RuntimeError, TypeError):
            return None

    def save(self, body, documents):
        total = validate_pages(documents, body)
        identity = self._identity(body)
        path, now = self._path(identity), time.time()
        day = datetime.strptime(body["DayTime"], "%Y%m%d").replace(tzinfo=utils.TAIPEI)
        # 日結未滿 24 小時或空回應五分鐘後重查；其他歷史快照可沿用。
        expires = now + 300 if not total or (day + timedelta(days=2)).timestamp() > now else None
        record = {"Version": CACHE_VERSION, "Query": identity, "SavedAt": now,
                  "ExpiresAt": expires, "Documents": documents}
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".pending-", suffix=".json", delete=False) as output:
                temporary = Path(output.name)
                json.dump(record, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass


def query_details(client, args, cache=None):
    days = [left.date() for left, _ in utils.iter_day_ranges(args.start_time, args.end_time)]
    stopped = threading.Event()

    def query_day(day):
        if stopped.is_set():
            raise CancelledError()
        body = api_client.transcode_body(day, args.push_domain)
        initial = time.monotonic()
        documents = cache.load(body) if cache is not None else None
        hit = documents is not None
        if not hit:
            documents = []
            total_pages = 1
            page = 1
            while page <= total_pages:
                if stopped.is_set():
                    raise CancelledError()
                with utils.api_query(api_client.TRANSCODE_ACTION, utils.midnight(day),
                                     utils.midnight(day) + timedelta(days=1) - timedelta(seconds=1)):
                    document = client.transcode_details(day, args.push_domain, page)
                    rows, _ = utils.response_data(document)
                    response = document["Response"]
                    total = response.get("TotalNum")
                    if type(total) is not int or total < 0:
                        raise ValueError("Invalid transcode TotalNum")
                    expected = max(1, (total + body["PageSize"] - 1) // body["PageSize"])
                    if expected > MAX_PAGES:
                        raise ValueError("Transcode details exceed the 100-page API limit; refusing truncated results")
                    if page == 1:
                        total_pages = expected
                    elif expected != total_pages or total != documents[0]["Response"]["TotalNum"]:
                        raise ValueError("Transcode pagination changed during query")
                    if response.get("PageNum") != page or response.get("PageSize") != body["PageSize"]:
                        raise ValueError("Wrong transcode page number or size")
                    documents.append(document)
                if getattr(args, "verbose", False):
                    print(f"Transcode progress: {day} page {page}/{total_pages}, {len(rows)} records", file=sys.stderr)
                page += 1
            validate_pages(documents, body)
            if cache is not None:
                cache.save(body, documents)
        total = validate_pages(documents, body)
        return documents, {"Date": day.isoformat(), "DayTime": body["DayTime"],
                           "QueryScope": "whole_day", "CacheHit": hit, "Pages": len(documents),
                           "ReturnedRecords": total,
                           "RequestIds": [doc["Response"]["RequestId"] for doc in documents],
                           "Seconds": round(time.monotonic() - initial, 3)}

    initial_requests = client.request_count(api_client.TRANSCODE_ACTION)
    results = {}
    with ThreadPoolExecutor(max_workers=args.transcode_workers) as executor:
        futures = {executor.submit(query_day, day): day for day in days}
        try:
            pending = set(futures)
            last_progress = time.monotonic()
            while pending:
                done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for future in done:
                    results[futures[future]] = future.result()
                if not pending or time.monotonic() - last_progress >= 5:
                    print(f"Transcode days: {len(results)}/{len(days)}", file=sys.stderr)
                    last_progress = time.monotonic()
        except BaseException:
            stopped.set()
            for future in futures:
                future.cancel()
            raise
    documents = [(day.isoformat(), results[day][0]) for day in days]
    audit = {"CacheEnabled": cache is not None, "RefreshCache": cache.refresh if cache else False,
             "APIRequests": client.request_count(api_client.TRANSCODE_ACTION) - initial_requests,
             "Daily": [results[day][1] for day in days]}
    return documents, audit
