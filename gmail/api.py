"""Gmail REST GETs, conservative quota bucket and bounded retry."""
import datetime as dt
from email.utils import parsedate_to_datetime
import json
import re
import time
from urllib import error, parse, request
from tools.archiveio import CollectorError


class Bucket:
    def __init__(self, sleep=time.sleep, clock=time.monotonic):
        self.sleep, self.clock = sleep, clock
        self.tokens, self.capacity, self.rate, self.last = 20.0, 20.0, 5000 / 60, clock()

    def take(self, units):
        current = self.clock()
        self.tokens = min(self.capacity, self.tokens + max(0, current - self.last) * self.rate)
        self.last = current
        delay = max(0, (units - self.tokens) / self.rate)
        if delay:
            self.sleep(delay)
        self.tokens = max(0, self.tokens - units)
        self.last = self.clock()


def retry_delay(value, attempt):
    delay = 2 ** attempt
    if value:
        try:
            delay = max(delay, float(value))
        except ValueError:
            try:
                delay = max(delay, (parsedate_to_datetime(value) - dt.datetime.now(dt.timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return max(0, delay)


class GmailClient:
    def __init__(self, credential, opener=request.urlopen, sleep=time.sleep, clock=time.monotonic):
        self.credential, self.opener, self.sleep = credential, opener, sleep
        self.bucket, self.requests = Bucket(sleep, clock), 0

    def request(self, path, query=None):
        if not re.fullmatch(r"/(profile|labels|messages|history|messages/[0-9a-f]{1,16})", path):
            raise CollectorError("bad_endpoint")
        units = 20 if path.startswith("/messages/") else {"/messages": 5, "/history": 2}.get(path, 1)
        url = "https://gmail.googleapis.com/gmail/v1/users/me" + path
        if query:
            url += "?" + parse.urlencode(query, doseq=True)
        req = request.Request(url, headers={"Authorization": "Bearer " + self.credential})
        for attempt in range(6):
            self.bucket.take(units)
            self.requests += 1
            try:
                with self.opener(req, timeout=60) as response:
                    obj = json.loads(response.read())
                if not isinstance(obj, dict):
                    raise CollectorError("bad_response")
                return obj
            except error.HTTPError as e:
                try:
                    obj = json.loads(e.read())
                    reasons = {v.get("reason") for v in obj.get("error", {}).get("errors", []) if isinstance(v, dict)}
                except (ValueError, AttributeError, TypeError):
                    reasons = set()
                finally:
                    e.close()
                retryable = e.code == 429 or e.code == 403 and bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})
                if (retryable or e.code in (500, 502, 503, 504)) and attempt < 5:
                    self.sleep(retry_delay(e.headers.get("Retry-After"), attempt))
                    continue
                raise CollectorError("auth_required" if e.code == 401 else "api_error", e.code) from None
            except (error.URLError, OSError, ValueError):
                raise CollectorError("transport_error") from None
        raise CollectorError("api_error")


def pages(client, path, query=None, field="messages"):
    cursor, seen = None, set()
    while True:
        q = dict(query or {})
        if cursor:
            q["pageToken"] = cursor
        obj = client.request(path, q)
        rows = obj.get(field, [])
        if not isinstance(rows, list) or not all(isinstance(v, dict) for v in rows):
            raise CollectorError("bad_response")
        yield obj, rows
        cursor = obj.get("nextPageToken")
        if cursor is None:
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise CollectorError("bad_response")
        seen.add(cursor)
