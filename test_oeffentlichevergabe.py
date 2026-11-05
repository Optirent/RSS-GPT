import datetime as dt
import unittest
import xml.etree.ElementTree as ET
from oeffentlichevergabe import BERLIN, deadline, matching_groups, parse_notice, relevance

class ImportTests(unittest.TestCase):
    def test_cpv_does_not_require_keyword(self):
        self.assertEqual(matching_groups('Ungewöhnlicher Titel',['44211100']),['5 CPV 442111'])

    def test_keyword_does_not_require_cpv(self):
        self.assertIn('2 Containeranlagen',matching_groups('Anmietung Schulcontainer',['45223800']))

    def test_multiple_groups_preserved(self):
        self.assertEqual(len(matching_groups('Mietcontainer und Schulcontainer',['44211100'])),3)

    def test_opening_date_is_not_deadline(self):
        el=ET.fromstring('<x xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"><cac:OpenTenderEvent><cbc:OccurrenceDate>2026-12-10</cbc:OccurrenceDate></cac:OpenTenderEvent></x>')
        self.assertEqual(deadline(el),(None,'',False))

    def test_participation_deadline_and_timezone(self):
        el=ET.fromstring('<x xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"><cac:ParticipationRequestReceptionPeriod><cbc:EndDate>2026-12-08+01:00</cbc:EndDate><cbc:EndTime>08:00:00+01:00</cbc:EndTime></cac:ParticipationRequestReceptionPeriod></x>')
        value,label,exact=deadline(el)
        self.assertEqual(value.isoformat(),'2026-12-08T08:00:00+01:00')
        self.assertEqual(label,'Teilnahmefrist');self.assertTrue(exact)

    def test_date_only_does_not_invent_display_time(self):
        el=ET.fromstring('<x xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"><cac:TenderSubmissionDeadlinePeriod><cbc:EndDate>2026-07-08+02:00</cbc:EndDate></cac:TenderSubmissionDeadlinePeriod></x>')
        value,label,exact=deadline(el)
        self.assertFalse(exact);self.assertEqual(value.utcoffset(),dt.timedelta(hours=2))

    def test_site_setup_is_review_not_rejected(self):
        self.assertEqual(relevance('Baustelleneinrichtung','mit Bürocontainer','competition')[0],'Prüfen')

    def test_planning_without_deadline_is_review(self):
        self.assertEqual(relevance('Containeranlage','Markterkundung','planning')[0],'Prüfen')

    def test_lot_links_remain_distinct(self):
        xml='''<ContractNotice xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"><cbc:ID>N1</cbc:ID><cbc:VersionID>02</cbc:VersionID><cbc:NoticeTypeCode listName="competition">cn-standard</cbc:NoticeTypeCode><cac:ProcurementProject><cbc:Name>Mietcontainer</cbc:Name></cac:ProcurementProject><cac:ProcurementProjectLot><cbc:ID>LOT-0001</cbc:ID></cac:ProcurementProjectLot><cac:ProcurementProjectLot><cbc:ID>LOT-0002</cbc:ID></cac:ProcurementProjectLot></ContractNotice>'''
        entries=parse_notice(xml.encode())[-1]
        self.assertEqual(len(entries),2)
        self.assertNotEqual(entries[0]['url'],entries[1]['url'])
        self.assertIn('noticeId=N1',entries[0]['url'])

    def test_inventory_for_container_school_is_not_container_supply(self):
        title='Containeranlage, LOS 8: Lieferung und Montage Inventar Werkräume'
        self.assertEqual(relevance(title,'','competition')[0],'Nicht passend')

if __name__=='__main__': unittest.main()
