from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts import study


class RoundFolderTests(unittest.TestCase):
    def test_next_round_stays_closed_through_deadline(self):
        deadlines = study.config.DEADLINES
        for current, following in zip(deadlines, deadlines[1:]):
            for clock in (time(), time(9), time(23, 59, 59)):
                with self.subTest(deadline=current, clock=clock), TemporaryDirectory() as directory:
                    root = Path(directory)
                    now = datetime.combine(date.fromisoformat(current), clock, study.KST)
                    state = study.load_state(root)
                    study.sync_folders(root, state, now)
                    for member in study.config.MEMBERS:
                        self.assertTrue((root / member / current).is_dir())
                        self.assertFalse((root / member / following).exists())

    def test_next_round_opens_after_midnight_and_preserves_submissions(self):
        deadlines = study.config.DEADLINES
        for current, following in zip(deadlines, deadlines[1:]):
            with self.subTest(deadline=current), TemporaryDirectory() as directory:
                root = Path(directory)
                cutoff = datetime.combine(
                    date.fromisoformat(current) + timedelta(days=1), time(), study.KST)
                state = study.load_state(root)
                study.sync_folders(root, state, cutoff - timedelta(seconds=1))
                submission = root / study.config.MEMBERS[0] / current / "notes.md"
                submission.write_text("운영체제 학습 기록", encoding="utf-8")

                # UTC input must cross the same Korean midnight boundary.
                study.sync_folders(root, state, cutoff.astimezone(timezone.utc))
                for member in study.config.MEMBERS:
                    self.assertTrue((root / member / current).is_dir())
                    self.assertTrue((root / member / following / ".gitkeep").is_file())
                    self.assertEqual(
                        sorted(path.name for path in (root / member).iterdir()),
                        [current, following])
                self.assertEqual(submission.read_text(encoding="utf-8"), "운영체제 학습 기록")
                self.assertEqual(study.sync_folders(root, state, cutoff), [])

    def test_final_round_does_not_create_another_folder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            current = study.config.DEADLINES[-1]
            now = datetime.combine(date.fromisoformat(current), time(23, 59, 59), study.KST)
            state = study.load_state(root)
            study.sync_folders(root, state, now)
            study.sync_folders(root, state, now + timedelta(seconds=1))
            for member in study.config.MEMBERS:
                self.assertEqual([path.name for path in (root / member).iterdir()], [current])


if __name__ == "__main__":
    unittest.main()
