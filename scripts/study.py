#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
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
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import study_config as config
from scripts.discord_webhook import send_message
from scripts.delivery import (DeliveryError, DeliveryStore, atomic_json, event_key,
                              runtime_directory)
from scripts.git_snapshot import SnapshotError, snapshot_at
from scripts.submission import has_content

KST = ZoneInfo(config.TIMEZONE)
RESERVED = {"scripts", "README.md", "AUTOMATION.md"}


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
    if type(config.MISSED_SUBMISSION_FINE) is not int or config.MISSED_SUBMISSION_FINE <= 0:
        raise StudyError("MISSED_SUBMISSION_FINE에는 양의 정수 금액을 적어 주세요.")
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
    extensions = config.DEADLINE_EXTENSIONS
    if not isinstance(extensions, dict) or set(extensions) - set(config.DEADLINES):
        raise StudyError("DEADLINE_EXTENSIONS에는 기존 회차의 마감일을 키로 지정하세요.")
    for original, extended in extensions.items():
        try:
            revised = date.fromisoformat(extended)
        except (TypeError, ValueError):
            raise StudyError("연장 마감일은 YYYY-MM-DD 날짜여야 합니다.") from None
        index = list(config.DEADLINES).index(original)
        if (revised.isoformat() != extended or revised <= deadlines[index]
                or (index + 1 < len(deadlines) and revised >= deadlines[index + 1])):
            raise StudyError("연장 마감일은 원래 마감 이후이며 다음 회차 전이어야 합니다.")
    if tuple(config.REMINDER_DAYS) != (7, 3, 1, 0):
        raise StudyError("REMINDER_DAYS는 (7, 3, 1, 0)이어야 합니다.")
    if type(config.REMINDER_HOUR) is not int or not 0 <= config.REMINDER_HOUR <= 23:
        raise StudyError("REMINDER_HOUR에는 0~23 사이의 정수를 적어 주세요.")
    if len(members) > 30:
        raise StudyError("Discord 체크리스트는 최대 30명까지 지원합니다.")
    return members, deadlines


def effective_deadline(due):
    return date.fromisoformat(config.DEADLINE_EXTENSIONS.get(due.isoformat(), due.isoformat()))


def calendar_events():
    _, deadlines = validate_config()
    events = []
    for index, due in enumerate(deadlines, 1):
        for days in config.REMINDER_DAYS:
            scheduled = datetime.combine(due - timedelta(days=days), time(config.REMINDER_HOUR), KST)
            events.append({"round": index,
                           "deadline": due.isoformat(), "kind": "reminder",
                           "scheduled_at": scheduled.isoformat()})
        if config.SEND_FINAL_RESULTS:
            scheduled = datetime.combine(effective_deadline(due) + timedelta(days=1), time(), KST)
            events.append({"round": index,
                           "deadline": due.isoformat(), "kind": "final",
                           "scheduled_at": scheduled.isoformat()})
    return events


