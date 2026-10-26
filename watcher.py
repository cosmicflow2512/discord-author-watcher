"""DiscordChatExporter polling bridge. Python standard library only."""
import datetime as dt
import fcntl
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger('watcher')
STOP = threading.Event()

PUSHOVER_URL = 'https://api.pushover.net/1/messages.json'
DISCORD_EPOCH_MS = 1420070400000
# Pushover-Limits laut https://pushover.net/api
LIMIT_MESSAGE, LIMIT_TITLE, LIMIT_URL, LIMIT_URL_TITLE = 1024, 250, 512, 100
# Nur diese Variablen bekommt DiscordChatExporter; Pushover-Zugangsdaten bleiben im Elternprozess.
CHILD_ENV = ('PATH', 'HOME', 'TZ', 'LANG', 'LC_ALL', 'DISCORD_TOKEN',
             'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
             'HTTPS_PROXY', 'HTTP_PROXY', 'NO_PROXY', 'https_proxy', 'http_proxy', 'no_proxy')


class ConfigError(Exception):
    """Fehler, deren Text keine Geheimnisse enthält und ausgegeben werden darf."""


class PushoverError(Exception):
    def __init__(self, kind, detail=''):
        super().__init__(f'{kind}: {detail}' if detail else kind)
        self.kind = kind  # 'config' | 'quota' | 'message' | 'temporary'
        self.detail = detail


# ---------------------------------------------------------------- Konfiguration

def ids(name, required=False):
    values = {v.strip() for v in os.getenv(name, '').split(',') if v.strip()}
    if required and not values:
        raise ConfigError(f'{name} fehlt (kommaseparierte numerische IDs)')
    if any(not (v.isascii() and v.isdigit()) or len(v) > 20 for v in values):
        raise ConfigError(f'{name}: nur kommaseparierte numerische IDs erlaubt')
    return values


def integer(name, default, minimum, maximum):
    raw = os.getenv(name, '').strip() or str(default)
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f'{name} muss eine ganze Zahl sein') from None
    if not minimum <= value <= maximum:
        raise ConfigError(f'{name} muss zwischen {minimum} und {maximum} liegen')
    return value


def load_config():
    cfg = {
        'users': ids('DISCORD_USER_IDS', True),
        'channels': ids('DISCORD_CHANNEL_IDS', True),
        'guilds': ids('DISCORD_GUILD_IDS'),
        'interval': integer('POLL_INTERVAL_SECONDS', 60, 30, 86400),
        'lookback': integer('INITIAL_LOOKBACK_SECONDS', 0, 0, 30 * 86400),
        'timeout': integer('EXPORT_TIMEOUT_SECONDS', 300, 30, 3600),
        'retention_days': integer('SENT_RETENTION_DAYS', 30, 1, 3650),
        'data': Path(os.getenv('DATA_DIR', '/data')),
    }
    for name in ('DISCORD_TOKEN', 'PUSHOVER_API_TOKEN', 'PUSHOVER_USER_KEY'):
        value = os.getenv(name, '')
        if not value.strip():
            raise ConfigError(f'{name} fehlt')
        if value != value.strip() or any(c in value for c in '\r\n'):
            raise ConfigError(f'{name} enthält Leerzeichen oder Zeilenumbrüche am Rand')
    return cfg


# ---------------------------------------------------------------- Hilfsfunktionen

def snowflake_ms(snowflake):
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def snowflake_at(when):
    return max(int(when.timestamp() * 1000) - DISCORD_EPOCH_MS, 0) << 22


def redact(text, secrets):
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, '***')
    return text


def clip(text, limit):
    text = str(text)
    return text if len(text) <= limit else text[:limit - 1] + '…'


# ---------------------------------------------------------------- Datenbank

def database(path):
    db = sqlite3.connect(path, timeout=30)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=NORMAL')
    db.execute('CREATE TABLE IF NOT EXISTS cursors (channel TEXT PRIMARY KEY, after TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS sent (id TEXT PRIMARY KEY)')
    db.execute('CREATE TABLE IF NOT EXISTS rejected (id TEXT PRIMARY KEY, reason TEXT NOT NULL)')
    db.commit()
    return db


