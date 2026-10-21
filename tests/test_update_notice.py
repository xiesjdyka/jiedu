"""The update notice follows the verified local installation, not browser state."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from knowledge_bridge import update_notice as notice


class UpdateNoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.install = self.root / "Jiedu"
        self.program = self.install / ".jiedu" / "program"
        self.program.mkdir(parents=True)
        (self.install / "jiedu-install.txt").write_text(notice.INSTALL_IDENTITY, "ascii")
        self.state_root = self.root / "machine-state"
        self.version("1.1.6")

    def version(self, value):
        (self.program / "VERSION").write_text(value, "ascii")
        (self.program / "pyproject.toml").write_text(f'[project]\nversion = "{value}"\n', "utf-8")

    def test_new_install_uses_first_guide_once_across_workspace_changes(self):
        first = notice.record_install(self.program, state_root=self.state_root)
        self.assertEqual("fresh_install", first["kind"])
        self.assertTrue(first["show_first_run"])
        self.assertFalse(first["show_update"])
        notice.complete_onboarding(self.program, state_root=self.state_root)
        # Browser storage and the selected knowledge workspace are absent from
        # the installation identity and cannot trigger the guide again.
        self.assertFalse(notice.status(self.program, state_root=self.state_root)["show_first_run"])
        self.assertFalse(notice.record_install(self.program, state_root=self.state_root)["show_first_run"])

    def test_upgrade_acknowledgement_and_same_version_reinstall(self):
        first = notice.record_install(self.program, "1.1.5", state_root=self.state_root)
        self.assertTrue(first["show_update"])
        self.assertTrue(first["onboarding_completed"])
        again = notice.record_install(self.program, state_root=self.state_root)
        self.assertEqual("upgrade", again["kind"])
        self.assertTrue(again["show_update"])
        notice.acknowledge(self.program, state_root=self.state_root)
        self.assertFalse(notice.status(self.program, state_root=self.state_root)["show_update"])
        self.assertFalse(notice.record_install(self.program, "1.1.6", state_root=self.state_root)["show_update"])

    def test_old_helper_supplies_previous_version_after_moving_install(self):
        token = "a" * 32
        backup = self.install.with_name("Jiedu.previous-" + token)
        old_program = backup / ".jiedu" / "program"
        old_program.mkdir(parents=True)
        (old_program / "VERSION").write_text("1.1.5", "ascii")
        (old_program / "pyproject.toml").write_text('[project]\nversion = "1.1.5"\n', "utf-8")
        work = self.root / (".jiedu-update-" + token)
        work.mkdir()
        (work / "job.json").write_text(json.dumps({"token": token, "kind": "complete-installer",
            "target": str(self.install), "backup": str(backup), "version": "1.1.6",
            "old_version": "1.1.5"}), "utf-8")
        (work / "result.json").write_text(json.dumps({"token": token, "state": "applying"}), "utf-8")
        result = notice.record_install(self.program, state_root=self.state_root)
        self.assertEqual("pending_confirmation", result["state"])
        self.assertFalse(result["show_update"])
        (work / "result.json").write_text(json.dumps({"token": token, "state": "complete"}), "utf-8")
        result = notice.status(self.program, state_root=self.state_root)
        self.assertEqual("1.1.5", result["previous_version"])
        self.assertTrue(result["show_update"])

    def test_failed_install_or_rollback_never_claims_success(self):
        self.assertEqual("unconfirmed", notice.status(self.program, state_root=self.state_root)["state"])
        with patch.object(notice, "_write", side_effect=OSError("disk busy")):
            with self.assertRaises(OSError):
                notice.record_install(self.program, "1.1.5", state_root=self.state_root)
        self.assertFalse(notice.status(self.program, state_root=self.state_root)["show_update"])
        self.assertTrue(notice.record_install(self.program, "1.1.5", state_root=self.state_root)["show_update"])
        self.version("1.1.5")  # update helper restored the old installation
        rolled_back = notice.status(self.program, state_root=self.state_root)
        self.assertEqual("unconfirmed", rolled_back["state"])
        self.assertFalse(rolled_back["show_update"])

    def test_permissions_pause_and_archive_progress_are_untouched(self):
        control = self.root / "workspace" / ".jiedu" / "control"
        control.mkdir(parents=True)
        original = {"deny": True, "paused": True, "scope": "one-project", "history_cursor": 812}
        file = control / "customer-settings.json"
        file.write_text(json.dumps(original), "utf-8")
        notice.record_install(self.program, "1.1.5", state_root=self.state_root)
        notice.acknowledge(self.program, state_root=self.state_root)
        self.assertEqual(original, json.loads(file.read_text("utf-8")))


if __name__ == "__main__":
    unittest.main()
