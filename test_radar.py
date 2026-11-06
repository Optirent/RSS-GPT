import copy
import io
import zipfile
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
from oeffentlichevergabe import BERLIN
from optirent_filter import canonical_key
from radar import initial_state, ingest, ov_rows, merge_rows, rss_bytes, refresh_ov, run, IncompleteExportError

NOW=dt.datetime(2026,11,5,12,tzinfo=BERLIN)

def notice(nid='N1',version='01',title='Lieferung Schulcontainer',deadline='2026-12-01',changed='',kind='competition',result=''):
    return f'''<ContractNotice xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" xmlns:efac="http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1" xmlns:efbc="http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1"><cbc:ID>{nid}</cbc:ID><cbc:VersionID>{version}</cbc:VersionID><cbc:ContractFolderID>P1</cbc:ContractFolderID><cbc:NoticeTypeCode listName="{kind}">cn-standard</cbc:NoticeTypeCode><efbc:ChangedNoticeIdentifier>{changed}</efbc:ChangedNoticeIdentifier><cac:ProcurementProject><cbc:Name>{title}</cbc:Name></cac:ProcurementProject><cac:ProcurementProjectLot><cbc:ID>LOT-0001</cbc:ID><cac:TenderingProcess><cac:TenderSubmissionDeadlinePeriod><cbc:EndDate>{deadline}</cbc:EndDate><cbc:EndTime>10:00:00+01:00</cbc:EndTime></cac:TenderSubmissionDeadlinePeriod></cac:TenderingProcess></cac:ProcurementProjectLot>{result}</ContractNotice>'''.encode()

def row(source='öffentlichevergabe.de',lot='LOT-0001'):
    return dict(title='Lieferung Schulcontainer',buyer='Stadt Mainz',deadline='2026-12-01T10:00:00+01:00',deadline_kind='Angebotsfrist',deadline_has_time=True,description='Container',source=source,documents=[],publication='2026-11-01T10:00:00Z',location='Mainz',notice_id='N1',lot_id=lot,url='https://oeffentlichevergabe.de/ui/de/search/details?noticeId=N1&lotId='+lot,status='Aktuell',rating='Passend',reason='',groups=['2'],service_body='Angebotsfrist: 01.12.2026 10:00')

