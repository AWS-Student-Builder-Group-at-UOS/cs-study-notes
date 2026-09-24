#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study_config as config
from scripts.discord_webhook import WebhookError, send_message
from scripts.git_snapshot import SnapshotError, snapshot_at, snapshot_revision
from scripts.submission import has_content

KST = ZoneInfo(config.TIMEZONE)
RESERVED = {"scripts", "docs", "study_config.py", "README.md"}


class StudyError(RuntimeError):
    pass


def safe_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= 40
            and re.fullmatch(r"[\w가-힣][\w가-힣 -]*", name) is not None
            and name.strip() == name
            and name.casefold() not in {value.casefold() for value in RESERVED}
            and unicodedata.normalize("NFC", name) == name)


def validate_config():
    if not isinstance(config.MEMBERS, (list, tuple)):
        raise StudyError("MEMBERS는 이름을 담은 목록이어야 합니다.")
    members = list(config.MEMBERS)
    if not all(safe_name(member) for member in members):
        raise StudyError("명단에는 경로 문자나 예약 이름을 사용할 수 없습니다.")
    if not members or len({member.casefold() for member in members}) != len(members):
        raise StudyError("MEMBERS에는 중복 없이 한 명 이상을 적어 주세요.")
    if not isinstance(config.MEMBER_EMOJIS, dict):
        raise StudyError("MEMBER_EMOJIS에는 이름과 이모지를 짝지어 적어 주세요.")
    emojis = [config.DEFAULT_MEMBER_EMOJI, *config.MEMBER_EMOJIS.values()]
    if any(not isinstance(emoji, str) or not 1 <= len(emoji) <= 16
           or any(char.isspace() for char in emoji) for emoji in emojis):
        raise StudyError("참여자 이모지는 공백 없이 1~16자로 적어 주세요.")
    if (not isinstance(config.BOT_NAME, str) or not config.BOT_NAME.strip()
            or len(config.BOT_NAME) > 80 or any(char in config.BOT_NAME for char in "\r\n")):
        raise StudyError("BOT_NAME에는 1~80자의 봇 이름을 적어 주세요.")
    try:
        deadlines = [date.fromisoformat(value) for value in config.DEADLINES]
    except (TypeError, ValueError):
        raise StudyError("DEADLINES에는 YYYY-MM-DD 날짜를 적어 주세요.") from None
    if not deadlines or deadlines != sorted(set(deadlines)):
        raise StudyError("마감일은 중복 없이 오름차순이어야 합니다.")
    if any(value.weekday() != 6 for value in deadlines):
        raise StudyError("모든 마감일은 일요일이어야 합니다.")
    if any((b - a).days != 14 for a, b in zip(deadlines, deadlines[1:])):
        raise StudyError("마감일은 2주 간격이어야 합니다.")
    if tuple(config.REMINDER_DAYS) != (3, 2, 1, 0):
        raise StudyError("REMINDER_DAYS는 (3, 2, 1, 0)이어야 합니다.")
    if len(members) > 30:
        raise StudyError("Discord 체크리스트는 최대 30명까지 지원합니다.")
    return members, deadlines


def calendar_events():
    _, deadlines = validate_config()
    events = []
    for index, due in enumerate(deadlines, 1):
        for days in config.REMINDER_DAYS:
            scheduled = datetime.combine(due - timedelta(days=days), time(), KST)
            events.append({"id": f"{due}:d-{days}", "round": index,
                           "deadline": due.isoformat(), "kind": "reminder", "days_left": days,
                           "scheduled_at": scheduled.isoformat()})
        if config.SEND_FINAL_RESULTS:
            scheduled = datetime.combine(due + timedelta(days=1), time(), KST)
            events.append({"id": f"{due}:final", "round": index,
                           "deadline": due.isoformat(), "kind": "final",
                           "scheduled_at": scheduled.isoformat()})
    return events