def ingest(db, doc, users, channels, guilds):
    channel = str(doc['channel']['id'])
    guild = str(doc['guild']['id'])
    if (channels and channel not in channels) or (guilds and guild not in guilds):
        return 0
    count = 0
    with db:
        for message in doc['messages']:
            mid = str(message['id'])
            author = message.get('author') or {}
            if str(author.get('id')) not in users:
                continue
            if db.execute('SELECT 1 FROM sent WHERE id=? UNION ALL SELECT 1 FROM rejected WHERE id=?',
                          (mid, mid)).fetchone():
                continue
            payload = {'author': author.get('nickname') or author.get('name') or author.get('id'),
                       'channel': doc['channel'].get('name') or channel,
                       'timestamp': message.get('timestamp', ''),
                       'content': message.get('content') or '[Anhang oder Nachricht ohne Text]',
                       'url': f'https://discord.com/channels/{guild if guild != "0" else "@me"}/{channel}/{mid}'}
            cur = db.execute('INSERT OR IGNORE INTO pending VALUES (?,?)', (mid, json.dumps(payload)))
            count += cur.rowcount
    return count


def prune(db, retention_days):
    """sent/rejected nur so lange halten, wie ein Export die ID noch liefern kann.

    Entfernt wird nur, was älter als die Aufbewahrung UND nicht neuer als jeder Kanal-Cursor ist;
    Exporte liefern nur Nachrichten nach dem Cursor, solche IDs kommen also nie wieder.
    """
    cutoff = snowflake_at(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=retention_days))
    for (after,) in db.execute('SELECT after FROM cursors'):
        if after.isdigit():
            cutoff = min(cutoff, int(after) + 1)
        else:
            try:
                cutoff = min(cutoff, snowflake_at(dt.datetime.fromisoformat(after)))
            except ValueError:
                return 0
    with db:
        removed = sum(db.execute(f'DELETE FROM {table} WHERE CAST(id AS INTEGER) < ?', (cutoff,)).rowcount
                      for table in ('sent', 'rejected'))
    if removed:
        log.info('%d alte Duplikateinträge entfernt', removed)
    return removed


# ---------------------------------------------------------------- Pushover

