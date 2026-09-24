import http.client
import json
import math
import re
import time
from urllib import error, request


MAX_ATTEMPTS = 3
TIMEOUT_SECONDS = 15
MAX_RETRY_AFTER_SECONDS = 30
MAX_RESPONSE_BYTES = 256 * 1024
_WEBHOOK_URL = re.compile(
    r"https://discord\.com/api/webhooks/[0-9]{1,20}/[A-Za-z0-9_-]+"
)
_MESSAGE_ID = re.compile(r"[0-9]{1,20}")


class WebhookError(RuntimeError):

    def __init__(self, message: str, *, delivery_uncertain: bool = False):
        super().__init__(message)
        self.delivery_uncertain = delivery_uncertain


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_body(response) -> bytes:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise WebhookError(
            "Discord response was too large; delivery could not be confirmed.",
            delivery_uncertain=True,
        ) from None
    return body


def _post_once(opener, req):
    try:
        with opener.open(req, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _read_body(response), response.headers
    except error.HTTPError as exc:
        with exc:
            return exc.code, _read_body(exc), exc.headers


def _retry_delay(body: bytes, headers) -> float:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        payload = None
    delay = payload.get("retry_after") if isinstance(payload, dict) else None
    if not isinstance(delay, (int, float)) or isinstance(delay, bool):
        delay = headers.get("Retry-After") if headers else None
    try:
        delay = float(delay)
    except (ValueError, TypeError, OverflowError):
        raise WebhookError("Discord rate limit did not provide a valid retry delay.") from None
    if not math.isfinite(delay) or not 0 <= delay <= MAX_RETRY_AFTER_SECONDS:
        raise WebhookError("Discord rate limit exceeds the automatic retry window.")
    return delay


def send_message(webhook_url: str, content: str, *, username: str) -> str:
    if not isinstance(webhook_url, str) or not _WEBHOOK_URL.fullmatch(webhook_url):
        raise WebhookError("DISCORD_WEBHOOK_URL must be a valid HTTPS Discord webhook URL.")
    if not isinstance(content, str) or not content.strip() or len(content) > 2000:
        raise WebhookError("Discord message content must contain 1 to 2000 characters.")
    if not isinstance(username, str) or not username.strip() or len(username) > 80:
        raise WebhookError("Discord username must contain 1 to 80 characters.")
    try:
        body = json.dumps(
            {"username": username, "content": content, "allowed_mentions": {"parse": []}},
            ensure_ascii=False,
        ).encode("utf-8")
    except UnicodeError:
        raise WebhookError("Discord message content must be valid Unicode.") from None

    req = request.Request(
        webhook_url + "?wait=true",
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "CS-Retrospective-Study/1.0",
        },
        method="POST",
    )
    opener = request.build_opener(_NoRedirect())
    for attempt in range(MAX_ATTEMPTS):
        try:
            status, response_body, headers = _post_once(opener, req)
        except (error.URLError, OSError, http.client.HTTPException):
            # 이미 전송됐을 수 있어 연결 오류는 재시도하지 않습니다.
            raise WebhookError(
                "Discord connection failed; delivery is unknown. Check the channel before replaying.",
                delivery_uncertain=True,
            ) from None

        if status == 200:
            try:
                response = json.loads(response_body)
            except (ValueError, UnicodeError, RecursionError):
                response = None
            message_id = response.get("id") if isinstance(response, dict) else None
            if isinstance(message_id, str) and _MESSAGE_ID.fullmatch(message_id):
                return message_id
            raise WebhookError(
                "Discord returned no valid message acknowledgment. Check the channel before replaying.",
                delivery_uncertain=True,
            )

        if status == 429 or 500 <= status <= 599:
            if attempt == MAX_ATTEMPTS - 1:
                raise WebhookError(
                    "Discord rejected delivery after the automatic retry limit (HTTP %d)." % status,
                    delivery_uncertain=status >= 500,
                )
            delay = _retry_delay(response_body, headers) if status == 429 else 2 ** attempt
            time.sleep(delay)
            continue

        if 200 <= status <= 299:
            raise WebhookError(
                "Discord returned an unexpected success response. Check the channel before replaying.",
                delivery_uncertain=True,
            )
        raise WebhookError("Discord rejected delivery (HTTP %d)." % status)

    raise AssertionError("Unreachable retry state")
