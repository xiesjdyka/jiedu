"""Synthetic freshness checks for public product help; no user data is read."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from knowledge_bridge import product_help


class ProductHelpCurrentTests(unittest.TestCase):
    def test_changed_evidence_never_sends_old_prose_as_current_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'implementation.py'
            evidence.write_text('old', encoding='utf-8')
            payload = {'topics': [{
                'id': 'software_update', 'title': '更新', 'status': 'implemented',
                'summary': '旧说明：更新尚未公开', 'usage': '旧入口',
                'details': '旧说明：安装器不带 OCR', 'limits': '旧边界',
                'verification': '旧验收', 'keywords': ['更新'],
                'evidence': [{'path': 'implementation.py',
                              'sha256': hashlib.sha256(b'old').hexdigest()}],
            }]}
            data_path = root / 'product_capabilities.json'
            data_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            with patch.object(product_help, 'ROOT', root), patch.object(product_help, 'DATA_PATH', data_path):
                self.assertFalse(product_help.catalog()['topics'][0]['needs_recheck'])
                evidence.write_text('new', encoding='utf-8')
                topic = product_help.catalog()['topics'][0]
                self.assertTrue(topic['needs_recheck'])
                self.assertEqual(topic['status_label'], '待复核')
                self.assertNotIn('更新尚未公开', json.dumps(topic, ensure_ascii=False))
                self.assertNotIn('安装器不带 OCR', json.dumps(topic, ensure_ascii=False))
                sources, _ = product_help.model_sources('界渡更新', ['software_update'])
                combined = '\n'.join(source['content'] for source in sources)
                self.assertNotIn('更新尚未公开', combined)
                self.assertNotIn('安装器不带 OCR', combined)
                self.assertIn('product_status', combined)
                self.assertIn('当前状态未知', combined)

    def test_current_public_release_and_connect_help_prose(self):
        data = json.loads(product_help.DATA_PATH.read_text(encoding='utf-8'))
        topics = {topic['id']: topic for topic in data['topics']}
        update = topics['software_update']
        self.assertIn('1.1.8', update['summary'])
        self.assertIn('已公开发布', update['summary'])
        self.assertIn('下载并校验 OCR 和语义向量资源', update['details'])
        self.assertIn('不附带管家生成模型', update['details'])
        self.assertIn('不代表', update['limits'])
        sources = topics['sources']
        self.assertIn('DSH 与 Claude', sources['details'])
        self.assertIn('助手最终答复', sources['details'])
        self.assertIn('Claude Desktop 安装', sources['details'])
        self.assertIn('主人确认', topics['butler']['summary'])
        self.assertIn('云端普通聊天', sources['limits'])
        self.assertIn('本机 ChatGPT 工作/Codex', sources['limits'])

    def test_new_navigation_obeys_registration_and_tool_switch(self):
        enabled = product_help.request_identity(
            tools_enabled=True,
            registered_operations=('product_status', 'source_archive_progress', 'source_archive_resume'),
        )
        self.assertIn('product_status、source_archive_progress、source_archive_resume', enabled)
        self.assertIn('仅在本轮实际注册且未被显式限制时调用', enabled)
        disabled = product_help.request_identity(
            tools_enabled=False,
            registered_operations=('product_status', 'source_archive_progress', 'source_archive_resume'),
        )
        self.assertIn('本轮工具关闭，不能调用诊断、恢复', disabled)
        self.assertNotIn('实际注册操作导航', disabled)
        for name in ('DSH', 'Claude'):
            selected, topics = product_help.select_topics('界渡 '+name+' 聊天归档')
            self.assertTrue(selected)
            self.assertIn('sources', topics)


if __name__ == '__main__':
    unittest.main()
