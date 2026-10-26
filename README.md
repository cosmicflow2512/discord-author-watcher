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

Bestehende Installation aktualisieren: Template mit dem Befehl oben neu laden, Container in Unraid über **Edit → Apply** neu erstellen. Nur so übernimmt Unraid die neuen Felder (`PUID`, `PGID`, `SENT_RETENTION_DAYS`) und die Sicherheitsoptionen unter *Extra Parameters*.

Maskierte Felder verstecken nur die Anzeige. Unraid speichert Variablen im Template; Docker-Administratoren können Container-Umgebungsvariablen lesen. Template und Appdata nicht öffentlich sichern. Keine Zugangsdaten in Issues oder Logs posten.

## Sicherheitsmodell

- Der Container startet als root, übernimmt `/data` für `PUID:PGID` (Standard `99:100`, Unraid `nobody:users`), setzt `/data` auf `0700` und wechselt per `su-exec` auf diesen Benutzer. Watcher und DiscordChatExporter laufen danach ohne root.
- Template und Compose setzen `no-new-privileges`, entfernen alle Capabilities und geben nur die für den Benutzerwechsel nötigen zurück (`CHOWN`, `SETUID`, `SETGID`, `DAC_OVERRIDE`, `FOWNER`). Dazu Speicher- und Prozesslimit.
- Der Discord-Token geht nur per Umgebungsvariable an DiscordChatExporter, nie als Argument (sonst sichtbar in `ps`/`docker top`). Pushover-Zugangsdaten werden an den Exporter gar nicht weitergereicht.
- Fehlerausgaben des Exporters werden auf eine Zeile gekürzt und der Token darin geschwärzt. Nachrichteninhalte erscheinen nie im Log.
- Python kommt per `apk` aus demselben Alpine wie das Basisimage. Die CI startet das fertige Image vor der Veröffentlichung mit genau diesen Einschränkungen (`--self-test`) und führt die Tests mit dem Python im Image aus.

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
| `EXPORT_TIMEOUT_SECONDS` | Maximale Dauer je Kanalexport, Standard 300, erlaubt 30–3600 |
| `SENT_RETENTION_DAYS` | Standard 30: so lange bleiben zugestellte IDs für den Duplikatschutz gespeichert |
| `PUID` / `PGID` | Standard `99` / `100`: Benutzer und Gruppe, unter denen der Dienst läuft |
| `TZ` | Standard `Europe/Berlin`; Exportzeitstempel können UTC enthalten |
| `LOG_LEVEL` | Standard `INFO`, alternativ `DEBUG`, `WARNING`, `ERROR` |
| `DATA_DIR` | Intern standardmäßig `/data` |

Ungültige Werte (keine Zahl, außerhalb des Bereichs, Leerzeichen am Rand von Tokens) beenden den Start mit einer konkreten Meldung `Konfigurationsfehler: …` im Log.

Beispiel: `DISCORD_USER_IDS=123456789012345678,234567890123456789`. Es wird die **Autor-ID**, nicht die Anzeigename-Zeichenfolge geprüft. Discord-Entwicklermodus aktivieren und „Benutzer-ID kopieren“ bzw. „Kanal-ID kopieren“ verwenden. Serverfilter und Channelfilter gelten zusätzlich zum Benutzerfilter. Eine automatische Abfrage sämtlicher Server ist bewusst nicht implementiert.

## Zustellung und Wiederanlauf

SQLite speichert pro Kanal einen Fortschritt und eine dauerhafte Versandwarteschlange. Nach einem Exportfehler bleibt der Fortschritt erhalten. Bei Fehlern verlängert sich die Pause bis maximal 15 Minuten. Fehlgeschlagene Pushover-Zustellungen werden erneut versucht, auch nach Neustarts. Einzelne kaputte Kanäle blockieren andere Exporte nicht. `docker stop` bricht einen laufenden Export sofort ab, statt auf das Export-Timeout zu warten.