def at(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise StudyError("시각에는 +09:00 같은 시간대를 포함해 주세요.")
    return result.astimezone(KST)


def due_events(now, events=None):
    return [event for event in (events if events is not None else calendar_events())
            if at(event["scheduled_at"]) <= now
            and at(event["scheduled_at"]).date() == now.astimezone(KST).date()]


def atomic_json(path, data):
    if path.is_symlink():
        raise StudyError("상태 파일은 심볼릭 링크일 수 없습니다.")
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


def validate_state(state):
    try:
        if (not isinstance(state, dict) or type(state["version"]) is not int
                or state["version"] != 1 or not isinstance(state["members"], dict)
                or not isinstance(state["events"], dict)):
            raise ValueError
        folders = set()
        names = set()
        for name, entry in state["members"].items():
            if not safe_name(name) or not isinstance(entry, dict) or name.casefold() in names:
                raise ValueError
            names.add(name.casefold())
            folder = entry["folder"]
            if (not isinstance(folder, str) or folder.casefold() in folders
                    or not isinstance(entry["active"], bool)):
                raise ValueError
            if entry["active"]:
                if folder != name:
                    raise ValueError
            elif not re.fullmatch(re.escape(f"[종료] {name}") + r"(?: \([2-9][0-9]*\)| \(1[0-9]+\))?", folder):
                raise ValueError
            folders.add(folder.casefold())
        for event_id, event in state["events"].items():
            if (not isinstance(event_id, str)
                    or not re.fullmatch(r"\d{4}-\d{2}-\d{2}:(?:d-[0-3]|final)", event_id)
                    or not isinstance(event, dict) or event.get("status") not in ("pending", "sent")):
                raise ValueError
            date.fromisoformat(event_id.split(":", 1)[0])
        manual = state.get("manual", {})
        if not isinstance(manual, dict):
            raise ValueError
        for run_id, record in manual.items():
            if (not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id)
                    or not isinstance(record, dict) or record.get("status") not in ("pending", "sent")):
                raise ValueError
    except (ValueError, KeyError, TypeError):
        raise StudyError(".study/state.json이 손상되었습니다. 이전 기록을 복구해 주세요.") from None


