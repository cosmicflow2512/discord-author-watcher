import datetime as dt
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

import watcher

DOC = {'guild': {'id': '10'}, 'channel': {'id': '20', 'name': 'general'},
       'messages': [{'id': '100', 'author': {'id': '30', 'name': 'Alice'},
                     'timestamp': '2026-10-26T12:00:00Z', 'content': 'Hallo'}]}
ENV = {'DISCORD_TOKEN': 'discord-secret-token', 'DISCORD_USER_IDS': '30',
       'DISCORD_CHANNEL_IDS': '20', 'PUSHOVER_API_TOKEN': 'test-app', 'PUSHOVER_USER_KEY': 'test-user'}


def http_error(code, body):
    return urllib.error.HTTPError(watcher.PUSHOVER_URL, code, 'err', {}, io.BytesIO(json.dumps(body).encode()))


def fake_dce(directory, script):
    """Erzeugt ein ausführbares Ersatzprogramm für DiscordChatExporter."""
    path = Path(directory) / 'fake-dce'
    path.write_text(f'#!{sys.executable}\n{script}')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = self.tmp.name + '/state.db'
        self.db = watcher.database(self.path)
        watcher.STOP.clear()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------ Filter / Queue

    def test_filters(self):
        for users, channels, guilds in [({'99'}, set(), set()), ({'30'}, {'99'}, set()),
                                         ({'30'}, set(), {'99'})]:
            self.assertEqual(watcher.ingest(self.db, DOC, users, channels, guilds), 0)
        self.assertEqual(watcher.ingest(self.db, DOC, {'30'}, {'20'}, {'10'}), 1)

    def test_restart_dedup_and_link(self):
        watcher.ingest(self.db, DOC, {'30'}, set(), set())
        watcher.ingest(self.db, DOC, {'30'}, set(), set())
        delivered = []
        self.assertTrue(watcher.deliver(self.db, delivered.append))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0]['url'], 'https://discord.com/channels/10/20/100')
        self.db.close()
        self.db = watcher.database(self.path)
        self.assertEqual(watcher.ingest(self.db, DOC, {'30'}, set(), set()), 0)
        watcher.deliver(self.db, delivered.append)
        self.assertEqual(len(delivered), 1)

    def test_temporary_failure_keeps_queue_across_restart(self):
        watcher.ingest(self.db, DOC, {'30'}, set(), set())

        def fail(_):
            raise watcher.PushoverError('temporary', 'HTTP 503')
        self.assertFalse(watcher.deliver(self.db, fail))
        self.db.close()
        self.db = watcher.database(self.path)
        delivered = []
        self.assertTrue(watcher.deliver(self.db, delivered.append))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM pending').fetchone()[0], 0)

    def test_rejected_message_does_not_block_queue(self):
        doc = json.loads(json.dumps(DOC))
        doc['messages'].append({'id': '101', 'author': {'id': '30', 'name': 'Alice'},
                                'timestamp': '', 'content': 'zweite'})
        watcher.ingest(self.db, doc, {'30'}, set(), set())
        delivered = []

        def sender(payload):
            if payload['content'] == 'Hallo':
                raise watcher.PushoverError('message', 'message cannot be blank')
            delivered.append(payload)
        self.assertTrue(watcher.deliver(self.db, sender))
        self.assertEqual([p['content'] for p in delivered], ['zweite'])
        self.assertEqual(self.db.execute('SELECT id FROM rejected').fetchall(), [('100',)])
        # Abgelehnte Nachricht wird nicht erneut eingereiht.
        self.assertEqual(watcher.ingest(self.db, DOC, {'30'}, set(), set()), 0)

    def test_prune_keeps_recent_ids(self):
        old = str(watcher.snowflake_at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)))
        new = str(watcher.snowflake_at(dt.datetime.now(dt.timezone.utc)))
        with self.db:
            self.db.executemany('INSERT INTO sent VALUES (?)', [(old,), (new,)])
        self.assertEqual(watcher.prune(self.db, 30), 1)
        self.assertEqual(self.db.execute('SELECT id FROM sent').fetchall(), [(new,)])

    def test_prune_never_passes_a_cursor(self):
        old = watcher.snowflake_at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40))
        with self.db:
            self.db.executemany('INSERT INTO sent VALUES (?)', [(str(old - 10),), (str(old),)])
            self.db.execute('INSERT INTO cursors VALUES (?,?)', ('20', str(old - 10)))
        # Kanal 20 steht noch vor `old`: diese ID kann noch exportiert werden und bleibt.
        self.assertEqual(watcher.prune(self.db, 30), 1)
        self.assertEqual(self.db.execute('SELECT id FROM sent').fetchall(), [(str(old),)])

    # ------------------------------------------------------------ Pushover

    def test_pushover_request_limits_and_success(self):
        payload = {'author': 'A', 'channel': 'c', 'timestamp': 't', 'content': 'x' * 5000,
                   'url': 'https://discord.com/channels/1/2/3'}
        with patch.dict(os.environ, ENV), patch('urllib.request.urlopen') as request:
            request.return_value.__enter__.return_value.read.return_value = b'{"status":1}'
            watcher.push(payload)
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, 'https://api.pushover.net/1/messages.json')
            fields = dict(p.split('=', 1) for p in sent.data.decode().split('&'))
            self.assertEqual(fields['token'], 'test-app')
            self.assertLessEqual(len(watcher.urllib.parse.unquote_plus(fields['message'])), 1024)

    def test_pushover_error_classification(self):
        cases = [(http_error(400, {'token': 'invalid', 'errors': ['application token is invalid']}), 'config'),
                 (http_error(400, {'user': 'invalid', 'errors': ['user identifier is invalid']}), 'config'),
                 (http_error(400, {'errors': ['message cannot be blank']}), 'message'),
                 (http_error(429, {}), 'quota'),
                 (http_error(503, {}), 'temporary'),
                 (urllib.error.URLError('dns'), 'temporary')]
        payload = {'author': 'A', 'channel': 'c', 'timestamp': 't', 'content': 'x', 'url': 'u'}
        with patch.dict(os.environ, ENV):
            for error, kind in cases:
                with patch('urllib.request.urlopen', side_effect=error):
                    with self.assertRaises(watcher.PushoverError) as ctx:
                        watcher.push(payload)
                    self.assertEqual(ctx.exception.kind, kind)
                    self.assertNotIn('test-app', str(ctx.exception))

    # ------------------------------------------------------------ Export

    def test_export_passes_token_only_via_env(self):
        script = ('import json, os, sys\n'
                  'a = sys.argv\n'
                  'out = a[a.index("-o") + 1]\n'
                  'assert "--token" not in a and "-t" not in a\n'
                  'assert os.environ["DISCORD_TOKEN"] == "discord-secret-token"\n'
                  'assert "PUSHOVER_API_TOKEN" not in os.environ\n'
                  f'json.dump({DOC!r}, open(out, "w"))\n')
        work = Path(self.tmp.name) / 'w'
        work.mkdir()
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            self.assertEqual(watcher.export('20', '100', work), DOC)

    def test_export_error_is_redacted(self):
        script = 'import sys\nsys.stderr.write("Token discord-secret-token is invalid\\n")\nsys.exit(2)\n'
        work = Path(self.tmp.name) / 'w'
        work.mkdir()
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            with self.assertRaises(RuntimeError) as ctx:
                watcher.export('20', '100', work)
        self.assertIn('exit=2', str(ctx.exception))
        self.assertNotIn('discord-secret-token', str(ctx.exception))

    def test_export_rejects_foreign_channel(self):
        script = ('import json, sys\na = sys.argv\n'
                  f'json.dump({DOC!r}, open(a[a.index("-o") + 1], "w"))\n')
        work = Path(self.tmp.name) / 'w'
        work.mkdir()
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            with self.assertRaises(ValueError):
                watcher.export('21', '100', work)

    def test_export_timeout_kills_process(self):
        script = 'import time\ntime.sleep(30)\n'
        with patch.dict(os.environ, {**ENV, 'DCE_EXECUTABLE': fake_dce(self.tmp.name, script)}):
            start = time.monotonic()
            with self.assertRaises(TimeoutError):
                watcher.run_dce(['export'], 1, Path(self.tmp.name) / 'err.log')
            self.assertLess(time.monotonic() - start, 10)

    # ------------------------------------------------------------ Konfiguration / Health

    def test_config_validation(self):
        with patch.dict(os.environ, ENV):
            cfg = watcher.load_config()
            self.assertEqual(cfg['interval'], 60)
        for key, value in [('DISCORD_USER_IDS', 'abc'), ('DISCORD_CHANNEL_IDS', ''),
                           ('POLL_INTERVAL_SECONDS', '5'), ('POLL_INTERVAL_SECONDS', 'x'),
                           ('DISCORD_TOKEN', ''), ('PUSHOVER_USER_KEY', ' key ')]:
            with patch.dict(os.environ, {**ENV, key: value}):
                with self.assertRaises(watcher.ConfigError, msg=key) as ctx:
                    watcher.load_config()
                self.assertNotIn('discord-secret-token', str(ctx.exception))

    def test_healthcheck(self):
        data = Path(self.tmp.name)
        with patch.dict(os.environ, {'DATA_DIR': str(data)}):
            self.assertEqual(watcher.healthcheck(), 1)
            watcher.write_heartbeat(data, time.time() + 60)
            self.assertEqual(watcher.healthcheck(), 0)
            watcher.write_heartbeat(data, time.time() - 1)
            self.assertEqual(watcher.healthcheck(), 1)


if __name__ == '__main__':
    unittest.main()
