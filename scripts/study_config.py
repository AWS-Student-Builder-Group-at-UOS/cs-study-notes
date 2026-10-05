# 명단 변경 후 sync를 실행하면 참여자 폴더를 동기화합니다.
MEMBERS = ("손수빈", "황수진", "이예나", "장지원", "최진영")

MEMBER_EMOJIS = {
    "손수빈": "🐿️",
    "황수진": "🐲",
    "이예나": "🙀",
    "장지원": "🤪",
    "최진영": "🐰",
}
DEFAULT_MEMBER_EMOJI = "🐾"
BOT_NAME = "회고냥"
MISSED_SUBMISSION_FINE = 10_000

DEADLINES = (
    "2026-10-04",
    "2026-10-18",
    "2026-11-01",
    "2026-11-15",
    "2026-11-29",
    "2026-12-13",
    "2026-12-27",
)

# 기존 회차·제출 폴더와 사전 알림은 유지하고, 실제 마감·최종 처리를 연장합니다.
DEADLINE_EXTENSIONS = {"2026-10-04": "2026-10-05"}

TIMEZONE = "Asia/Seoul"
REMINDER_DAYS = (7, 3, 1, 0)
REMINDER_HOUR = 9
SEND_FINAL_RESULTS = True
