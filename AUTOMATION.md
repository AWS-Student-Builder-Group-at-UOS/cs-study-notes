# 자동 알림 운영

macOS `launchd`가 Python을 실행해 Discord 웹훅으로 보냅니다. **Mac의 시간대를 서울로 설정하고, 로그인·인터넷 연결을 유지하며 예약 시각에 잠자지 않도록 합니다.** 별도 서버나 유료 API는 필요하지 않습니다. 기기·네트워크 상황에 따라 도착 시각은 늦어질 수 있습니다.

## 1. 최초 설치

Python 3.9 이상과 Git, 저장소 조회·푸시 권한 및 Git 작성자 설정이 필요합니다. 작업용 폴더와 분리한 **운영 전용 체크아웃**에서 실행합니다.

```sh
mkdir -p ~/study-automation
cd ~/study-automation
git clone https://github.com/AWS-Student-Builder-Group-at-UOS/cs-study-notes.git
cd cs-study-notes
python3 -m venv .venv
.venv/bin/python -m pip install -r scripts/requirements.txt
.venv/bin/python scripts/launchd.py install
```

설치 명령은 웹훅 URL을 **화면에 표시하지 않고** 입력받습니다. GitHub API 토큰은 선택 사항이며 Git 푸시 인증은 별도로 설정되어 있어야 합니다. 값은 Git에서 제외한 `.study/secrets.json`에 본인만 읽고 쓸 수 있는 권한으로 저장합니다. 채팅·소스·plist에 웹훅을 적지 않습니다.

시간대·최신 `main`·전체 Git 이력·작성자·푸시 권한을 점검하고, `~/Library/LaunchAgents/`에 예약 3개를 등록합니다. 기존 발송 기록은 보존하며 **설치 시 실제 메시지는 보내지 않습니다.** 이후 터미널을 닫아도 실행되지만 로그아웃하면 사용자 예약이 실행되지 않습니다.

## 2. 실행 일정

모든 시각은 **한국 시간**입니다. 마감일은 [`scripts/study_config.py`](scripts/study_config.py)를 기준으로 판단합니다.
첫 회차만 **10월 5일(월) 23:59 마감 → 10월 6일 00:00 최종 확인**으로 연장하며, 제출 폴더도 `2026-10-05`를 사용합니다. 기존 첫 회차 확정 결과·벌금은 취소하고, 나머지 일정은 유지합니다.

| 실행 | 역할 |
| --- | --- |
| 매일 09:00 | 마감 7일 전·3일 전·전날·당일에만 제출 현황 발송 |
| 매일 00:00 | 마감 다음 날에만 결과 확정·다음 폴더 생성·발송·Git 게시 |
| 매일 09:05·00:05 | 발송 누락·실패·도착 불명·Git 게시 실패 점검 |

다음 폴더는 제출이 끝난 **다음 날 00:00 이후**에 생성합니다. 첫 회차 연장으로 `2026-10-18` 폴더는 **10월 6일 00:00 이후**에 열립니다. 마지막 회차 뒤에는 새 폴더를 만들지 않습니다. 마지막 알림 5분 뒤 점검에서 문제가 없으면 예약을 자동 비활성화합니다. 현재 종료 시점은 **2026년 12월 28일 00:05**입니다.

오전 현황은 `origin/main`을 가져와 읽으므로 새 제출을 확인하면서도 미게시 로컬 결과를 덮어쓰지 않습니다. 마감 결과는 Discord 전송과 Git 게시를 분리해 처리합니다.

## 3. 확인·중지·재설치

운영 체크아웃에서 실행합니다.

```sh
.venv/bin/python scripts/launchd.py status   # 예약 등록 여부
.venv/bin/python scripts/study.py schedule  # 전체 일정과 알림 ID
.venv/bin/python scripts/study.py check     # 발송·게시 누락 점검
.venv/bin/python scripts/launchd.py remove  # 예약 제거, 설정·기록 보존
```

다시 켜려면 `install`을 실행합니다. 명단·일정·스크립트를 변경했다면 미게시 기록을 먼저 해결하고 `git pull --ff-only`로 갱신한 뒤 의존성 설치와 `install`을 다시 실행합니다. 명단 변경 후에는 `sync --persist`로 참여자 폴더를 동기화합니다. 탈퇴·복귀 폴더와 기존 제출물은 보존합니다.

## 4. 기록과 오류 복구

- `.study/state.json`: 확정된 제출 결과·벌금·참여자 폴더. Git에 보관합니다.
- `.study/runtime.json`: 알림별 최신 상태·Discord 메시지 ID·Git 게시 미완료 여부. 운영 Mac에 보관하며 지우거나 매번 초기화하지 않습니다.
- `.study/last-error.log`: **가장 최근 오류 하나만** 덮어써서 보관합니다. 새 오류는 Mac 알림으로도 안내하고, 정상 점검 후 오류 파일을 지웁니다. 상세 실행 로그·메시지 전문은 쌓지 않습니다.

예정 시각부터 **10분 미만**일 때만 전송합니다. Discord의 성공 응답과 메시지 ID를 확인해야 발송 완료이며, 이미 보낸 알림은 다시 보내지 않습니다. 연결 단절·서버 오류·실행 중단으로 도착이 불명확하면 자동 재전송하지 않습니다. Mac이 꺼져 있어 발송과 점검 모두 실행되지 않으면 즉시 감지할 수 없습니다.

| 상황 | 조치 |
| --- | --- |
| 명확한 발송 실패 | 원인을 해결하고 10분 이내에 `scripts/launchd.py run reminder` 또는 `run final`을 같은 가상 환경 Python으로 실행 |
| 도착 불명 | Discord 채널을 먼저 확인하고, 도착했다면 `resolve --event '<알림 ID>' --message-id '<메시지 ID>'`로 기록 |
| 미도착을 확인한 불명 상태 | 10분 이내라면 `resolve --event '<알림 ID>' --retry` 후 해당 발송 명령 재실행 |
| 오래된 누락을 생략하기로 결정 | `resolve --event '<알림 ID>' --skip` |
| Git 게시 실패·늦어진 마감 처리 | 권한·충돌을 해결한 뒤 `sync --persist`; Discord에는 재전송하지 않음 |

`resolve`와 `sync`는 `.venv/bin/python scripts/study.py` 뒤에 붙입니다. 발송 실패를 숨기려고 기록을 초기화하지 않습니다. 설정·발송 기록을 보존한 채 복구합니다.

## 제출 판정

이름/마감일 폴더의 글이 있는 `.md`, `.txt`, `.html`, 텍스트 포함 `.pdf`를 인정합니다. `.gitkeep`, 빈 내용, 숨김 경로, 심볼릭 링크는 제외합니다. 마감 뒤에는 **다음 날 00:00 미만에 실제 GitHub `main`에 반영된 상태**로 명단·제출·벌금액을 확정하므로 늦게 푸시한 과거 작성 커밋은 인정하지 않습니다. 이후 파일이나 설정을 바꿔도 확정 결과는 바뀌지 않습니다.

현재 참여자의 누적 미제출 벌금과 횟수만 표시합니다. 실제 납부와 중도 포기 벌금은 운영자가 별도로 관리합니다.
