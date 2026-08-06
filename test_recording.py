import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from souffleur.daemon import MeetingRecorder, safe_filename_component
from souffleur.teams_ui import meeting_name_from_window_title


class MeetingRecordingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
