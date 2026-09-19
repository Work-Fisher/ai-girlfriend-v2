import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from memory import read_json, write_json, commit_session, recovery_candidates


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.patches = [
            patch.object(server, 'SESSION_PATH', self.root / 'session.json'),
            patch.object(server, '_cfg', {'dsh_home': str(self.root)}),
            patch.object(server, '_active_session_id', 'new'),
            patch.object(server, '_memory_error', ''),
            patch.object(server, '_harness', None),
            patch.object(server, '_store', None),
        ]
        for item in self.patches:
            item.start()
        write_json(server.SESSION_PATH, {'session_id': 'previous', 'event_count': 12})

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.directory.cleanup()

    def test_atomic_failure_preserves_checkpoint(self):
        with patch('memory.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                write_json(server.SESSION_PATH, {'session_id': 'lost'})
        self.assertEqual(read_json(server.SESSION_PATH)['session_id'], 'previous')

    def test_commit_retains_event_boundaries_for_fallback(self):
        commit_session(server.SESSION_PATH, 'new', {}, 24)
        self.assertEqual(recovery_candidates(read_json(server.SESSION_PATH)), [
            {'session_id': 'new', 'event_count': 24},
            {'session_id': 'previous', 'event_count': 12},
        ])

    def test_broken_status_closes_runtime_and_preserves_checkpoint(self):
        closed = []
        harness = SimpleNamespace(close=lambda: closed.append(True))
        server._harness = harness
        with self.assertRaises(server.HTTPException):
            server.run_with_session(harness, 'ignored')
        self.assertTrue(closed)
        self.assertIsNone(server._harness)
        self.assertEqual(read_json(server.SESSION_PATH)['session_id'], 'previous')

    def test_reset_cannot_race_an_active_turn(self):
        with server._run_lock:
            with self.assertRaises(server.HTTPException) as error:
                server.reset()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(read_json(server.SESSION_PATH)['session_id'], 'previous')

    def test_explicit_reset_does_not_recover_old_history(self):
        server.reset()
        self.assertEqual(recovery_candidates(read_json(server.SESSION_PATH)), [])

    def test_corrupt_index_is_not_treated_as_empty(self):
        server.SESSION_PATH.write_text('{broken', encoding='utf8')
        with self.assertRaises(server.HTTPException) as error:
            server._read_session_state()
        self.assertEqual(error.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