def push(payload):
    text = f"{payload['author']} in #{payload['channel']}\n{payload['timestamp']}\n{payload['content']}"
    fields = {'token': os.environ['PUSHOVER_API_TOKEN'], 'user': os.environ['PUSHOVER_USER_KEY'],
              'title': clip('Discord Author Watcher', LIMIT_TITLE),
              'message': clip(text, LIMIT_MESSAGE),
              'url_title': clip('Nachricht öffnen', LIMIT_URL_TITLE)}
    if len(payload['url']) <= LIMIT_URL:
        fields['url'] = payload['url']
    request = urllib.request.Request(PUSHOVER_URL, data=urllib.parse.urlencode(fields).encode(),
                                     headers={'User-Agent': 'discord-author-watcher'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as err:
        try:
            body = json.loads(err.read() or b'{}')
        except ValueError:
            body = {}
        detail = '; '.join(str(e) for e in body.get('errors', []))[:300]
        if err.code == 429:
            raise PushoverError('quota', 'Monatslimit erreicht') from None
        if 400 <= err.code < 500:
            # Ungültiges Token/Empfänger betrifft alle Nachrichten -> Warteschlange behalten.
            kind = 'config' if ('token' in body or 'user' in body) else 'message'
            raise PushoverError(kind, detail or f'HTTP {err.code}') from None
        raise PushoverError('temporary', f'HTTP {err.code}') from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        raise PushoverError('temporary', type(err).__name__) from None
    if body.get('status') != 1:
        raise PushoverError('temporary', 'unerwartete Antwort')


def deliver(db, sender=push):
    """Arbeitet die Warteschlange ab. Gibt False zurück, wenn ein Wiederholungsversuch nötig ist."""
    for mid, raw in db.execute('SELECT id,payload FROM pending ORDER BY rowid').fetchall():
        try:
            sender(json.loads(raw))
        except PushoverError as err:
            if err.kind == 'message':
                # Einzelne unzustellbare Nachricht darf die Warteschlange nicht dauerhaft blockieren.
                with db:
                    db.execute('INSERT OR IGNORE INTO rejected VALUES (?,?)', (mid, err.detail))
                    db.execute('DELETE FROM pending WHERE id=?', (mid,))
                log.error('Pushover lehnt message_id=%s dauerhaft ab: %s', mid, err.detail)
                continue
            log.error('Pushover-Zustellung pausiert (%s); Warteschlange bleibt erhalten', err)
            return False
        with db:
            db.execute('INSERT OR IGNORE INTO sent VALUES (?)', (mid,))
            db.execute('DELETE FROM pending WHERE id=?', (mid,))
        log.info('Benachrichtigung bestätigt message_id=%s', mid)
    return True


# ---------------------------------------------------------------- DiscordChatExporter

def child_env():
    env = {k: os.environ[k] for k in CHILD_ENV if k in os.environ}
    env['FUCK_RUSSIA'] = 'true'  # DCE-Option: Konsolenbanner unterdrücken
    return env


def run_dce(args, timeout, stderr_path):
    command = [os.getenv('DCE_EXECUTABLE', '/opt/app/DiscordChatExporter.Cli'), *args]
    with open(stderr_path, 'wb') as stderr:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=stderr, env=child_env(), start_new_session=True)
        deadline = time.monotonic() + timeout
        while True:
            try:
                return proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                if STOP.is_set() or time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    raise TimeoutError('Export abgebrochen' if STOP.is_set() else 'Export-Timeout') from None


def export(channel, after, workdir, timeout=300):
    workdir = Path(workdir)
    output, errors = workdir / 'messages.json', workdir / 'stderr.log'
    # Token nur über die Umgebung, nie als Argument (sichtbar in ps/docker top).
    args = ['export', '-c', channel, '-f', 'Json', '-o', str(output), '--after', after,
            '--before', dt.datetime.now(dt.timezone.utc).isoformat(), '--parallel', '1']
    code = run_dce(args, timeout, errors)
    if code:
        tail = errors.read_text(errors='replace').strip().splitlines()[-1:] if errors.exists() else []
        hint = redact(tail[0], [os.getenv('DISCORD_TOKEN', '')])[:300] if tail else 'keine Ausgabe'
        raise RuntimeError(f'DiscordChatExporter exit={code}: {hint}')
    with output.open(encoding='utf-8') as f:
        doc = json.load(f)
    if str(doc['channel']['id']) != channel:
        raise ValueError('Export enthält unerwarteten Kanal')
    return doc


# ---------------------------------------------------------------- Health

def write_heartbeat(data, deadline):
    tmp = data / 'heartbeat.tmp'
    tmp.write_text(str(int(deadline)))
    tmp.replace(data / 'heartbeat')


def healthcheck():
    path = Path(os.getenv('DATA_DIR', '/data')) / 'heartbeat'
    try:
        deadline = int(path.read_text())
    except (OSError, ValueError):
        print('kein Heartbeat')
        return 1
    if time.time() > deadline:
        print('Heartbeat überfällig')
        return 1
    return 0


def self_test():
    """Prüft das fertige Image: Laufzeitbibliotheken, CA-Zertifikate, Schreibrechte, DCE-Start."""
    data = Path(os.getenv('DATA_DIR', '/data'))
    paths = ssl.get_default_verify_paths()
    has_ca = ssl.create_default_context().cert_store_stats().get('x509_ca') or \
        (paths.capath and any(Path(paths.capath).glob('*.0')))
    if not has_ca:
        raise ConfigError('Selbsttest: keine CA-Zertifikate gefunden')
    with tempfile.TemporaryDirectory(dir=data) as tmp:
        database(Path(tmp) / 'test.sqlite3').close()
        if run_dce(['--help'], 60, Path(tmp) / 'stderr.log') != 0:
            raise ConfigError('Selbsttest: DiscordChatExporter startet nicht')
    print(f'ok: python {sys.version.split()[0]}, {ssl.OPENSSL_VERSION}, sqlite {sqlite3.sqlite_version}, '
          f'uid={os.getuid()} gid={os.getgid()}')
    return 0


# ---------------------------------------------------------------- Hauptschleife

def poll_channel(db, cfg, channel):
    after = db.execute('SELECT after FROM cursors WHERE channel=?', (channel,)).fetchone()[0]
    with tempfile.TemporaryDirectory(prefix='daw-') as tmp:
        doc = export(channel, after, tmp, cfg['timeout'])
    messages = doc['messages']
    with db:
        count = ingest(db, doc, cfg['users'], cfg['channels'], cfg['guilds'])
        # Cursor nur auf empfangene Nachricht setzen, nie auf Uhrzeit: keine Lücken bei Verzögerung.
        if messages:
            newest = max(int(m['id']) for m in messages)
            if not after.isdigit() or newest > int(after):
                db.execute('UPDATE cursors SET after=? WHERE channel=?', (str(newest), channel))
    log.info('Kanal %s: %d Nachrichten, %d neue Treffer', channel, len(messages), count)


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO').upper(),
                        format='%(asctime)s %(levelname)s %(message)s')
    cfg = load_config()
    data = cfg['data']
    data.mkdir(parents=True, exist_ok=True)
    if os.getuid() == 0:
        log.warning('Läuft als root; PUID/PGID im Container setzen')
    lock = (data / 'watcher.lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ConfigError(f'{data} wird bereits von einer anderen Instanz verwendet') from None
    db = database(data / 'watcher.sqlite3')
    initial = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=cfg['lookback'])).isoformat()
    with db:
        for channel in cfg['channels']:
            db.execute('INSERT OR IGNORE INTO cursors VALUES (?,?)', (channel, initial))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    cycle_budget = len(cfg['channels']) * cfg['timeout'] + 120
    write_heartbeat(data, time.time() + cycle_budget)
    log.info('Überwachung gestartet: %d Benutzer, %d Kanäle, Intervall %ds',
             len(cfg['users']), len(cfg['channels']), cfg['interval'])
    failures, last_prune = 0, 0.0
    while not STOP.is_set():
        failed = False
        for channel in sorted(cfg['channels']):
            if STOP.is_set():
                break
            try:
                poll_channel(db, cfg, channel)
            except Exception as exc:
                failed = True
                log.error('Kanal %s fehlgeschlagen: %s', channel,
                          exc if isinstance(exc, (RuntimeError, TimeoutError)) else type(exc).__name__)
        try:
            failed |= not deliver(db)
        except Exception as exc:
            failed = True
            log.error('Zustellung fehlgeschlagen (%s); Warteschlange bleibt erhalten', type(exc).__name__)
        if time.time() - last_prune > 86400:
            prune(db, cfg['retention_days'])
            last_prune = time.time()
        failures = min(failures + 1, 4) if failed else 0
        wait = min(cfg['interval'] * 2 ** failures, 900)
        write_heartbeat(data, time.time() + wait + cycle_budget)
        STOP.wait(wait)
    log.info('Beendet')
    db.close()
    lock.close()


def cli(argv):
    if argv[1:2] == ['--healthcheck']:
        return healthcheck()
    try:
        if argv[1:2] == ['--self-test']:
            return self_test()
        main()
        return 0
    except ConfigError as exc:
        logging.error('Konfigurationsfehler: %s', exc)
    except Exception as exc:
        logging.error('Start fehlgeschlagen (%s): Konfiguration und Datenverzeichnis prüfen', type(exc).__name__)
    return 1


if __name__ == '__main__':
    raise SystemExit(cli(sys.argv))