Pushover-Fehler werden nach der [API-Dokumentation](https://pushover.net/api) unterschieden:

| Antwort | Verhalten |
| --- | --- |
| 4xx mit ungültigem Token oder User-Key | Warteschlange bleibt, Zustellung pausiert, Meldung im Log |
| 4xx wegen der Nachricht selbst | nur diese Nachricht wird verworfen (Tabelle `rejected`), der Rest läuft weiter |
| 429 Monatslimit | Warteschlange bleibt, Wiederholung mit Backoff |
| 5xx, Timeout, Netzwerk | Warteschlange bleibt, Wiederholung mit Backoff |

Normale Wiederholungen und Neustarts erzeugen keine Duplikate. **Genau-einmal-Zustellung ist nicht garantiert:** Wenn Pushover eine Nachricht annimmt, die Antwort aber verloren geht oder der Prozess vor dem Speichern der Bestätigung abstürzt, kann eine Wiederholung entstehen. Pushover bietet hierfür keinen Idempotenzschlüssel. Bestätigte IDs werden einmal täglich bereinigt: Entfernt wird nur, was älter als `SENT_RETENTION_DAYS` ist und hinter dem Fortschritt aller Kanäle liegt, also nie wieder exportiert werden kann. Löschen der Datenbank setzt Fortschritt und Duplikatschutz zurück.

Nachrichten enthalten Autor, Kanal, Exportzeitstempel, gekürzten Text und Discord-Link. Medien werden nicht heruntergeladen. Logausgaben enthalten Kanal-/Nachrichten-IDs und Fehlerklassen, keine Tokens oder Nachrichteninhalte. Ein dauerhaft ungültiger Token benötigt manuelle Korrektur; Wiederholungen reparieren keine Zugangsdaten.

## Healthcheck

Der Dienst schreibt nach jedem Durchlauf einen Heartbeat nach `/data/heartbeat` mit dem spätesten erwarteten nächsten Lebenszeichen. Der Docker-`HEALTHCHECK` (`watcher.py --healthcheck`) meldet `unhealthy`, wenn diese Frist überschritten ist, also bei hängender Schleife. Unraid zeigt den Status in der Docker-Übersicht.

## Entwicklung und Prüfung

Python 3.12+, keine Python-Pakete erforderlich:

```sh
python3 -m unittest discover -s tests -v
docker build -t discord-author-watcher:local .
docker run --rm -v "$(mktemp -d):/data" discord-author-watcher:local --self-test
```

Die Tests prüfen Filter, persistente Duplikaterkennung, Wiederholung nach Versandfehlern, Pushover-Fehlerklassen und Längenlimits, Token-Übergabe nur per Umgebung, Schwärzung von Fehlerausgaben, Export-Timeout, Konfigurationsprüfung, Bereinigung und Healthcheck. `--self-test` prüft im fertigen Image CA-Zertifikate, SQLite, Schreibrechte auf `/data` und den Start von DiscordChatExporter als `PUID:PGID`. Sie ersetzen keinen Live-Nachweis. Zum echten Empfangstest eine neue Nachricht eines konfigurierten Benutzers im konfigurierten Kanal senden lassen, `neue Treffer` im Log und die Pushover-Nachricht inklusive Link prüfen; anschließend neu starten und auf doppelte Zustellung prüfen. Ohne echte Zugangsdaten wird kein erfolgreicher Discord-/Pushover-PoC behauptet.

## Builds und Lizenz

GitHub Actions testet Python, baut ein amd64-Testimage, führt darin Tests und Selbsttest aus und baut erst danach die Linux-amd64-/arm64-Images. Pushes auf `main` veröffentlichen `latest` und einen SHA-Tag; `v*`-Tags veröffentlichen entsprechende Versionstags. Pull Requests bauen ohne Veröffentlichung. Grundlage ist das versionierte MIT-lizenzierte Image `tyrrrz/discordchatexporter:2.48`. Dieses Projekt: MIT, siehe LICENSE.
