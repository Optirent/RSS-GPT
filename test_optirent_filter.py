import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from optirent_filter import canonical_key, deduplicate_active, keep_entry, offer_deadline

NOW = datetime(2026, 11, 2, 15, 30, tzinfo=ZoneInfo("Europe/Berlin"))


def item(title, deadline="12.11.2026 10:00", link="https://www.service.bund.de/x/id.html"):
    return {
        "title": title,
        "link": link,
        "description": (
            "Erf&uuml;llungsort: <strong>Mainz</strong><br/>"
            "Vergabestelle: <strong>Stadt Mainz</strong><br/>"
            "Angebotsfrist: <strong>" + deadline + "</strong><br/>"
            "Veröffentlichungsende: <strong>30.11.2026 00:00</strong>"
        ),
    }


class FilterTests(unittest.TestCase):
    def test_active_container_delivery(self):
        entry = item("Lieferung und Aufbau einer Schulcontaineranlage")
        self.assertEqual(offer_deadline(entry).hour, 10)
        self.assertTrue(keep_entry(entry, now=NOW)[0])

    def test_expired_but_publication_still_open(self):
        entry = item("Bugenhagen-Schule - Interim", "12.08.2026 10:00")
        self.assertFalse(keep_entry(entry, now=NOW)[0])

    def test_deadline_today_already_passed(self):
        self.assertFalse(keep_entry(item("Containeranlage", "02.11.2026 15:29"), now=NOW)[0])
        self.assertTrue(keep_entry(item("Containeranlage", "02.11.2026 15:31"), now=NOW)[0])

    def test_unrelated_trades(self):
        for title in [
            "Neubau Containeranlage - Los 9b Sonnenschutzfolie",
            "Containeranlage Primusschule - Tiefbauarbeiten",
            "Hortcontainer - Los 5a Akustikdecken",
            "Interims-Containeranlage - Bodenbelagsarbeiten",
            "Bauvorhaben - Allgemeine Baustelleneinrichtung",
            "Interimsschule - Architektenleistung",
        ]:
            with self.subTest(title=title):
                self.assertFalse(keep_entry(item(title), now=NOW)[0])

    def test_full_scope_contract_not_filtered(self):
        for title in [
            "Erstellung der Ausführungsplanung und schlüsselfertige Errichtung einer Interimsschulanlage",
            "Lieferung und Errichtung von Modulgebäuden",
            "Sanitärcontainer und Laufsteg",
            "Bauvorhaben Neubau Adolf-Tannert-Schule",
        ]:
            with self.subTest(title=title):
                self.assertTrue(keep_entry(item(title), now=NOW)[0])

    def test_missing_deadline_not_mistaken_for_publication_end(self):
        entry = item("Containeranlage", "")
        self.assertFalse(keep_entry(entry, now=NOW)[0])
        self.assertIsNone(offer_deadline(entry))

    def test_same_notice_in_two_service_bund_import_paths(self):
        a = item("Neubau Hortcontainer", link="https://www.service.bund.de/IMPORTE/Ausschreibungen/subreport15/2026/10/E61872468.html#track=feed")
        b = item("Neubau Hortcontainer", link="https://www.service.bund.de/IMPORTE/Ausschreibungen/subreport/2026/10/E61872468.html")
        self.assertEqual(canonical_key(a), canonical_key(b))
        self.assertEqual(len(deduplicate_active([a, b], now=NOW)), 1)

    def test_different_lots_not_combined(self):
        a = item("Schule - Modulbau", link="https://www.service.bund.de/IMPORTE/Ausschreibungen/evergabede/2026/10/7080246.html")
        b = item("Schule - Modulbau", link="https://www.service.bund.de/IMPORTE/Ausschreibungen/evergabede/2026/10/7065368.html")
        self.assertEqual(len(deduplicate_active([a, b], now=NOW)), 2)

    def test_participation_deadline_supported(self):
        x = item("Errichtung Schulcontainer")
        x["description"] = "Teilnahmefrist: <strong>09.11.2026 10:00</strong>"
        self.assertTrue(keep_entry(x, now=NOW)[0])


if __name__ == "__main__":
    unittest.main()