def at(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise StudyError("시각에는 +09:00 같은 시간대를 포함해 주세요.")
    return result.astimezone(KST)


def due_events(now, events=None, *, kind=None):
    return [event for event in (events if events is not None else calendar_events())
            if (kind is None or event["kind"] == kind)
            and timedelta() <= now - at(event["scheduled_at"]) < timedelta(minutes=10)]


def validate_state(state):
    try:
        if (not isinstance(state, dict) or type(state["version"]) is not int
                or state["version"] != 2 or not isinstance(state["members"], dict)
                or not isinstance(state["rounds"], dict)):
            raise ValueError
        folders = set()
        names = set()
        for name, folder in state["members"].items():
            if not safe_name(name) or name.casefold() in names:
                raise ValueError
            names.add(name.casefold())
            if not isinstance(folder, str) or folder.casefold() in folders:
                raise ValueError
            if folder != name and not re.fullmatch(re.escape(f"[종료] {name}") + r"(?: \([2-9][0-9]*\)| \(1[0-9]+\))?", folder):
                raise ValueError
            folders.add(folder.casefold())
        for deadline, record in state["rounds"].items():
            date.fromisoformat(deadline)
            if (not isinstance(record, dict) or type(record["fine"]) is not int or record["fine"] <= 0
                    or not isinstance(record["results"], dict) or not record["results"]
                    or any(not safe_name(name) or type(submitted) is not bool
                           for name, submitted in record["results"].items())):
                raise ValueError
    except (ValueError, KeyError, TypeError):
        raise StudyError(".study/state.json이 손상되었습니다. 이전 기록을 복구해 주세요.") from None


def load_state(root):
    path = root / ".study/state.json"
    if (root / ".study").is_symlink() or path.is_symlink():
        raise StudyError(".study는 실제 폴더와 파일이어야 합니다.")
    if not path.exists():
        return {"version": 2, "members": {}, "rounds": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and state.get("version") == 1:
            state = {"version": 2,
                     "members": {name: entry["folder"] for name, entry in state["members"].items()},
                     "rounds": {}}
        state = {key: state[key] for key in ("version", "members", "rounds")}
    except (ValueError, UnicodeError, KeyError, TypeError, AttributeError):
        raise StudyError(".study/state.json이 손상되었습니다. 이전 기록을 복구해 주세요.") from None
    validate_state(state)
    return state


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
            registry[path.name] = path.name
    previous_names = {name.casefold(): name for name in registry}
    if any(member.casefold() in previous_names and previous_names[member.casefold()] != member
           for member in members):
        raise StudyError("이름의 대소문자만 변경할 때에는 기존 폴더와 상태 기록을 함께 수정해 주세요.")
    destinations = {folder.casefold() for folder in registry.values()}
    for name, folder in registry.items():
        if name not in members and folder == name:
            source = root / folder
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
            registry[name] = archived
    dates_to_create = []
    for due in deadlines:
        if effective_deadline(due) >= today:
            dates_to_create.append(due)
            break
    for member in members:
        folder = root / member
        source = folder
        previous_folder = registry.get(member)
        if previous_folder and previous_folder != member:
            archived = root / previous_folder
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
        registry[member] = member
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


def close_rounds(root, state, now):
    _, deadlines = validate_config()
    closed = {}
    for due in deadlines:
        actual_due = effective_deadline(due)
        if actual_due >= now.astimezone(KST).date() or due.isoformat() in state["rounds"]:
            continue
        cutoff = datetime.combine(actual_due + timedelta(days=1), time(), KST)
        with snapshot_at(root, cutoff) as snapshot:
            try:
                config_path = snapshot / "scripts/study_config.py"
                if not config_path.exists():
                    config_path = snapshot / "study_config.py"
                tree = ast.parse(config_path.read_text(encoding="utf-8"))
                members = next(ast.literal_eval(node.value) for node in tree.body
                               if isinstance(node, ast.Assign)
                               and any(isinstance(target, ast.Name) and target.id == "MEMBERS"
                                       for target in node.targets))
                fine = next((ast.literal_eval(node.value) for node in tree.body
                             if isinstance(node, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == "MISSED_SUBMISSION_FINE"
                                     for target in node.targets)), 10_000)
                if (not isinstance(members, (list, tuple)) or not members
                        or not all(safe_name(name) for name in members)
                        or len({name.casefold() for name in members}) != len(members)
                        or type(fine) is not int or fine <= 0):
                    raise ValueError
            except (OSError, SyntaxError, ValueError, TypeError, StopIteration):
                raise StudyError(f"{due} 마감 당시 명단을 확인할 수 없습니다.") from None
            historic = load_state(snapshot)
            results = {name: submission_status(snapshot, name, due.isoformat(),
                       historic["members"].get(name, name)) for name in members}
        closed[due.isoformat()] = {"results": results, "fine": fine}
    state["rounds"].update(closed)
    return [f"마감 확정: {due}" for due in closed]


def event_result(root, event, members, state):
    record = state["rounds"].get(event["deadline"])
    if record:
        return [{"name": name, "submitted": submitted}
                for name, submitted in record["results"].items()]
    if event["kind"] == "final":
        raise StudyError("확정된 마감 결과가 없습니다.")
    return [{"name": member, "submitted": submission_status(root, member, event["deadline"])}
            for member in members]


def deadline_label(due, now):
    due = effective_deadline(due)
    year = f"{due.year}년 " if due.year != now.astimezone(KST).year else ""
    weekday = "월화수목금토일"[due.weekday()]
    return f"{year}{due.month}월 {due.day}일({weekday}) 밤 11시 59분"


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
    return lines + ["", summary]


def fine_lines(state):
    fines = {name: {"count": 0, "amount": 0} for name in config.MEMBERS}
    for record in state["rounds"].values():
        for name, submitted in record["results"].items():
            if name in fines and not submitted:
                fines[name]["amount"] += record["fine"]
                fines[name]["count"] += 1
    lines = [f"- {config.MEMBER_EMOJIS.get(name, config.DEFAULT_MEMBER_EMOJI)} "
             f"**{name}** · 누적 **{fine['amount']:,}원** ({fine['count']}회)"
             for name, fine in fines.items() if fine["count"]]
    return ["💰 현재 참여자의 누적 벌금이에요!"] + lines if lines else []


def next_round_lines(due, now):
    _, deadlines = validate_config()
    next_due = next((value for value in deadlines if value > due), None)
    if next_due is None:
        return ["이번이 마지막 회차였어요. 끝까지 함께해 주셔서 고마워요! 🐾"]
    number = deadlines.index(next_due) + 1
    label = deadline_label(next_due, now)
    return [f"다음은 **{number}회차**예요! **{label}**까지 함께 기록해 봐요. 🐾"]


def message_tail(result, now, state, closed=False, due=None):
    lines = status_lines(result, now, closed)
    if due is not None:
        lines += [""] + next_round_lines(due, now)
    fines = fine_lines(state)
    if fines:
        lines += [""] + fines
    stamp = now.astimezone(KST).strftime("%Y년 %m월 %d일 %H시 %M분")
    return lines + ["", f"{stamp} 기준으로 확인한 결과예요!"]


def render_message(event, result, now, state):
    due = date.fromisoformat(event["deadline"])
    deadline = deadline_label(due, now)
    closed = event["kind"] == "final"
    lines = [f"안녕하세요! 여러분의 회고를 챙기는 {config.BOT_NAME}이에요! 🐾", ""]
    if closed:
        lines += [f"**{event['round']}회차 회고가 마감됐어요!**",
                  f"{deadline}까지의 제출 결과를 정리했어요."]
    else:
        remaining = (effective_deadline(due) - now.astimezone(KST).date()).days
        if remaining == 0:
            lines += [f"**오늘은 {event['round']}회차 회고 마감일이에요! ⏰**"]
        else:
            lines += [f"**{event['round']}회차 회고 마감까지 {remaining}일 남았어요!**"]
        lines += [f"**{deadline}**까지 회고를 올려 주세요."]
    return "\n".join(lines + [""] + message_tail(
        result, now, state, closed, due if closed else None))


def render_status(root, now, state):
    members, deadlines = validate_config()
    today = now.astimezone(KST).date()
    current = next((index for index, due in enumerate(deadlines)
                    if effective_deadline(due) >= today), None)
    ended = current is None
    index = len(deadlines) - 1 if ended else current
    due = deadlines[index]
    actual_due = effective_deadline(due)
    event = {"deadline": due.isoformat(), "kind": "final" if ended else "reminder",
             "scheduled_at": datetime.combine(actual_due + timedelta(days=1), time(), KST).isoformat()}
    result = event_result(root, event, members, state)
    lines = [f"안녕하세요! 여러분의 회고를 챙기는 {config.BOT_NAME}이에요! 🐾", ""]
    deadline = deadline_label(due, now)
    if ended:
        lines += [f"모든 회차가 끝났어요! **마지막 {index + 1}회차 결과**를 전해드려요.",
                  f"{deadline}까지의 제출 결과예요."]
    else:
        lines += [f"**{index + 1}회차 회고는 {deadline}까지예요.**"]
        lines += ["오늘 마감이에요! 잊지 말고 회고를 올려 주세요. ⏰" if actual_due == today
                  else f"마감까지 {(actual_due - today).days}일 남았어요. 이번에도 함께 기록해 봐요!"]
    return "\n".join(lines + [""] + message_tail(result, now, state, ended))


def git(root, *args):
    try:
        result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise StudyError("Git 작업이 시간 내에 끝나지 않았습니다. 네트워크·원격 상태를 확인하세요.") from None
    if result.returncode:
        raise StudyError("Git 작업이 실패했습니다. 원격 권한·브랜치·충돌 상태를 확인해 주세요.")
    return result.stdout.strip()


def check_checkout(root):
    if git(root, "branch", "--show-current") != "main":
        raise StudyError("자동 게시에는 main 전용 체크아웃이 필요합니다.")
    changed = set()
    for args in (("diff", "--name-only", "-z"),
                 ("diff", "--cached", "--name-only", "-z"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        changed.update(filter(None, git(root, *args).split("\0")))
    for name in changed:
        if name == ".study/state.json":
            continue
        path = root / name
        if (path.name == ".gitkeep" and len(path.relative_to(root).parts) == 3
                and path.relative_to(root).parts[0] in config.MEMBERS
                and path.is_file() and not path.is_symlink() and path.stat().st_size == 0):
            continue
        raise StudyError("미커밋 변경이 있어 자동 게시를 중단했습니다. 전용 체크아웃을 확인하세요.")


def persist(root, folders):
    paths = [".study/state.json"] + sorted(set(folders))
    # 생성·이동한 참여자 경로와 운영 기록만 커밋한다.
    paths = [path for path in paths if (root / path).exists()
             or git(root, "ls-files", "--", path)]
    git(root, "add", "--all", "--", *paths)
    if git(root, "diff", "--cached", "--name-only"):
        git(root, "commit", "-m", "chore(study): sync study records")
    try:
        git(root, "push", "origin", "HEAD:main")
    except StudyError:
        git(root, "fetch", "origin", "main")
        subjects = git(root, "log", "origin/main..HEAD", "--format=%s").splitlines()
        if not subjects or any(not subject.startswith("chore(study): ") for subject in subjects):
            raise StudyError("Git 게시가 실패했습니다. 로컬 기록을 보존한 채 원격 상태를 확인하세요.") from None
        try:
            git(root, "rebase", "origin/main")
        except StudyError:
            git(root, "rebase", "--abort")
            raise StudyError("자동 게시 충돌: 기존 기록을 보존했습니다. 알림을 재전송하지 말고 Git 충돌만 해결하세요.") from None
        git(root, "push", "origin", "HEAD:main")


@contextmanager
def exclusive_lock(root):
    directory = runtime_directory(root)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "run.lock"
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


def deliver(content):
    return send_message(os.environ["DISCORD_WEBHOOK_URL"], content, username=config.BOT_NAME)


def status(root, now):
    state = load_state(root)
    close_rounds(root, state, now)
    print(render_status(root, now, state))


def synchronize(root, state, now):
    changes = close_rounds(root, state, now) + sync_folders(root, state, now)
    atomic_json(root / ".study/state.json", state)
    for change in changes:
        print(change)


def run(root, now, *, kind, send=False, persist_changes=False, submission_root=None):
    if persist_changes and (not send or kind != "final"):
        raise StudyError("--persist는 run --kind final --send에서만 사용하세요.")
    store = DeliveryStore(root)
    data = store.load() if send else None
    selected = [event for event in due_events(now, kind=kind)
                if data is None or store.active(event, data)]
    if not selected:
        if data is not None:
            today = [event for event in calendar_events() if event["kind"] == kind
                     and at(event["scheduled_at"]).date() == now.astimezone(KST).date()]
            issues = store.problems(data, today, now)
            if issues:
                raise DeliveryError("; ".join(issues))
        print("현재 발송할 예약이 없습니다.")
        return
    if send and not os.environ.get("DISCORD_WEBHOOK_URL"):
        raise StudyError("DISCORD_WEBHOOK_URL 환경 변수가 필요합니다.")
    state = load_state(root)
    folders = set(state["members"].values()) | set(config.MEMBERS)
    publication_error = None
    if send and kind == "final":
        # Git 사전 점검이 실패해도 확정 결과와 Discord 발송은 별도로 처리한다.
        if persist_changes:
            try:
                check_checkout(root)
            except StudyError as error:
                publication_error = error
        store.publication(data, True)
        synchronize(root, state, now)
        folders.update(state["members"].values())
    else:
        # 오전 현황에서는 폴더·Git·확정 기록을 변경하지 않는다.
        close_rounds(root, state, now)
    delivery_error = None
    try:
        for event in selected:
            result = event_result(submission_root or root, event, list(config.MEMBERS), state)
            content = render_message(event, result, now, state)
            if send:
                store.send(data, event, now, lambda: deliver(content))
            else:
                print(content)
    except DeliveryError as error:
        delivery_error = error
    # Discord 확인을 기록한 뒤 게시한다. 실패해도 다음 실행은 발송 기록을 먼저 본다.
    if send and kind == "final" and persist_changes:
        try:
            if publication_error:
                raise publication_error
            persist(root, folders)
            store.publication(data, False)
        except StudyError as error:
            publication_error = error
    if delivery_error or publication_error:
        raise StudyError("; ".join(str(error) for error in (delivery_error, publication_error) if error))


def sync(root, now, persist_changes):
    if persist_changes:
        check_checkout(root)
    store = DeliveryStore(root)
    data = store.load()
    state = load_state(root)
    folders = set(state["members"].values()) | set(config.MEMBERS)
    store.publication(data, True)
    synchronize(root, state, now)
    folders.update(state["members"].values())
    if persist_changes:
        persist(root, folders)
        store.publication(data, False)


def resolve(root, now, args):
    store = DeliveryStore(root)
    data = store.load()
    event = next((event for event in calendar_events() if event_key(event) == args.event), None)
    if event is None or not store.active(event, data) or at(event["scheduled_at"]) > now:
        raise StudyError("운영 시작 이후의 지난 알림만 확인 처리할 수 있습니다.")
    previous = data["events"].get(args.event, {}).get("status")
    if previous == "sent":
        raise StudyError("이미 도착이 확인된 알림입니다. 발송 기록을 변경하지 않습니다.")
    if args.message_id:
        if not re.fullmatch(r"[0-9]{1,20}", args.message_id):
            raise StudyError("Discord 메시지 ID를 숫자로 지정하세요.")
        store.record(data, event, "sent", now, args.message_id)
    elif args.retry:
        if previous not in ("sending", "unknown", "failed") or event not in due_events(now):
            raise StudyError("재시도 허용은 실패·불확실한 알림의 예정 시각 이후 10분 이내에만 가능합니다.")
        store.record(data, event, "failed", now)
    else:
        store.record(data, event, "skipped", now)
    print("발송 기록을 확인 처리했습니다. 메시지는 보내지 않았습니다.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="CS 회고 스터디 직접 실행")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("schedule", help="한국 시간 기준 전체 일정과 알림 ID")
    sub.add_parser("status", help="현재 제출 현황 조회 (저장·전송 없음)")
    sub.add_parser("init", help="최초 한 번만 발송 기록 생성; 이후 예약부터 관리")
    sub.add_parser("check", help="5분 이상 지난 알림의 도착 확인 및 게시 누락 검사")
    sync_parser = sub.add_parser("sync", help="마감 확정·폴더 동기화 및 게시 재시도")
    sync_parser.add_argument("--persist", action="store_true")
    runner = sub.add_parser("run", help="현재 10분 이내의 예약만 처리")
    runner.add_argument("--kind", choices=("reminder", "final"), required=True)
    runner.add_argument("--send", action="store_true")
    runner.add_argument("--persist", action="store_true")
    resolver = sub.add_parser("resolve", help="Discord 채널을 직접 확인한 뒤 기록 정정")
    resolver.add_argument("--event", required=True)
    outcome = resolver.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--message-id")
    outcome.add_argument("--retry", action="store_true", help="미도착을 확인한 불확실한 발송의 재시도 허용")
    outcome.add_argument("--skip", action="store_true", help="이 알림을 보내지 않기로 확정")
    args = parser.parse_args(argv)
    try:
        validate_config()
        if args.command == "schedule":
            print(json.dumps([dict(event, id=event_key(event)) for event in calendar_events()],
                             ensure_ascii=False, indent=2))
            return 0
        now = datetime.now(KST)
        if args.command == "status":
            status(ROOT, now)
            return 0
        with exclusive_lock(ROOT):
            if args.command == "init":
                DeliveryStore(ROOT).initialize(now)
                print("발송 기록을 생성했습니다. 이 시각 이후 예약부터 관리합니다.")
            elif args.command == "check":
                store = DeliveryStore(ROOT)
                issues = store.problems(store.load(), calendar_events(), now)
                if issues:
                    raise DeliveryError("; ".join(issues))
                print("누락·불확실한 발송·미게시 기록이 없습니다.")
            elif args.command == "sync":
                sync(ROOT, now, args.persist)
            elif args.command == "resolve":
                resolve(ROOT, now, args)
            elif args.command == "run":
                run(ROOT, now, kind=args.kind, send=args.send, persist_changes=args.persist)
        return 0
    except (StudyError, DeliveryError, SnapshotError, ValueError, OSError) as error:
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
