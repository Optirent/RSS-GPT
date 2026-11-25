# Persistente OPTIRENT-Historie

## Datenvertrag

`docs/optirent-ausschreibungen-historie.json` (Schema 1) bewahrt jede erfasste
Bekanntmachung/Los-Kombination, Quellen, RSS-Aliasse, erstmalige/letzte Sichtung,
Feldänderungen mit Vorher-/Nachherstand sowie ereignisgenaue Meldebestätigungen.
Explizite amtliche IDs haben Vorrang. service.bund.de subreport/subreport15 und
Tracking-Parameter werden normalisiert. Eine spätere amtliche Anreicherung einer
bekannten RSS-Kennung eröffnet keinen zweiten Datensatz. Unterschiedliche Lose
werden nicht zusammengeführt. Freitext-Lose werden nicht erfunden.

`docs/optirent-ausschreibungen-neu.json` enthält:

- `daily_events`: alle Ereignisse des letzten Erfassungstags (Europe/Berlin),
  auch bei mehreren Läufen an diesem Tag. Am nächsten Tag beginnt nur diese Liste neu.
- `pending_events`: sämtliche meldepflichtigen Ereignisse ohne Zustellbestätigung,
  unabhängig von Alter, Frist, Einstufung und aktuellem Feed-Vorkommen.
- Zähler für RSS-Neueinträge, normalisierte neue Datensätze, identifizierte
  Verfahrenskennungen und explizite Los-IDs. Unbekannte Verfahrenskennungen werden
  separat ausgewiesen; LOT-0000 belegt keine Anzahl fachlicher Lose.
- Erfassungsstand, Quellenstatus und Migration. Vollständige Beschreibung und
  Vorherstand stehen in der Historie; die Tagesdatei ist bewusst kompakter.

`new`, `changed`, `correction`, `reappeared` und `left_feed` sind verschiedene
Ereignisarten. Eine unverändert wieder auftauchende ID ist kein Neuzugang und löst
keine erneute Meldung aus. Änderungen derselben ID – auch A → B → A – erhalten
eigene Ereignisse. Ein Wechsel der Vergabenummer unter derselben ID bleibt damit
sichtbar und muss fachlich als mögliches neues Verfahren geprüft werden.
Explizite Berichtigungsverweise werden als `corrects` erhalten; neue korrigierende
Bekanntmachungs-IDs zählen nicht als neue Verfahren. Ein Verschwinden aus dem Feed
ist **keine bestätigte Aufhebung**. Ein noch ungemeldeter ursprünglicher Treffer
bleibt auch dann in der Warteschlange.

## Erfasst versus gemeldet

Feed-Erstellung bestätigt niemals einen Versand. Erst ein Beleg mit exakten
Ereignis-IDs, Berichtskennung, Zeitpunkt mit Zeitzone und Zustellnachweis setzt
`reported_at`. Keine Quittierung aller Einträge bis zu einem Datum, keine
Quittierung anhand von Alter, Frist oder bloßer Existenz in `radar-seen.json`.

Nach einem nachweislich zugestellten Bericht kann eine neue Datei unter
`data/radar-receipts/<eindeutige-berichtskennung>.json` committed werden:

```json
{
  "event_ids": ["exakte ID aus pending_events"],
  "report_id": "eindeutige-berichtskennung",
  "evidence": "Link/ID der tatsächlich zugestellten Nachricht",
  "reported_at": "2026-11-26T08:20:00+01:00"
}
```

Der bestehende Workflow verarbeitet diese Dateien vor der Veröffentlichung.
Identische Quittungen sind wiederholbar, unbekannte IDs und widersprüchliche
Quittungen brechen ab. Neuere Änderungen derselben Bekanntmachung bleiben offen.
Alternativ: `python radar_history.py ack --receipt receipt.json`; Historie und
Tagesdatei müssen anschließend **gemeinsam** committed werden. Entwürfe und
fehlgeschlagene Zustellungen sind keine Belege. Der aufrufende Berichtsprozess
muss den Nachweis prüfen; das Python-Programm kann Chat-Zustellung nicht selbst
bestätigen. Bei fehlenden Schreibrechten bleiben Ereignisse offen. Der nächste
Bericht gleicht sie gegen tatsächlich vorhandene frühere Berichte ab.

## Migration am 25.11.2026