def load_state(root):
    path = root / ".study/state.json"
    if (root / ".study").is_symlink() or path.is_symlink():
        raise StudyError(".study는 실제 폴더와 파일이어야 합니다.")
    if not path.exists():
        return {"version": 1, "members": {}, "events": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise StudyError(".study/state.json이 손상되었습니다. 이전 기록을 복구해 주세요.") from None
    validate_state(state)
    return state


def ensure_dir(path):
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise StudyError(f"폴더 경로를 확인해 주세요: {path.name}")
    path.mkdir(exist_ok=True)


def sync_folders(root, state, now):
    members, deadlines = validate_config()
    validate_state(state)
    today = now.astimezone(KST).date()
    changes = []
    planned_state = deepcopy(state)
    registry = planned_state["members"]
    renames, directories, placeholders = [], [], []

    def inspect_directory(path):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise StudyError(f"폴더 경로를 확인해 주세요: {path.name}")
        return path.exists()

    def is_round_directory(path):
        if path.is_symlink() or not path.is_dir() or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.name):
            return False
        try:
            date.fromisoformat(path.name)
        except ValueError:
            return False
        return True

    for path in root.iterdir():
        if (not re.fullmatch(r"[가-힣]{2,10}", path.name) or not safe_name(path.name)
                or path.is_symlink() or not path.is_dir()):
            continue
        if path.name not in registry and any(is_round_directory(child) for child in path.iterdir()):
            registry[path.name] = {"folder": path.name, "active": True}
    previous_names = {name.casefold(): name for name in registry}
    if any(member.casefold() in previous_names and previous_names[member.casefold()] != member
           for member in members):
        raise StudyError("이름의 대소문자만 변경할 때에는 기존 폴더와 상태 기록을 함께 수정해 주세요.")
    destinations = {entry["folder"].casefold() for entry in registry.values()}
    for name, entry in registry.items():
        if name not in members and entry["active"]:
            source = root / entry["folder"]
            source_exists = inspect_directory(source)
            archived = f"[종료] {name}"
            recovered = [path for path in root.iterdir()
                         if re.fullmatch(re.escape(archived) + r"(?: \([2-9][0-9]*\)| \(1[0-9]+\))?", path.name)]
            if not source_exists and recovered:
                if len(recovered) != 1:
                    raise StudyError(f"종료 폴더 기록을 수동으로 확인해 주세요: {name}")
                inspect_directory(recovered[0])
                archived = recovered[0].name
                changes.append(f"종료 기록 복구: {name} → {archived}")
            else:
                sequence = 2
                while ((root / archived).exists() or (root / archived).is_symlink()
                       or archived.casefold() in destinations):
                    archived = f"[종료] {name} ({sequence})"
                    sequence += 1
            if source_exists:
                renames.append((source, root / archived))
                changes.append(f"종료: {name} → {archived}")
            destinations.add(archived.casefold())
            entry.update(folder=archived, active=False)
    dates_to_create = []
    for index, due in enumerate(deadlines):
        if due >= today:
            dates_to_create.append(due)
            if due == today and index + 1 < len(deadlines):
                dates_to_create.append(deadlines[index + 1])
            break
    for member in members:
        folder = root / member
        source = folder
        entry = registry.get(member)
        if entry and not entry["active"]:
            archived = root / entry["folder"]
            if inspect_directory(archived):
                if folder.exists() or folder.is_symlink():
                    raise StudyError(f"복귀 폴더가 충돌합니다. 수동으로 합쳐 주세요: {member}")
                source = archived
                renames.append((archived, folder))
                changes.append(f"복귀: {member}")
        exists = inspect_directory(source)
        if not exists:
            changes.append(f"참가자 생성: {member}")
            directories.append(folder)
        registry[member] = {"folder": member, "active": True}
        for due in dates_to_create:
            target = folder / due.isoformat()
            physical = source / due.isoformat()
            target_exists = inspect_directory(physical)
            if not target_exists:
                changes.append(f"회차 생성: {member}/{due}")
                directories.append(target)
            if not target_exists or not any(physical.iterdir()):
                placeholders.append(target / ".gitkeep")
        if not dates_to_create and (not exists or not any(source.iterdir())):
            placeholders.append(folder / ".gitkeep")
    validate_state(planned_state)
    completed_renames, created_directories, created_files = [], [], []
    try:
        for source, destination in renames:
            if not inspect_directory(source) or destination.exists() or destination.is_symlink():
                raise StudyError("동기화 중 폴더 상태가 변경되었습니다. 다시 확인해 주세요.")
            source.rename(destination)
            completed_renames.append((source, destination))
        for path in directories:
            path.mkdir()
            created_directories.append(path)
        for path in placeholders:
            with path.open("x"):
                pass
            created_files.append(path)
    except (OSError, StudyError):
        rollback_failed = False
        for path in reversed(created_files):
            try:
                if path.is_symlink() or path.stat().st_size:
                    raise OSError
                path.unlink()
            except OSError:
                rollback_failed = True
        for path in reversed(created_directories):
            try:
                path.rmdir()
            except OSError:
                rollback_failed = True
        for source, destination in reversed(completed_renames):
            try:
                if source.exists() or source.is_symlink():
                    raise OSError
                destination.rename(source)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise StudyError("폴더 동기화와 일부 복구가 실패했습니다. 폴더와 상태 기록을 수동으로 확인해 주세요.") from None
        raise
    state.clear()
    state.update(planned_state)
    return changes


