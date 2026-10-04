#!/usr/bin/env python3
"""macOS 사용자 LaunchAgent 설치와 스터디 예약 실행."""

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timedelta
import getpass
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import study
from scripts.delivery import DeliveryError, DeliveryStore, event_key
from scripts.discord_webhook import _WEBHOOK_URL
from scripts.git_snapshot import SnapshotError, snapshot_tree

LABEL = "kr.ac.uos.asbg.cs-study"
AGENTS = Path.home() / "Library/LaunchAgents"
CLOCKS = {"reminder": [(study.config.REMINDER_HOUR, 0)], "final": [(0, 0)],
          "check": [(0, 5), (study.config.REMINDER_HOUR, 5)]}
SECRETS = ROOT / ".study/secrets.json"
ERROR_LOG = ROOT / ".study/last-error.log"


def system_command(*args, required=True):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        raise study.StudyError("시스템 명령을 실행하지 못했습니다. 설치 경로와 로그인 상태를 확인하세요.") from None
    if required and result.returncode:
        raise study.StudyError("launchd 명령이 실패했습니다. 사용자 로그인 세션과 등록 상태를 확인하세요.")
    return result


def target(job):
    return f"gui/{os.getuid()}/{LABEL}.{job}"


def plist_path(job):
    return AGENTS / f"{LABEL}.{job}.plist"


def check_clock():
    if not str(Path("/etc/localtime").resolve()).endswith("/Asia/Seoul"):
        raise study.StudyError("시스템 설정에서 날짜 및 시간의 시간대를 서울(Asia/Seoul)로 설정하세요.")


def read_secrets():
    if (not SECRETS.is_file() or SECRETS.is_symlink()
            or SECRETS.stat().st_mode & 0o077):
        raise study.StudyError(".study/secrets.json이 없거나 권한이 올바르지 않습니다. install을 실행하세요.")
    try:
        values = json.loads(SECRETS.read_text(encoding="utf-8"))
        if (not isinstance(values, dict) or set(values) - {"DISCORD_WEBHOOK_URL", "GITHUB_TOKEN"}
                or not all(isinstance(value, str) for value in values.values())
                or not _WEBHOOK_URL.fullmatch(values.get("DISCORD_WEBHOOK_URL", ""))):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        raise study.StudyError(".study/secrets.json 형식을 확인하세요. 웹훅은 HTTPS Discord URL이어야 합니다.") from None
    return values


def configure_secrets():
    if SECRETS.exists() or SECRETS.is_symlink():
        read_secrets()
        return
    if not sys.stdin.isatty():
        raise study.StudyError("웹훅을 비공개로 입력할 수 있는 Mac 터미널에서 install을 실행하세요.")
    url = getpass.getpass("Discord 웹훅 URL (입력 숨김): ").strip()
    if not _WEBHOOK_URL.fullmatch(url):
        raise study.StudyError("올바른 HTTPS Discord 웹훅 URL을 입력하세요.")
    token = getpass.getpass("GitHub API 토큰 (선택, 없으면 Enter): ").strip()
    values = {"DISCORD_WEBHOOK_URL": url}
    if token:
        values["GITHUB_TOKEN"] = token
    with SECRETS.open("x", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
        json.dump(values, stream)
        stream.write("\n")


def agent_definition(job):
    return {
        "Label": f"{LABEL}.{job}",
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), str(ROOT / "scripts/launchd.py"), "run", job],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                                 "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"},
        "StartCalendarInterval": [{"Hour": hour, "Minute": minute} for hour, minute in CLOCKS[job]],
    }


def check_owner(job):
    path = plist_path(job)
    if path.is_symlink():
        raise study.StudyError("등록 파일이 심볼릭 링크입니다. LaunchAgents 폴더를 확인하세요.")
    if path.exists():
        with path.open("rb") as stream:
            owner = plistlib.load(stream).get("WorkingDirectory")
        if owner != str(ROOT):
            raise study.StudyError("다른 체크아웃에 예약이 있습니다. 기존 위치에서 remove 후 다시 설치하세요.")


