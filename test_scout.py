import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from souffleur.scout import ScoutError, ScoutWriter, resolve_clawpilot_exe


class ClawpilotExecutableTests(unittest.TestCase):
    def test_clawpilot_launch_does_not_inherit_console_output(self):
        writer = ScoutWriter(exe=r"C:\Clawpilot\Clawpilot.exe")
        with patch(
            "souffleur.scout.resolve_clawpilot_exe",
            return_value=writer.exe,
        ), patch("souffleur.scout.subprocess.Popen") as popen:
            self.assertTrue(writer.launch())

        popen.assert_called_once_with(
            [writer.exe],
            stdin=-3,
            stdout=-3,
            stderr=-3,
            close_fds=True,
        )

    def test_auto_detects_program_files_install(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "Clawpilot" / "Clawpilot.exe"
            executable.parent.mkdir()
            executable.touch()
            env = {
                "ProgramFiles": temp,
                "ProgramFiles(x86)": "",
                "LOCALAPPDATA": "",
            }
            with patch.dict(os.environ, env, clear=True), patch(
                "souffleur.scout.shutil.which", return_value=None
            ), patch("souffleur.scout._registry_clawpilot_paths", return_value=[]):
                self.assertEqual(resolve_clawpilot_exe("auto"), str(executable))

    def test_missing_configured_path_falls_back_to_auto_detection(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "Programs" / "Clawpilot" / "Clawpilot.exe"
            executable.parent.mkdir(parents=True)
            executable.touch()
            env = {
                "ProgramFiles": "",
                "ProgramFiles(x86)": "",
                "LOCALAPPDATA": temp,
            }
            with patch.dict(os.environ, env, clear=True), patch(
                "souffleur.scout.shutil.which", return_value=None
            ), patch("souffleur.scout._registry_clawpilot_paths", return_value=[]):
                self.assertEqual(
                    resolve_clawpilot_exe(r"C:\missing\Clawpilot.exe"),
                    str(executable),
                )

    def test_reports_how_to_override_when_not_found(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "souffleur.scout.shutil.which", return_value=None
        ), patch("souffleur.scout._registry_clawpilot_paths", return_value=[]):
            with self.assertRaisesRegex(ScoutError, "CLAWPILOT_EXE"):
                resolve_clawpilot_exe("auto")


if __name__ == "__main__":
    unittest.main()
