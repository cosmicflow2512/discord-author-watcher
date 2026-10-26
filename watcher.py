"""DiscordChatExporter polling bridge. Python standard library only."""
import datetime as dt
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import tempfile
import threading
import urllib.parse
import urllib.request

log = logging.getLogger('watcher')
STOP = threading.Event()


def ids(name, required=False):
    values = {v.strip() for v in os.getenv(name, '').split(',') if v.strip()}
    if (required and not values) or any(not v.isascii() or not v.isdigit() for v in values):
        raise ValueError(f'{name}: kommaseparierte numerische IDs erforderlich')
    return values


def database(path):
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS cursors (channel TEXT PRIMARY KEY, after TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY)')
    return db


def ingest(db, doc, users, channels, guilds):
    channel = str(doc['channel']['id'])
    guild = str(doc['guild']['id'])
    if channels and channel not in channels or guilds and guild not in guilds:
        return 0
    count = 0
    with db:
        for message in doc['messages']:
            mid = str(message['id'])
            if str(message['author']['id']) not in users:
                continue
            if db.execute('SELECT 1 FROM sent WHERE id=?', (mid,)).fetchone():
                continue
            payload = {'author': message['author'].get('nickname') or message['author']['name'],
                       'channel': doc['channel']['name'], 'timestamp': message['timestamp'],
                       'content': message.get('content') or '[Anhang oder Nachricht ohne Text]',
                       'url': f'https://discord.com/channels/{guild if guild != "0" else "@me"}/{channel}/{mid}'}
            cur = db.execute('INSERT OR IGNORE INTO pending VALUES (?,?)', (mid, json.dumps(payload)))
            count += cur.rowcount
    return count


def push(payload):
    text = f"{payload['author']} in #{payload['channel']}\n{payload['timestamp']}\n{payload['content']}"
    data = urllib.parse.urlencode({'token': os.environ['PUSHOVER_API_TOKEN'],
                                  'user': os.environ['PUSHOVER_USER_KEY'],
                                  'title': 'Discord Author Watcher', 'message': text[:1024],
                                  'url': payload['url'], 'url_title': 'Nachricht öffnen'}).encode()
    request = urllib.request.Request('https://api.pushover.net/1/messages.json', data=data)
    with urllib.request.urlopen(request, timeout=30) as response:
        if json.load(response).get('status') != 1:
            raise RuntimeError('Pushover hat die Nachricht abgelehnt')


def deliver(db, sender=push):
    for mid, raw in db.execute('SELECT id,payload FROM pending ORDER BY rowid').fetchall():
        sender(json.loads(raw))
        with db:
            db.execute('INSERT OR IGNORE INTO sent VALUES (?)', (mid,))
            db.execute('DELETE FROM pending WHERE id=?', (mid,))
        log.info('Benachrichtigung bestätigt message_id=%s', mid)


def export(channel, after, output):
    # Token stays in the environment, never in command arguments or captured logs.
    command = [os.getenv('DCE_EXECUTABLE', '/opt/app/DiscordChatExporter.Cli'),
               'export', '-c', channel, '-f', 'Json', '-o', str(output),
               '--after', after, '--before', dt.datetime.now(dt.timezone.utc).isoformat(),
               '--parallel', '1']
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=int(os.getenv('EXPORT_TIMEOUT_SECONDS', '300')), check=False)
    if result.returncode:
        raise RuntimeError(f'DiscordChatExporter exit={result.returncode}; Zugang, Kanalrechte und Netzwerk prüfen')
    with output.open() as f:
        return json.load(f)


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s %(levelname)s %(message)s')
    users = ids('DISCORD_USER_IDS', True)
    channels = ids('DISCORD_CHANNEL_IDS', True)
    guilds = ids('DISCORD_GUILD_IDS')
    for name in ('DISCORD_TOKEN', 'PUSHOVER_API_TOKEN', 'PUSHOVER_USER_KEY'):
        if not os.getenv(name):
            raise ValueError(f'{name} fehlt')
    interval = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))
    lookback = int(os.getenv('INITIAL_LOOKBACK_SECONDS', '0'))
    if interval < 30 or lookback < 0:
        raise ValueError('Intervall mindestens 30 Sekunden; Lookback mindestens 0')
    data = Path(os.getenv('DATA_DIR', '/data'))
    data.mkdir(parents=True, exist_ok=True)
    # Prevent two processes sharing the same SQLite queue.
    import fcntl
    lock = (data / 'watcher.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = database(data / 'watcher.sqlite3')
    initial = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=lookback)).isoformat()
    with db:
        for channel in channels:
            db.execute('INSERT OR IGNORE INTO cursors VALUES (?,?)', (channel, initial))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    log.info('Überwachung gestartet: %d Benutzer, %d Kanäle, Intervall %ds', len(users), len(channels), interval)
    failures = 0
    while not STOP.is_set():
        failed = False
        for channel in sorted(channels):
            if STOP.is_set():
                break
            try:
                after = db.execute('SELECT after FROM cursors WHERE channel=?', (channel,)).fetchone()[0]
                with tempfile.TemporaryDirectory(prefix='daw-') as tmp:
                    doc = export(channel, after, Path(tmp) / 'messages.json')
                if str(doc['channel']['id']) != channel:
                    raise ValueError('Export enthält unerwarteten Kanal')
                with db:
                    count = ingest(db, doc, users, channels, guilds)
                    # Advance only to a received message, never wall-clock time: no gaps on delayed responses.
                    messages = doc['messages']
                    if messages:
                        newest = str(max(int(m['id']) for m in messages))
                        db.execute('UPDATE cursors SET after=? WHERE channel=?', (newest, channel))
                log.info('Kanal %s: %d Nachrichten, %d neue Treffer', channel, len(messages), count)
            except Exception as exc:
                failed = True
                log.error('Kanal %s fehlgeschlagen (%s)', channel, type(exc).__name__)
        try:
            deliver(db)
        except Exception as exc:
            failed = True
            log.error('Pushover fehlgeschlagen (%s); Warteschlange bleibt erhalten', type(exc).__name__)
        failures = min(failures + 1, 4) if failed else 0
        STOP.wait(min(interval * 2 ** failures, 900))
    db.close()
    lock.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.error('Start fehlgeschlagen (%s): Konfiguration und Datenverzeichnis prüfen', type(exc).__name__)
        raise SystemExit(1)
