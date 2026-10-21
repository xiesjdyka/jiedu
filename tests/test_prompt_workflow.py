from pathlib import Path
import tempfile
import unittest

from knowledge_bridge.prompt_workflow import distillation_gate, save_startup, startup_status, restore_default
from knowledge_bridge.workspace import initialize_workspace


def codex(status="completed", *, sqlite="complete", failed=0, review=0, configured=True):
    return {"id": "codex", "detected": True, "readable": True, "configured": configured,
            "participating": True, "last_error": None, "source_issues": [],
            "locations": [{"kind": "jsonl", "detected": True}, {"kind": "sqlite", "detected": True}],
            "history": {"status": status, "by_source": [
                {"source": "jsonl", "status": status, "failed_missing": failed},
                {"source": "thread_history_1.sqlite", "status": sqlite, "failed_missing": 0, "needs_review": review}]}}


def hermes(status="complete", *, missing=0, configured=True):
    return {"id": "hermes", "detected": True, "readable": True, "configured": configured,
            "participating": True, "history": {"status": status, "failed_missing": missing}}


class PromptWorkflowTests(unittest.TestCase):
    def test_only_participating_sources_block_and_require_explicit_completion(self):
        self.assertFalse(distillation_gate({"sources": []})["ready"])
        self.assertTrue(distillation_gate({"sources": [hermes()]})["ready"])
        self.assertTrue(distillation_gate({"sources": [hermes(), {"id": "codex", "detected": True,
            "configured": False, "participating": False, "history": {"status": "not_started"}}]})["ready"])
        self.assertFalse(distillation_gate({"sources": [hermes(), codex("importing")]})["ready"])
        self.assertFalse(distillation_gate({"sources": [hermes(), codex(sqlite="completed_with_review")]})["ready"])
        self.assertFalse(distillation_gate({"sources": [hermes(), codex(failed=1)]})["ready"])
        self.assertFalse(distillation_gate({"sources": [hermes(), codex(review=1)]})["ready"])
        self.assertFalse(distillation_gate({"sources": [hermes("paused", configured=False)]})["ready"])
        paused = codex(configured=False)
        paused["paused"] = True
        self.assertFalse(distillation_gate({"sources": [paused]})["ready"])
        self.assertFalse(distillation_gate({"sources": [codex(), hermes(missing=1)]})["ready"])
        self.assertTrue(distillation_gate({"sources": [codex(), hermes()]})["ready"])

    def test_workspace_prompt_stays_with_its_workspace_and_restores_default(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = initialize_workspace(root / "one")
            two = initialize_workspace(root / "two")
            save_startup(one, "Use confirmed facts from this workspace.", mode="generated",
                         source_files=["00_系统/背景.md"])
            self.assertEqual("generated", startup_status(one, "default")["mode"])
            self.assertEqual("default", startup_status(two, "default")["mode"])
            save_startup(one, "My edited prompt", mode="custom")
            self.assertEqual("My edited prompt", startup_status(one, "default")["prompt"])
            restore_default(one)
            self.assertEqual("default", startup_status(one, "default")["prompt"])
            with self.assertRaises(ValueError):
                save_startup(one, "sk-" + "a" * 32, mode="custom")


if __name__ == "__main__":
    unittest.main()
