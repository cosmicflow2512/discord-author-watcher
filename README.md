# Discord Author Watcher

Schlanker Hintergrunddienst für Unraid: DiscordChatExporter fragt ausgewählte Kanäle ab; Nachrichten ausgewählter User-IDs werden an Pushover geschickt. Kein Dashboard, keine offenen Ports, kein zusätzlicher Discord-Client erforderlich.

## Wichtige Einschränkung

Der Zugriff auf fremde Server ohne Bot erfolgt mit einem **Discord-Benutzer-Token**. Automatisierte Benutzerkonten verstoßen laut [DiscordChatExporter-Dokumentation](https://github.com/Tyrrrz/DiscordChatExporter/blob/prime/.docs/Token-and-IDs.md) gegen Discords Nutzungsbedingungen; Kontosperrungen sind möglich. Dieses Projekt ist keine von Discord unterstützte Integration. Alternative: ein autorisierter Bot im jeweiligen Server. Es werden ausschließlich Nachrichten erfasst, auf die das verwendete Konto Zugriff hat.

Es handelt sich um Polling, nicht Echtzeit: standardmäßig 60 Sekunden plus Exportdauer. Bearbeitungen und Löschungen bereits erfasster Nachrichten lösen keine neue Benachrichtigung aus. Gelöschte Nachrichten zwischen zwei Abfragen können nicht erfasst werden. Threads müssen mit ihrer eigenen Channel-ID eingetragen werden; neue Threads werden nicht automatisch entdeckt.

## Installation mit Docker Compose

```sh
git clone https://github.com/cosmicflow2512/discord-author-watcher.git
cd discord-author-watcher
cp .env.example .env
chmod 600 .env
# .env lokal bearbeiten, Zugangsdaten niemals veröffentlichen
mkdir -p /mnt/cache/appdata/discord-author-watcher
docker compose up -d --build
docker compose logs -f
```

Zum Bezug eines bereits veröffentlichten Images: `docker compose pull && docker compose up -d --no-build`. Das GHCR-Image entsteht erst nach erfolgreichem GitHub-Actions-Lauf. Für anonymen Download muss das GHCR-Paket öffentlich sein (Package settings → Change visibility → Public).

## Unraid

1. Nach Veröffentlichung das Template herunterladen:

```sh
mkdir -p /boot/config/plugins/dockerMan/templates-user
curl --fail --location \
  https://raw.githubusercontent.com/cosmicflow2512/discord-author-watcher/main/templates/my-discord-author-watcher.xml \
  -o /boot/config/plugins/dockerMan/templates-user/my-discord-author-watcher.xml
mkdir -p /mnt/cache/appdata/discord-author-watcher
```

2. In **Docker → Add Container** das Template `discord-author-watcher` auswählen.
3. Discord-Token, Pushover-App-Token und Pushover-User-Key in die maskierten Felder eintragen. User- und Channel-IDs ergänzen.
4. Container starten und Unraid-Autostart aktivieren. Keine Ports und kein Docker-Socket nötig.

Maskierte Felder verstecken nur die Anzeige. Unraid speichert Variablen im Template; Docker-Administratoren können Container-Umgebungsvariablen lesen. Template und Appdata nicht öffentlich sichern. Keine Zugangsdaten in Issues oder Logs posten.

## Konfiguration

Alle Einstellungen erfolgen über Umgebungsvariablen; Compose lädt `.env`. Das Programm selbst lädt keine Datei.

| Variable | Bedeutung |
| --- | --- |
| `DISCORD_TOKEN` | Discord-Zugangstoken; nur lokal eintragen |
| `DISCORD_USER_IDS` | Pflicht: überwachte User-IDs, durch Kommas getrennt |
| `DISCORD_CHANNEL_IDS` | Pflicht in dieser Version: abzufragende Channel-/Thread-IDs, durch Kommas getrennt |
| `DISCORD_GUILD_IDS` | Optional: zusätzlicher Server-ID-Filter |
| `PUSHOVER_API_TOKEN` | App-Token aus deinem Pushover-Konto |
| `PUSHOVER_USER_KEY` | Pushover-Empfänger-Key |
| `POLL_INTERVAL_SECONDS` | Standard 60, Minimum 30 |
| `INITIAL_LOOKBACK_SECONDS` | Standard 0: bei erstmaligem Start nur neue Nachrichten; z. B. 600 für letzte zehn Minuten |
| `EXPORT_TIMEOUT_SECONDS` | Maximale Dauer je Kanalexport, Standard 300 |
| `TZ` | Standard `Europe/Berlin`; Exportzeitstempel können UTC enthalten |
| `LOG_LEVEL` | Standard `INFO`, alternativ `DEBUG`, `WARNING`, `ERROR` |
| `DATA_DIR` | Intern standardmäßig `/data` |

Beispiel: `DISCORD_USER_IDS=123456789012345678,234567890123456789`. Es wird die **Autor-ID**, nicht die Anzeigename-Zeichenfolge geprüft. Discord-Entwicklermodus aktivieren und „Benutzer-ID kopieren“ bzw. „Kanal-ID kopieren“ verwenden. Serverfilter und Channelfilter gelten zusätzlich zum Benutzerfilter. Eine automatische Abfrage sämtlicher Server ist bewusst nicht implementiert.

## Zustellung und Wiederanlauf

SQLite speichert pro Kanal einen Fortschritt und eine dauerhafte Versandwarteschlange. Nach einem Exportfehler bleibt der Fortschritt erhalten. Bei Fehlern verlängert sich die Pause bis maximal 15 Minuten. Fehlgeschlagene Pushover-Zustellungen werden erneut versucht, auch nach Neustarts. Einzelne kaputte Kanäle blockieren andere Exporte nicht.

Normale Wiederholungen und Neustarts erzeugen keine Duplikate. **Genau-einmal-Zustellung ist nicht garantiert:** Wenn Pushover eine Nachricht annimmt, die Antwort aber verloren geht oder der Prozess vor dem Speichern der Bestätigung abstürzt, kann eine Wiederholung entstehen. Pushover bietet hierfür keinen Idempotenzschlüssel. Bestätigte IDs werden dauerhaft gespeichert; Appdata wächst mit der Anzahl zugestellter Nachrichten. Löschen der Datenbank setzt Fortschritt und Duplikatschutz zurück.

Nachrichten enthalten Autor, Kanal, Exportzeitstempel, gekürzten Text und Discord-Link. Medien werden nicht heruntergeladen. Logausgaben enthalten Kanal-/Nachrichten-IDs und Fehlerklassen, keine Tokens oder Nachrichteninhalte. Ein dauerhaft ungültiger Token benötigt manuelle Korrektur; Wiederholungen reparieren keine Zugangsdaten.

## Entwicklung und Prüfung

Python 3.12+, keine Python-Pakete erforderlich:

```sh
python3 -m unittest discover -s tests -v
docker build -t discord-author-watcher:local .
docker run --rm --entrypoint /opt/app/DiscordChatExporter.Cli discord-author-watcher:local export --help
```

Die Tests prüfen Filter, persistente Duplikaterkennung, Wiederholung nach Versandfehlern, Pushover-Request und Exportaufruf. Sie ersetzen keinen Live-Nachweis. Zum echten Empfangstest eine neue Nachricht eines konfigurierten Benutzers im konfigurierten Kanal senden lassen, `neue Treffer` im Log und die Pushover-Nachricht inklusive Link prüfen; anschließend neu starten und auf doppelte Zustellung prüfen. Ohne echte Zugangsdaten wird kein erfolgreicher Discord-/Pushover-PoC behauptet.

## Builds und Lizenz

GitHub Actions testet Python und baut Linux-amd64-/arm64-Images. Pushes auf `main` veröffentlichen `latest` und einen SHA-Tag; `v*`-Tags veröffentlichen entsprechende Versionstags. Pull Requests bauen ohne Veröffentlichung. Grundlage ist das versionierte MIT-lizenzierte Image `tyrrrz/discordchatexporter:2.48`. Dieses Projekt: MIT, siehe LICENSE.