def submission_status(root, member, deadline, folder=None):
    target = root / (folder or member) / deadline
    if target.parent.is_symlink() or target.is_symlink() or not target.is_dir():
        return False
    for base, dirs, files in os.walk(target, followlinks=False):
        dirs[:] = sorted(name for name in dirs
                         if not name.startswith(".") and not (Path(base) / name).is_symlink())
        for name in sorted(files):
            if not name.startswith(".") and has_content(Path(base) / name):
                return True
    return False


def event_result(root, event, members, state):
    if event["kind"] == "final":
        cutoff = at(event["scheduled_at"])
        revision = snapshot_revision(root, cutoff)
        with snapshot_at(root, cutoff) as snapshot:
            historic = load_state(snapshot)
            result = []
            for member in members:
                folder = historic["members"].get(member, {}).get("folder", member)
                result.append({"name": member, "submitted": submission_status(
                    snapshot, member, event["deadline"], folder)})
        return result, revision
    return [{"name": member, "submitted": submission_status(root, member, event["deadline"])}
            for member in members], None


def deadline_label(due, now):
    year = f"{due.year}년 " if due.year != now.astimezone(KST).year else ""
    return f"{year}{due.month}월 {due.day}일(일) 밤 11시 59분"


def status_lines(result, now, closed=False):
    lines = [f"- {config.MEMBER_EMOJIS.get(item['name'], config.DEFAULT_MEMBER_EMOJI)} "
             f"**{item['name']}** · {'제출 완료!' if item['submitted'] else '제출 미완료 ㅠㅠ'}"
             for item in result]
    count = sum(item["submitted"] for item in result)
    total = len(result)
    if count == total:
        summary = f"**{total}명 모두 제출 완료!** 수고하셨어요! 🎉"
    elif closed:
        summary = f"이번 회차는 **{count}/{total}명**이 제출했어요. 모두 수고하셨어요! 🙌"
    elif count == 0:
        summary = f"현재 **0/{total}명** 제출! 첫 회고를 기다릴게요. ✍️"
    else:
        summary = f"지금까지 **{count}/{total}명**이 제출했어요. 남은 회고도 기다릴게요! ✍️"
    stamp = now.astimezone(KST).strftime("%Y년 %m월 %d일 %H시 %M분")
    return lines + ["", summary, "", f"{stamp}(한국 시간) 기준으로 확인한 결과예요!"]


def render_message(event, result, now, revision=None):
    due = date.fromisoformat(event["deadline"])
    deadline = deadline_label(due, now)
    closed = event["kind"] == "final"
    lines = [f"안녕하세요! 여러분의 회고를 챙기는 {config.BOT_NAME}이에요! 🐾", ""]
    if closed:
        lines += [f"**{event['round']}회차 회고가 마감됐어요!**",
                  f"{deadline}까지의 제출 결과를 정리했어요."]
    else:
        remaining = (due - now.astimezone(KST).date()).days
        if remaining == 0:
            lines += [f"**오늘은 {event['round']}회차 회고 마감일이에요! ⏰**"]
        elif remaining > 0:
            lines += [f"**{event['round']}회차 회고 마감까지 {remaining}일 남았어요!**"]
        else:
            lines += [f"**{event['round']}회차 회고 알림을 다시 전해드려요!**",
                      f"제출 기한은 {deadline}이었어요."]
        if remaining >= 0:
            lines += [f"**{deadline}**까지 회고를 올려 주세요."]
    return "\n".join(lines + [""] + status_lines(result, now, closed))


