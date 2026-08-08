import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from souffleur.daemon import MeetingRecorder, safe_filename_component
from souffleur.teams_ui import TranscriptReader, meeting_name_from_window_title


class MeetingRecordingTests(unittest.TestCase):
    def test_initializes_ui_automation_in_reader_thread(self):
        reader = TranscriptReader()
        with patch(
            "souffleur.teams_ui.auto.UIAutomationInitializerInThread"
        ) as initializer, patch.object(
            reader, "_run_initialized"
        ) as run_initialized:
            reader._run()

        initializer.assert_called_once_with()
        initializer.return_value.__enter__.assert_called_once_with()
        run_initialized.assert_called_once_with()

    def test_extracts_channel_meeting_name(self):
        title = "test (General) | Microsoft | user@example.com | Microsoft Teams"
        self.assertEqual(meeting_name_from_window_title(title), "test")

    def test_escapes_windows_filename_characters(self):
        self.assertEqual(
            safe_filename_component('Review: Q3/Q4 * "draft"?'),
            "Review- Q3-Q4 - -draft-",
        )
        self.assertEqual(safe_filename_component("CON"), "_CON")

    def test_writes_live_markdown_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = MeetingRecorder(
                temp, datetime(2026, 6, 30, 16, 30)
            )
            recorder.start_meeting(
                "test", (1,), datetime(2026, 6, 30, 16, 30)
            )
            recorder.add_final("<Alice>: Hello")
            recorder.set_live("<Bob>: Hi there")

            content = recorder.path.read_text(encoding="utf-8")
            self.assertIn("# test", content)
            self.assertIn("<Alice>: Hello", content)
            self.assertIn("<Bob>: Hi there", content)

    def test_resumes_existing_meeting_without_duplicate_replayed_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            started_at = datetime.now().replace(microsecond=0)
            first = MeetingRecorder(temp)
            path = first.start_meeting(
                "test", (1,), started_at
            )
            first.add_final("<Alice>: First")
            first.add_final("<Bob>: Second")
            first.set_live("<Alice>: unfinished")

            restarted = MeetingRecorder(temp)
            resumed = restarted.start_meeting(
                "test", (1,), started_at + timedelta(minutes=10)
            )
            restarted.add_final("<Alice>: First")
            restarted.add_final("<Bob>: Second")
            restarted.add_final("<Alice>: Third")

            self.assertEqual(resumed, path)
            content = path.read_text(encoding="utf-8")
            self.assertEqual(content.count("<Alice>: First"), 1)
            self.assertEqual(content.count("<Bob>: Second"), 1)
            self.assertEqual(content.count("<Alice>: Third"), 1)
            self.assertNotIn("unfinished", content)
            self.assertIn(started_at.astimezone().isoformat(), content)

    def test_different_meeting_does_not_append_to_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            first = MeetingRecorder(temp)
            first_path = first.start_meeting(
                "test", (1,), datetime(2026, 6, 30, 16, 30)
            )
            first.add_final("<Alice>: First meeting")

            second = MeetingRecorder(temp)
            second_path = second.start_meeting(
                "test", (2,), datetime(2026, 6, 30, 16, 30)
            )
            second.add_final("<Bob>: Second meeting")

            self.assertNotEqual(first_path, second_path)
            self.assertNotIn(
                "First meeting", second_path.read_text(encoding="utf-8")
            )

    def test_flushes_each_finalized_line_to_disk(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = MeetingRecorder(temp)
            recorder.start_meeting("test", (1,))
            with patch("souffleur.daemon.os.fsync") as fsync:
                recorder.add_final("<Alice>: Durable")

            fsync.assert_called_once()
            self.assertIn(
                "<Alice>: Durable", recorder.path.read_text(encoding="utf-8")
            )

    def test_rotates_file_when_meeting_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = MeetingRecorder(temp)
            first = recorder.start_meeting(
                "test", (1,), datetime(2026, 6, 30, 16, 30)
            )
            recorder.add_final("<Alice>: First meeting")
            second = recorder.start_meeting(
                "review", (2,), datetime(2026, 6, 30, 16, 31)
            )
            recorder.add_final("<Bob>: Second meeting")

            self.assertNotEqual(first, second)
            self.assertIn(
                "First meeting", first.read_text(encoding="utf-8")
            )
            second_content = second.read_text(encoding="utf-8")
            self.assertIn("# review", second_content)
            self.assertIn("Second meeting", second_content)
            self.assertNotIn("First meeting", second_content)

    def test_rotates_when_title_changes_in_same_teams_window(self):
        with tempfile.TemporaryDirectory() as temp:
            recorder = MeetingRecorder(temp)
            first = recorder.start_meeting(
                "standup", (1,), datetime(2026, 6, 30, 16, 30)
            )
            second = recorder.start_meeting(
                "review", (1,), datetime(2026, 6, 30, 16, 31)
            )

            self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