Basis: `45117bd9f935ae935ad7532fd75fedc1086b3aee`.
Ende: `bae77e1bce5bf075306f3b1c66e842c8eaf30afb`.
Alle 20 auf dem Hauptzweig gespeicherten Feed-Stände einschließlich der Basis
wurden chronologisch gelesen, mit Commit, XML-Blob und SHA-256 protokolliert.
Mehrere Stände vom 12.11. sind enthalten; Tage ohne Feed-Commit werden nicht als
erfolgreiche Abrufe erfunden. Die Vollständigkeit gilt für die Git-Historie,
nicht für sämtliche jemals extern veröffentlichten Ausschreibungen.

Ergebnis: 182 normalisierte Datensätze, 256 Ereignisse (182 neu, 16 geändert,
58 aus dem Feed verschwunden). 14 Quellenanreicherungen bleiben beim bekannten
Datensatz. Die bestehende Automation legt genau die 82 Basiseinträge ausdrücklich
als bereits gemeldet fest; Anzahl und XML-Blob wurden vor Übernahme geprüft.
Zwei weitere Neumeldungen sind anhand der tatsächlich vorliegenden Berichte vom
24.11. (Kleincotta, 7104082) und 25.11. (Kirchentag, 26074342/LOT-0000) bestätigt.
Weitere historische Meldungen sind nicht vollständig belegbar: 114 Ereignisse
bleiben zur Nachprüfung offen. Sie sind nicht automatisch 114 neue Verfahren.

## Verifikation und Betrieb

- 51 Tests einschließlich Tageswechsel, Ausfall, Wiederauftauchen, wechselnder
  Vergabenummer, zwei Losen, Berichtigung, A/B/A-Versionen, Quittungsfehlern,
  idempotentem Wiederholen und Abbruch vor Dateiersetzung.
- Zweimalige vollständige Migration liefert identische Ergebnisse.
- Der aktuelle RSS-Feed lässt sich aus dem gespeicherten Quellzustand bytegenau
  reproduzieren; isolierter Veröffentlichungslauf erzeugt keine Scheinänderungen.
- `config.ini`, die 25 Suchbegriffe, RSS-Adresse und RSS-Erzeugung bleiben erhalten.
- Historie, Tagesdatei, Feed und Abrufstatus werden zusammen committed. Bei einem
  zwischenzeitlich veränderten Remote-Stand wird der Push abgewiesen: kein Rebase
  alter generierter Zustände über neue Meldebestätigungen. Workflow erneut starten.
- Fehlende/beschädigte Historie führt zum Abbruch statt zu einem leeren Neustart.

Rekonstruktion in einer vollständigen frischen Kopie ohne vorhandene Historie:
`python radar_history.py migrate --base 45117bd9f935ae935ad7532fd75fedc1086b3aee`.
Das Werkzeug überschreibt keine bestehende Historie. Für einen isolierten Audit
kann `migrate(Path('.'), head='<Commit>')` ohne Schreiben aufgerufen werden.
Die zwei zusätzlich belegten Tagesberichte sind in den gelieferten Quittungen
der Historie dokumentiert; ein nackter Neuaufbau bestätigt nur die feste Basis.

## Grenzen und offene Punkte

- Ältere Tagesberichte nach der Basis liegen nicht lückenlos vor. Kein pauschales
  „bereits gemeldet“; historische offene Ereignisse separat nachprüfen.
- Verfahrensnummern und Fristen fehlen teils in den strukturierten Quellen.
  Unbekannte Angaben bleiben leer; fachliche Lose im Beschreibungstext benötigen
  Quellenprüfung. Quellstatus und Feed-Präsenz sind unterschiedliche Aussagen.
- Die Historie erfasst den bestehenden aggregierten RSS-Bestand. Bereits vorher
  durch den Sammler ausgeschlossene Quellenmeldungen lassen sich nicht aus dem
  Feed rekonstruieren; dessen bestehende Filter wurden nicht verändert.
- Ohne expliziten Berichtigungsverweis ist eine neue ID nicht sicher einem alten
  Verfahren zuzuordnen. Keine spekulative Zusammenführung nach Titel allein.
- Die vollständige Historie wächst; reguläre Berichte lesen die kompakte
  Tagesdatei. Bei langfristigem Größenwachstum kann die Historie später mit
  unveränderten Ereignis-IDs archiviert/aufgeteilt werden.