def install():
    check_clock()
    if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise study.StudyError(".venv/bin/python scripts/launchd.py install로 실행하세요.")
    study.check_checkout(ROOT)
    if study.git(ROOT, "rev-parse", "--is-shallow-repository") != "false":
        raise study.StudyError("전체 이력이 필요합니다. git fetch --unshallow origin을 실행하세요.")
    for job in CLOCKS:
        check_owner(job)
    study.git(ROOT, "fetch", "origin", "main")
    if study.git(ROOT, "rev-parse", "HEAD") != study.git(ROOT, "rev-parse", "origin/main"):
        raise study.StudyError("최신 main 체크아웃이 필요합니다. 미게시 기록을 보존한 채 Git 상태를 정리하세요.")
    study.git(ROOT, "var", "GIT_AUTHOR_IDENT")
    study.git(ROOT, "push", "--dry-run", "origin", "HEAD:main")
    store = DeliveryStore(ROOT)
    if not store.path.exists() and any(plist_path(job).exists() for job in CLOCKS):
        raise study.StudyError("기존 예약의 발송 기록이 없습니다. 초기화하지 말고 runtime.json을 복구하세요.")
    configure_secrets()
    if store.path.exists():
        store.load()
    else:
        store.initialize(datetime.now(study.KST))
    AGENTS.mkdir(parents=True, exist_ok=True)
    for job in CLOCKS:
        path = plist_path(job)
        system_command("/bin/launchctl", "bootout", target(job), required=False)
        with path.open("wb") as stream:
            plistlib.dump(agent_definition(job), stream)
        system_command("/usr/bin/plutil", "-lint", str(path))
        system_command("/bin/launchctl", "enable", target(job))
        system_command("/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path))
        system_command("/bin/launchctl", "print", target(job))
    hour = study.config.REMINDER_HOUR
    print(f"launchd 예약 3개 등록 완료: 매일 {hour:02}:00 발송, 00:00 마감, {hour:02}:05·00:05 점검")
    print("설치 과정에서는 Discord 메시지를 보내지 않았습니다.")


def remove():
    for job in CLOCKS:
        check_owner(job)
    for job in CLOCKS:
        system_command("/bin/launchctl", "bootout", target(job), required=False)
        if system_command("/bin/launchctl", "print", target(job), required=False).returncode == 0:
            raise study.StudyError("예약 중지에 실패했습니다. launchctl 상태를 확인하세요.")
        plist_path(job).unlink(missing_ok=True)
    print("예약을 제거했습니다. 웹훅 설정과 발송·마감 기록은 보존했습니다.")


def status():
    disabled = system_command("/bin/launchctl", "print-disabled", f"gui/{os.getuid()}").stdout
    for job in CLOCKS:
        check_owner(job)
        loaded = system_command("/bin/launchctl", "print", target(job), required=False).returncode == 0
        state = "비활성" if f'"{LABEL}.{job}" => true' in disabled else "등록됨" if loaded else "미등록"
        print(f"{LABEL}.{job}: {state}")
    if ERROR_LOG.exists():
        print(f"최근 오류: {ERROR_LOG}")


def report_failure(job, error):
    message = f"{job}: {error}\n"
    previous = ERROR_LOG.read_text(encoding="utf-8") if ERROR_LOG.exists() else ""
    ERROR_LOG.parent.mkdir(exist_ok=True)
    ERROR_LOG.write_text(message, encoding="utf-8")
    if message != previous:
        try:
            system_command("/usr/bin/osascript", "-e",
                           'display notification "자동 알림을 확인하세요. .study/last-error.log에 원인이 있습니다." with title "CS 스터디 실행 오류"',
                           required=False)
        except study.StudyError:
            pass


def execute(job):
    check_clock()
    os.environ.update(read_secrets())
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    store = DeliveryStore(ROOT)
    data = store.load()
    now = datetime.now(study.KST)
    if job == "check":
        issues = store.problems(data, study.calendar_events(), now)
        if issues:
            raise study.StudyError("; ".join(issues))
        ERROR_LOG.unlink(missing_ok=True)
        end = max(study.at(event["scheduled_at"]) for event in study.calendar_events()) + timedelta(minutes=5)
        if now >= end:
            for name in CLOCKS:
                system_command("/bin/launchctl", "disable", target(name))
        return
    selected = [event for event in study.due_events(now, kind=job) if store.active(event, data)]
    needs_delivery = any(data["events"].get(event_key(event), {}).get("status") not in ("sent", "skipped")
                         for event in selected)
    if selected and (needs_delivery or (job == "final" and data["publication_pending"])):
        # 읽기 전용 fetch는 미게시 로컬 결과를 덮어쓰거나 푸시하지 않는다.
        study.git(ROOT, "fetch", "origin", "main")
    if job == "reminder" and needs_delivery:
        with snapshot_tree(ROOT, "origin/main") as submissions:
            study.run(ROOT, datetime.now(study.KST), kind=job, send=True, submission_root=submissions)
    else:
        study.run(ROOT, datetime.now(study.KST), kind=job, send=True, persist_changes=(job == "final"))


def main():
    parser = argparse.ArgumentParser(description="macOS launchd 스터디 알림")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "status", "remove"):
        sub.add_parser(name)
    runner = sub.add_parser("run")
    runner.add_argument("job", choices=CLOCKS)
    args = parser.parse_args()
    try:
        if sys.platform != "darwin":
            raise study.StudyError("macOS에서 실행하세요.")
        os.environ["STUDY_RUNTIME_DIR"] = str(ROOT / ".study")
        study.validate_config()
        if args.command == "status":
            status()
        elif args.command == "remove":
            remove()
        else:
            with study.exclusive_lock(ROOT):
                if args.command == "install":
                    install()
                else:
                    with redirect_stdout(io.StringIO()):
                        execute(args.job)
        return 0
    except (study.StudyError, DeliveryError, SnapshotError, ValueError, OSError) as error:
        if args.command == "run":
            report_failure(args.job, error)
        print(f"오류: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
