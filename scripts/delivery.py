"""단일 실행 환경에서 공유하는 최소 발송 기록. 메시지 본문은 저장하지 않는다."""

from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re

from scripts.discord_webhook import WebhookError


class DeliveryError(RuntimeError):
    pass


def event_key(event):
    return f"{event['deadline']}/{event['kind']}/{event['scheduled_at']}"


def runtime_directory(root):
    configured = os.environ.get("STUDY_RUNTIME_DIR")
    directory = Path(configured) if configured else root / ".study"
    if not directory.is_absolute() or directory.is_symlink():
        raise DeliveryError("STUDY_RUNTIME_DIR에는 영구 보관할 실제 폴더의 절대 경로를 지정하세요.")
    return directory


def atomic_json(path, data):
    if path.is_symlink():
        raise DeliveryError("기록 파일은 심볼릭 링크일 수 없습니다.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    # 파일 교체도 디스크에 반영한 뒤 웹훅을 호출한다.
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def timestamp(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError
    return result


class DeliveryStore:
    def __init__(self, root):
        self.path = runtime_directory(root) / "runtime.json"

    def initialize(self, now):
        if self.path.exists() or self.path.is_symlink():
            raise DeliveryError("발송 기록이 이미 있습니다. 초기화하거나 덮어쓰지 마세요.")
        atomic_json(self.path, {"version": 1, "enabled_at": now.isoformat(),
                                "events": {}, "publication_pending": False})

    def load(self):
        if self.path.is_symlink():
            raise DeliveryError("발송 기록은 실제 파일이어야 합니다.")
        if not self.path.exists():
            raise DeliveryError("발송 기록이 없습니다. 최초 등록 때만 init을 실행하세요. 유실됐다면 복구하세요.")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if (data["version"] != 1 or not isinstance(data["events"], dict)
                    or type(data["publication_pending"]) is not bool):
                raise ValueError
            timestamp(data["enabled_at"])
            for key, record in data["events"].items():
                deadline, kind, scheduled = key.split("/", 2)
                datetime.strptime(deadline, "%Y-%m-%d")
                timestamp(scheduled)
                if kind not in ("reminder", "final") or record["status"] not in (
                        "sending", "sent", "failed", "unknown", "skipped"):
                    raise ValueError
                timestamp(record["updated_at"])
                if record["status"] == "sent" and not re.fullmatch(
                        r"[0-9]{1,20}", record["message_id"]):
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
            raise DeliveryError("runtime.json이 손상되었습니다. 기존 발송 기록을 복구하세요.") from None
        return data

    def active(self, event, data):
        return timestamp(event["scheduled_at"]) >= timestamp(data["enabled_at"])

    def record(self, data, event, status, now, message_id=None):
        record = {"status": status, "updated_at": now.isoformat()}
        if message_id is not None:
            record["message_id"] = message_id
        data["events"][event_key(event)] = record
        atomic_json(self.path, data)

    def publication(self, data, pending):
        data["publication_pending"] = pending
        atomic_json(self.path, data)

    def send(self, data, event, now, deliver):
        key = event_key(event)
        previous = data["events"].get(key, {}).get("status")
        if previous in ("sent", "skipped"):
            print(f"발송 생략 ({previous}): {key}")
            return
        if previous in ("sending", "unknown"):
            raise DeliveryError(f"도착 여부 확인 필요: {key}. 채널 확인 전에는 재전송하지 않습니다.")
        self.record(data, event, "sending", now)
        try:
            message_id = deliver()
        except WebhookError as error:
            self.record(data, event, "unknown" if error.delivery_uncertain else "failed", now)
            raise DeliveryError(f"발송 확인 필요: {key}. {error}") from None
        self.record(data, event, "sent", now, message_id)
        print(f"Discord 발송 완료: {key} (메시지 {message_id})")

    def problems(self, data, events, now):
        issues = []
        for event in events:
            if not self.active(event, data) or now < timestamp(event["scheduled_at"]) + timedelta(minutes=5):
                continue
            key = event_key(event)
            status = data["events"].get(key, {}).get("status", "missing")
            if status not in ("sent", "skipped"):
                issues.append(f"{status}: {key}")
        if data["publication_pending"]:
            issues.append("마감 기록·폴더의 Git 게시가 완료되지 않았습니다. 전송과 별도로 복구하세요.")
        return issues
