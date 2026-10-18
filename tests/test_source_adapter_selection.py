import unittest

from knowledge_bridge.source_adapters import adapter_id_for


class SourceAdapterSelectionTests(unittest.TestCase):
    def test_bound_source_key_can_select_adapter_for_legacy_client_type(self):
        legacy_client_type = "desktop_agent"
        bound_source_key = "doubao_work"
        selected = adapter_id_for(bound_source_key or legacy_client_type)
        self.assertEqual("doubao_work_local", selected)


if __name__ == "__main__":
    unittest.main()
