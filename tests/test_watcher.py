import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import watcher

DOC = {'guild': {'id': '10'}, 'channel': {'id': '20', 'name': 'general'},
       'messages': [{'id': '100', 'author': {'id': '30', 'name': 'Alice'},
                     'timestamp': '2026-10-26T12:00:00Z', 'content': 'Hallo'}]}


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/state.db'
        self.db = watcher.database(self.path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_filters(self):
        for users, channels, guilds in [({'99'}, set(), set()), ({'30'}, {'99'}, set()),
                                         ({'30'}, set(), {'99'})]:
            self.assertEqual(watcher.ingest(self.db, DOC, users, channels, guilds), 0)
        self.assertEqual(watcher.ingest(self.db, DOC, {'30'}, {'20'}, {'10'}), 1)

    def test_restart_dedup_and_link(self):
        watcher.ingest(self.db, DOC, {'30'}, set(), set())
        watcher.ingest(self.db, DOC, {'30'}, set(), set())
        delivered = []
        watcher.deliver(self.db, delivered.append)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['url'], 'https://discord.com/channels/10/20/100')
        self.db.close()
        self.db = watcher.database(self.path)
        self.assertEqual(watcher.ingest(self.db, DOC, {'30'}, set(), set()), 0)
        watcher.deliver(self.db, delivered.append)
        self.assertEqual(len(delivered), 1)

    def test_failed_push_survives_restart(self):
        watcher.ingest(self.db, DOC, {'30'}, set(), set())
        with self.assertRaises(OSError):
            watcher.deliver(self.db, lambda _: (_ for _ in ()).throw(OSError()))
        self.db.close()
        self.db = watcher.database(self.path)
        delivered = []
        watcher.deliver(self.db, delivered.append)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pending').fetchone()[0], 0)

    def test_pushover_request_and_rejection(self):
        watcher.ingest(self.db, DOC, {'30'}, set(), set())
        payload = json.loads(self.db.execute('SELECT payload FROM pending').fetchone()[0])
        with patch.dict(os.environ, PUSHOVER_API_TOKEN='test-app', PUSHOVER_USER_KEY='test-user'):
            with patch('urllib.request.urlopen') as request:
                request.return_value.__enter__.return_value.read.return_value = b'{"status":1}'
                watcher.push(payload)
                sent = request.call_args.args[0]
                self.assertEqual(sent.full_url, 'https://api.pushover.net/1/messages.json')
                self.assertIn(b'token=test-app', sent.data)
                request.return_value.__enter__.return_value.read.return_value = b'{"status":0}'
                with self.assertRaises(RuntimeError):
                    watcher.push(payload)

    def test_export_uses_env_not_token_argument(self):
        from pathlib import Path
        output = Path(self.tmp.name) / 'export.json'
        output.write_text(json.dumps(DOC))
        with patch('subprocess.run') as run:
            run.return_value.returncode = 0
            self.assertEqual(watcher.export('20', '100', output), DOC)
            self.assertNotIn('--token', run.call_args.args[0])
            run.return_value.returncode = 1
            with self.assertRaises(RuntimeError):
                watcher.export('20', '100', output)

    def test_invalid_ids(self):
        with patch.dict(os.environ, DISCORD_USER_IDS='abc'):
            with self.assertRaises(ValueError):
                watcher.ids('DISCORD_USER_IDS', True)


if __name__ == '__main__':
    unittest.main()
