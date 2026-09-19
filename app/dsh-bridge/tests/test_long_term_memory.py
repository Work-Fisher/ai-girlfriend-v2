import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(APP / 'dsh-bridge'), str(APP)]
from long_term_memory import MemoryStore
import server
import ui.server as ui


class LongTermTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.directory.name) / 'memory.sqlite', None)

    def tearDown(self):
        self.directory.cleanup()

    def test_first_and_second_turn_keep_real_user_message(self):
        for index, text in enumerate(['你好啊你现在知道我是谁吧', '我问的是我的名字']):
            if index == 0:
                ui.mark_affinity_opening()
            messages = [{'role': 'user', 'content': text},
                        {'role': 'user', 'content': 'Please reply to my message in Chinese.'}]
            result = ui.prepare_llm_payload({'messages': messages}, {'model': 'test'})
            self.assertEqual(result['messages'], messages)
            self.assertEqual(bool(result.get('companion_context')), index == 0)
            _, actual = server.split_messages([server.ChatMessage(**m) for m in result['messages']])
            self.assertEqual(actual, text)

    def test_manual_update_survives_background_extraction(self):
        source = self.store.record('s', '我叫小明', '你好')
        self.store.save_fact('姓名', '小明', source_id=source, quote='我叫小明')
        self.store.save_fact('姓名', '小林', manual=True)
        self.store.save_fact('姓名', '小明', source_id=source, quote='我叫小明')
        self.assertEqual(self.store.facts()[0]['value'], '小林')

    def test_deleted_fact_source_not_retrieved_or_recreated(self):
        source = self.store.record('s', '我喜欢香菜', '记住了')
        self.store.record('s', '我喜欢吃什么？', '你喜欢香菜')
        self.store.save_fact('饮食偏好', '喜欢香菜', source_id=source, quote='我喜欢香菜')
        self.store.delete_fact(self.store.facts()[0]['id'])
        self.store.save_fact('饮食偏好', '喜欢香菜', source_id=source, quote='我喜欢香菜')
        self.assertEqual(self.store.facts(), [])
        self.assertEqual(self.store.stats()['total'], 0)

    def test_facts_work_when_embedding_search_fails(self):
        self.store.save_fact('姓名', '小林', manual=True)
        with patch.object(self.store, 'search', side_effect=RuntimeError('model unavailable')):
            self.assertIn('小林', self.store.context('我是谁'))

    def test_legacy_language_suffix_preserves_real_history(self):
        path = Path(self.directory.name) / 'export.json'
        path.write_text(json.dumps({'source': 's', 'events': [
            {'type': 'user/message', 'time': 1000, 'data': {'id': 'm', 'content': [{'type': 'text', 'text': '我叫小林\n\n（Please reply to my message in Chinese.）'}]}},
            {'type': 'assistant/message', 'data': {'message': {'content': [{'type': 'text', 'text': '你好小林'}]}}},
            {'type': 'turn/end', 'data': {'reason': {'kind': 'completed'}}},
        ]}), encoding='utf8')
        self.store.import_history(path)
        self.store.import_history(path)
        self.assertEqual(self.store.stats()['total'], 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT user FROM turns').fetchone()['user'], '我叫小林')

    def test_clear_removes_facts_and_searchable_turns(self):
        self.store.record('s', '测试', '好的')
        self.store.save_fact('姓名', '测试', manual=True)
        self.store.clear()
        self.assertEqual(self.store.stats(), {'facts': 0, 'total': 0, 'indexed': 0})

    def test_memory_http_crud(self):
        from fastapi.testclient import TestClient
        with patch.object(server, '_store', self.store):
            client = TestClient(server.app)
            result = client.post('/memory/facts', json={'key': '称呼', 'value': '小林'})
            self.assertEqual(result.status_code, 200)
            fact = client.get('/memory').json()['facts'][0]
            client.post('/memory/facts', json={'key': '称呼', 'value': '阿林'})
            self.assertEqual(client.get('/memory').json()['facts'][0]['value'], '阿林')
            client.delete(f"/memory/facts/{fact['id']}")
            self.assertEqual(client.get('/memory').json()['facts'], [])


if __name__ == '__main__':
    unittest.main()
