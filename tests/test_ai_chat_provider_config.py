import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import provider_config


class AIChatProviderConfigTests(unittest.TestCase):
    def test_plain_key_is_replaced_by_windows_credential_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.json"
            with patch.object(provider_config, "_store_windows_secret", return_value="wincred:Jiedu.AI.Provider") as store:
                result = provider_config.save_ai_chat("openai-compatible", "fixture-model", "https://fixture.invalid/v1", "plain-fixture-key", path)
            store.assert_called_once_with("plain-fixture-key")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("plain-fixture-key", text)
            self.assertIn("wincred:Jiedu.AI.Provider", text)
            self.assertEqual("已配置", result["state"])


if __name__ == "__main__":
    unittest.main()