def render_status(root, now, state):
    members, deadlines = validate_config()
    today = now.astimezone(KST).date()
    current = next((index for index, due in enumerate(deadlines) if due >= today), None)
    ended = current is None
    index = len(deadlines) - 1 if ended else current
    due = deadlines[index]
    event = {"deadline": due.isoformat(), "kind": "final" if ended else "reminder",
             "scheduled_at": datetime.combine(due + timedelta(days=1), time(), KST).isoformat()}
    result, revision = event_result(root, event, members, state)
    lines = [f"안녕하세요! 여러분의 회고를 챙기는 {config.BOT_NAME}이에요! 🐾", ""]
    deadline = deadline_label(due, now)
    if ended:
        lines += [f"모든 회차가 끝났어요! **마지막 {index + 1}회차 결과**를 전해드려요.",
                  f"{deadline}까지의 제출 결과예요."]
    else:
        lines += [f"**{index + 1}회차 회고는 {deadline}까지예요.**"]
        lines += ["오늘 마감이에요! 잊지 말고 회고를 올려 주세요. ⏰" if due == today
                  else f"마감까지 {(due - today).days}일 남았어요. 이번에도 함께 기록해 봐요!"]
    return "\n".join(lines + [""] + status_lines(result, now, ended)), result, revision


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    if result.returncode:
        raise StudyError("Git 작업이 실패했습니다. 원격 권한·브랜치·충돌 상태를 확인해 주세요.")
    return result.stdout.strip()


def persist(root, label):
    git(root, "add", "--all")
    if git(root, "diff", "--cached", "--name-only"):
        git(root, "commit", "-m", f"chore(study): {label}")
    git(root, "push", "origin", "HEAD")


def check_checkout(root):
    if git(root, "status", "--porcelain"):
        raise StudyError("자동 커밋은 변경 사항이 없는 전용 체크아웃에서 실행해 주세요.")
    if git(root, "branch", "--show-current") != "main":
        raise StudyError("자동 커밋은 main 브랜치에서만 실행합니다.")
    git(root, "fetch", "origin", "main")
    if git(root, "rev-parse", "HEAD") != git(root, "rev-parse", "FETCH_HEAD"):
        raise StudyError("원격 main이 변경되었습니다. 최신 코드로 갱신한 뒤 다시 실행하세요.")


@contextmanager
def exclusive_lock(root):
    ensure_dir(root / ".study")
    lock_path = root / ".study/run.lock"
    if lock_path.is_symlink():
        raise StudyError("실행 잠금 파일이 올바르지 않습니다.")
    with lock_path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise StudyError("다른 스터디 스크립트가 실행 중입니다.") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def already_delivered(state, group, record_id):
    existing = state.get(group, {}).get(record_id)
    if not existing:
        return False
    if existing["status"] == "sent":
        print(f"이미 전송됨: {record_id}")
        return True
    raise StudyError(f"전송 확인이 필요한 기록이 있습니다: {record_id}. 운영 문서를 확인하세요.")


def deliver(root, state, group, record_id, content, result, revision, now, persist_changes):
    record = {"status": "pending", "reserved_at": now.isoformat(),
              "members": list(config.MEMBERS), "result": result,
              "revision": revision, "content": content}
    state.setdefault(group, {})[record_id] = record
    state_path = root / ".study/state.json"
    atomic_json(state_path, state)
    if persist_changes:
        # 중복 발송을 막기 위해 전송 전에 예약 상태를 저장합니다.
        persist(root, f"reserve {group} {record_id}")
    try:
        message_id = send_message(os.environ["DISCORD_WEBHOOK_URL"], content,
                                  username=config.BOT_NAME)
    except WebhookError as error:
        record["error"] = str(error)
        atomic_json(state_path, state)
        if persist_changes:
            persist(root, f"delivery needs review {group} {record_id}")
        raise StudyError(f"알림 전송을 확인해 주세요: {record_id}. {error}") from None
    record.update(status="sent", message_id=message_id, sent_at=datetime.now(KST).isoformat())
    atomic_json(state_path, state)
    if persist_changes:
        persist(root, f"sent {group} {record_id}")


