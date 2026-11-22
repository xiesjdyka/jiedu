import unittest

from knowledge_bridge.source_adapters import adapter_id_for


class SourceAdapterSelectionTests(unittest.TestCase):
    def test_antigravity_archive_is_independent_of_mcp_and_separate_from_other_sources(self):
        import tempfile
        from pathlib import Path
        from knowledge_bridge.archive_store import ArchiveStore
        from knowledge_bridge.visible_conversations import conversation, list_conversations
        from knowledge_bridge.workspace import initialize_workspace

        self.assertEqual(adapter_id_for("Antigravity"), "antigravity_local")
        with tempfile.TemporaryDirectory() as folder:
            ctx = initialize_workspace(Path(folder) / "workspace")
            store = ArchiveStore(ctx)
            code = "Code follows:\n```py\nprint(42)\n```"
            for source, body in (("antigravity", code), ("codex", "another source")):
                row = store.ensure_session(source_key=source, source_session_id="same",
                                           title="example", source_instance=source + ":local")
                store.append_message(row["session_key"], message_id="a", role="assistant", content=body)
            sessions = list_conversations("antigravity", ctx)["sessions"]
            self.assertEqual([(r["session_id"], r["saved_messages"]) for r in sessions], [("same", 1)])
            details = conversation("antigravity", "same", ctx)
            self.assertEqual([r["text"] for r in details["messages"]], [code])

    def test_bound_source_key_can_select_adapter_for_legacy_client_type(self):
        legacy_client_type = "desktop_agent"
        bound_source_key = "doubao_work"
        selected = adapter_id_for(bound_source_key or legacy_client_type)
        self.assertEqual("doubao_work_local", selected)


if __name__ == "__main__":
    unittest.main()