class RadarTests(unittest.TestCase):
    def export_pair(self, ids, xml_ids):
        csv_data='noticeIdentifier,noticeVersion,publicationDate\n'+''.join(f'{nid},1,2026-11-05T10:00:00Z\n' for nid in ids)
        bodies=[]
        for files in ({'notice.csv':csv_data}, {nid+'.xml':notice(nid=nid) for nid in xml_ids}):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as z:
                for name,body in files.items(): z.writestr(name,body)
            bodies.append(stream.getvalue())
        return bodies

    def test_incomplete_daily_export_recovers_from_month(self):
        state=initial_state();state['last_day']='2026-11-05'
        daily=self.export_pair(['N1','N2'],['N1'])
        monthly=self.export_pair(['N1','N2'],['N1','N2'])
        with patch('radar.request_bytes',side_effect=daily+monthly) as fetch:
            imports=refresh_ov(state,dt.date(2026,11,5),'2026-11-05')
        self.assertEqual(set(state['notices']),{'N1','N2'})
        self.assertEqual(imports[0]['source_period'],'2026-11')
        self.assertEqual(imports[0]['notices'],2)
        self.assertIn('pubMonth=2026-11',fetch.call_args_list[-1].args[0])

    def test_incomplete_month_preserves_state_and_watermark(self):
        state=initial_state();state['last_day']='2026-11-04';before=copy.deepcopy(state)
        incomplete=self.export_pair(['N1','N2'],['N1'])
        with patch('radar.request_bytes',side_effect=incomplete+incomplete):
            with self.assertRaises(IncompleteExportError):
                refresh_ov(state,dt.date(2026,11,5),'2026-11-05')
        self.assertEqual(state,before)

    def test_complete_daily_export_needs_no_month_download(self):
        state=initial_state();state['last_day']='2026-11-05'
        with patch('radar.request_bytes',side_effect=self.export_pair(['N1'],['N1'])) as fetch:
            refresh_ov(state,dt.date(2026,11,5),'2026-11-05')
        self.assertEqual(fetch.call_count,2)
        self.assertEqual(set(state['notices']),{'N1'})

    def test_query_ids_and_lots_preserved(self):
        self.assertNotEqual(canonical_key({'link':'https://oeffentlichevergabe.de/ui/de/search/details?noticeId=A'}),canonical_key({'link':'https://oeffentlichevergabe.de/ui/de/search/details?noticeId=B'}))
        self.assertNotEqual(canonical_key({'link':row(lot='LOT-0001')['url']}),canonical_key({'link':row(lot='LOT-0002')['url']}))

    def test_correction_new_id_replaces_old(self):
        s=initial_state();ingest(s,notice(),'2026-11-01T00:00:00Z');ingest(s,notice(nid='N2',changed='N1-01'),'2026-11-02T00:00:00Z')
        self.assertEqual({r['notice_id']:r['status'] for r in ov_rows(s,NOW)},{'N1':'Ersetzt','N2':'Aktuell'})

    def test_correction_same_id_keeps_latest(self):
        s=initial_state();ingest(s,notice(),'2026-11-01T00:00:00Z');ingest(s,notice(version='02',changed='N1-01',deadline='2026-12-05'),'2026-11-02T00:00:00Z')
        ingest(s,notice(),'2026-11-01T00:00:00Z')
        self.assertEqual(ov_rows(s,NOW)[0]['deadline'],'2026-12-05T10:00:00+01:00')
        self.assertEqual(ov_rows(s,NOW)[0]['status'],'Aktuell')

    def test_corrected_notice_loses_keyword(self):
        s=initial_state();ingest(s,notice(),'2026-11-01T00:00:00Z');ingest(s,notice(version='02',title='Andere Leistung'),'2026-11-02T00:00:00Z')
        self.assertEqual(ov_rows(s,NOW),[])

    def test_result_closes_only_named_lot(self):
        s=initial_state();ingest(s,notice(),'2026-11-01T00:00:00Z')
        result='<efac:LotResult><cbc:TenderResultCode>selec-w</cbc:TenderResultCode><efac:TenderLot><cbc:ID>LOT-0002</cbc:ID></efac:TenderLot></efac:LotResult>'
        ingest(s,notice(nid='R',kind='result',result=result),'2026-11-02T00:00:00Z')
        self.assertEqual(ov_rows(s,NOW)[0]['status'],'Aktuell')
        ingest(s,notice(nid='R2',kind='result',result=result.replace('LOT-0002','LOT-0001')),'2026-11-03T00:00:00Z')
        self.assertEqual(ov_rows(s,NOW)[0]['status'],'Abgeschlossen')

    def test_missing_deadline_retained(self):
        s=initial_state();ingest(s,notice(deadline='',kind='planning'),'2026-11-01T00:00:00Z')
        self.assertEqual(ov_rows(s,NOW)[0]['status'],'Status prüfen')

    def test_cross_source_duplicate_preserves_old_guid(self):
        service=row(source='service.bund.de');service['url']='https://www.service.bund.de/a/123.html'
        rows,count=merge_rows([row()],[service],NOW)
        self.assertEqual(len(rows),1);self.assertEqual(count,1)
        self.assertEqual(rows[0]['guid'],service['url']);self.assertEqual(len(rows[0]['sources']),2)

    def test_ambiguous_multi_lot_not_collapsed(self):
        service=row(source='service.bund.de');service['url']='https://www.service.bund.de/a/123.html'
        rows,count=merge_rows([row(),row(lot='LOT-0002')],[service],NOW)
        self.assertEqual(len(rows),3);self.assertEqual(count,0)

    def test_failed_source_leaves_files_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d);(p/'docs').mkdir();(p/'docs/optirent-ausschreibungen.xml').write_bytes(b'previous feed')
            (p/'config.ini').write_text('[source001]\nurl="https://source.test/feed"\n')
            with patch('radar.refresh_ov',return_value=[]),patch('radar.refresh_service',side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):run(p)
            self.assertEqual((p/'docs/optirent-ausschreibungen.xml').read_bytes(),b'previous feed')
            self.assertFalse((p/'data/radar-state.json').exists())

    def test_missed_days_replayed(self):
        s=initial_state();s['last_day']='2026-11-01'
        with patch('radar.request_bytes',return_value=b'zip') as fetch,patch('radar.apply_export',return_value=0):
            refresh_ov(s,dt.date(2026,11,4),'2026-09-01')
        urls=[x.args[0] for x in fetch.call_args_list]
        self.assertTrue(any('pubDay=2026-10-30' in u for u in urls))
        self.assertTrue(any('pubDay=2026-11-04' in u for u in urls))
        self.assertEqual(s['last_day'],'2026-11-04')

    def test_rss_escapes_untrusted_text(self):
        r=row();r['title']='A & B <script>';r.update(guid='ID',sources=['a'],links=[r['url']])
        root=ET.fromstring(rss_bytes([r],NOW))
        self.assertIn('A & B <script>',root.findtext('.//item/title'))

if __name__=='__main__':unittest.main()