def status(root, now, *, send=False, persist_changes=False, run_id=None):
    if persist_changes and not send:
        raise StudyError("--persist는 --send와 함께 사용하세요.")
    if send and not os.environ.get("DISCORD_WEBHOOK_URL"):
        raise StudyError("DISCORD_WEBHOOK_URL 환경 변수가 필요합니다.")
    record_id = str(run_id or os.environ.get("GITHUB_RUN_ID") or uuid4())
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", record_id):
        raise StudyError("수동 실행 ID가 올바르지 않습니다.")
    if persist_changes:
        check_checkout(root)
    state = load_state(root)
    if send:
        for change in sync_folders(root, state, now):
            print(change)
        atomic_json(root / ".study/state.json", state)
        if persist_changes:
            persist(root, "sync participant folders")
        if already_delivered(state, "manual", record_id):
            return
    content, result, revision = render_status(root, now, state)
    print(content)
    if send:
        deliver(root, state, "manual", record_id, content, result, revision, now, persist_changes)


def run(root, now, *, send=False, persist_changes=False, event_id=None):
    if persist_changes and not send:
        raise StudyError("--persist는 --send와 함께 사용하세요.")
    if send and not os.environ.get("DISCORD_WEBHOOK_URL"):
        raise StudyError("DISCORD_WEBHOOK_URL 환경 변수가 필요합니다.")
    if persist_changes:
        check_checkout(root)
    state = load_state(root)
    members, _ = validate_config()
    changes = sync_folders(root, state, now)
    events = calendar_events()
    if event_id:
        selected = [event for event in events if event["id"] == event_id]
        if not selected:
            raise StudyError("등록되지 않은 이벤트입니다. schedule 명령으로 ID를 확인하세요.")
        if at(selected[0]["scheduled_at"]) > now:
            raise StudyError("미래 알림은 실제 전송할 수 없습니다.")
    else:
        selected = due_events(now, events)
    state_path = root / ".study/state.json"
    atomic_json(state_path, state)
    if persist_changes:
        persist(root, "sync participant folders")
    for change in changes:
        print(change)
    for event in selected:
        if already_delivered(state, "events", event["id"]):
            continue
        result, revision = event_result(root, event, members, state)
        content = render_message(event, result, now, revision)
        print(content)
        if not send:
            continue
        deliver(root, state, "events", event["id"], content, result, revision, now, persist_changes)
    if not selected:
        print("오늘 예약된 알림이 없습니다. 폴더 동기화를 완료했습니다.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="CS 회고 스터디 자동화 (기본: 전송하지 않음)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schedule", help="한국 시간 기준 전체 알림 일정 JSON 출력")
    for name in ("sync", "preview", "run", "status"):
        command = sub.add_parser(name)
        if name == "sync":
            command.add_argument("--persist", action="store_true", help="자동화용 Git 커밋/푸시")
        if name in ("run", "status"):
            command.add_argument("--send", action="store_true")
            command.add_argument("--persist", action="store_true", help="자동화용 Git 커밋/푸시")
        if name == "run":
            command.add_argument("--event", help="누락된 과거 알림 ID 수동 복구")
    args = parser.parse_args(argv)
    try:
        validate_config()
        if args.command == "schedule":
            print(json.dumps(calendar_events(), ensure_ascii=False, indent=2))
            return 0
        now = datetime.now(KST)
        if args.command == "status" and not args.send:
            status(ROOT, now, persist_changes=args.persist)
            return 0
        if args.command == "preview":
            state = load_state(ROOT)
            for event in due_events(now):
                result, revision = event_result(ROOT, event, list(config.MEMBERS), state)
                print(render_message(event, result, now, revision))
            return 0
        with exclusive_lock(ROOT):
            if args.command == "sync":
                state = load_state(ROOT)
                if args.persist:
                    check_checkout(ROOT)
                for change in sync_folders(ROOT, state, now):
                    print(change)
                atomic_json(ROOT / ".study/state.json", state)
                if args.persist:
                    persist(ROOT, "sync participant folders")
            elif args.command == "run":
                run(ROOT, now, send=args.send, persist_changes=args.persist, event_id=args.event)
            elif args.command == "status":
                status(ROOT, now, send=args.send, persist_changes=args.persist)
        return 0
    except (StudyError, SnapshotError, ValueError, OSError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
