# OPTIRENT Ausschreibungsradar

Der tägliche Workflow führt `radar.py` aus. Er kombiniert die bestehenden
service.bund.de-Suchfeeds aus `config.ini` mit der amtlichen Open-Data-Schnittstelle
von öffentlichevergabe.de. Die bisherige RSS-Adresse bleibt erhalten:

https://optirent.github.io/RSS-GPT/optirent-ausschreibungen.xml

Die Startseite zeigt dieselben Chancen mit Suche und Einstufungsfilter.
`docs/radar-status.json` nennt den letzten erfolgreichen Abruf und die Anzahl der
zusammengeführten Dubletten. Automatisch als nicht passend bewertete Einträge
werden nicht in den gemeinsamen RSS-Feed aufgenommen.

## Aktualisierung

- Täglich 05:00 UTC (06:00 MEZ / 07:00 MESZ); GitHub kann geplante Läufe verzögert starten.
- öffentlichevergabe.de liefert abgeschlossene Veröffentlichungstage, daher bis
  zum Vortag. service.bund.de wird zusätzlich direkt abgefragt.
- Erstimport ab 01.09.2026. Ältere noch offene Verfahren können fehlen.
- Danach werden fehlende Tage seit dem letzten erfolgreichen Abruf nachgeholt;
  drei Tage werden überlappend erneut geprüft.
- `data/radar-state.json` hält Bekanntmachungen, Berichtigungsverweise,
  lotbezogene Ergebnisse, service.bund-Einträge und den letzten Abrufstand.
- Stimmen Tagesübersicht und XML-Details nicht überein, wird automatisch der
  vollständige Monat abgerufen und erneut auf Vollständigkeit geprüft. Schlägt auch
  diese Prüfung fehl, bleiben Feed und Abrufstand unverändert.
- Alle Quellen müssen erfolgreich abrufbar und syntaktisch gültig sein.
  Bei Fehlern bricht der Lauf vor der Veröffentlichung ab; der letzte gute Feed
  und sein Datenstand bleiben erhalten. Der Fehler ist in GitHub Actions sichtbar.
- Nach dem Commit der Daten fordert der Workflow ausdrücklich einen GitHub-Pages-
  Build an und prüft dessen Erfolg. So wird auch ein vom Workflow erzeugter Commit
  veröffentlicht, ohne von einem weiteren Push-Auslöser abhängig zu sein.

## Fünf Suchfilter

Stichwörter werden ohne Beachtung der Großschreibung im Bekanntmachungstext gesucht:

1. Mietcontainer, Modulcontainer, Containergebäude, Raummodule, Interimsgebäude, Kindergartencontainer.
2. Containeranlage, Containeranlagen, Bürocontainer, Schulcontainer, Sanitärcontainer, Raumcontainer.
3. Interimscontainer, Containerdorf, Raumzellen, Interimsschule.
4. Interimsgebäude, Ersatzgebäude, Behelfsgebäude, Interimslösung.
5. CPV 442111 einschließlich untergeordneter Codes, unabhängig von Stichwörtern.

Die Gruppen sind mit ODER verbunden. Die lokale Teilwortsuche ist bewusst breit
und kann von der Wortzerlegung der Browser-Suchmaschine abweichen. Lose bleiben
getrennt. „Passend“ ist eine regelbasierte Vorauswahl, keine abschließende Prüfung.
„Prüfen“ umfasst unklare Leistungen sowie Vorinformationen/Markterkundungen und
Einträge ohne erkennbare Frist. Fristen stammen aus Angebots-/Teilnahmefeldern;
Öffnungstermine und Veröffentlichungsende werden nicht als Angebotsfrist verwendet.

## Berichtigungen und Dubletten

- Höhere Versionen derselben ID ersetzen ältere, auch wenn der Suchbegriff entfällt.
- Explizite Berichtigungsverweise ersetzen ältere Bekanntmachungs-IDs.
- Zuschlags-/Nichtvergabemeldungen schließen nur das explizit bezeichnete Los.
- Quellenübergreifend wird nur bei einem eindeutigen gemeinsamen Dokumentlink
  oder einer eindeutigen Kombination aus Titel, Vergabestelle und Frist vereinigt.
  Bei mehreren möglichen Losen bleibt der Eintrag separat.
- Bei Zusammenführung wird die RSS-Identität des bestehenden service.bund-Eintrags
  beibehalten. Beide Quellen bleiben in der Beschreibung verlinkt.
- Unklare Dubletten und Aufhebungen ohne belastbare strukturierte Referenz können
  weiterhin eine manuelle Prüfung erfordern.

## Lokal prüfen

```text
python -m pip install -r requirements.txt
python -m unittest discover -p 'test_*.py' -v
python radar.py
```

Quelle: https://oeffentlichevergabe.de/documentation/swagger-ui/opendata/index.html

Der tägliche Radar benötigt für die Sammlung und Einstufung keinen OpenAI-API-Key.
Die ursprüngliche `main.py` bleibt als Legacy-Implementierung erhalten; der
produktive Workflow verwendet ausschließlich `radar.py`.
