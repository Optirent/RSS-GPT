import copy
import datetime as dt
import unittest
from unittest.mock import patch

import ted
import radar
from test_radar import notice

DAY=dt.date(2026,11,24)
TERMS=['container']*25


def result(number, nid='N1'):
    return {'publication-number':number,'notice-identifier':nid,
            'publication-date':DAY.isoformat()+'Z',
            'links':{'xml':{'MUL':'https://ted.europa.eu/en/notice/'+number+'/xml'}}}


class TEDTests(unittest.TestCase):
    def test_query_has_25_terms_cpv_and_publication_date(self):
        q=ted.query_for(DAY,TERMS)
        self.assertEqual(q.count('FT ~'),25)
        self.assertIn('442111*',q); self.assertIn('20261124',q)

    def test_multiple_pages_are_complete_and_unique(self):
        replies=[{'notices':[result('1-2026')],'totalNoticeCount':2,'timedOut':False,'iterationNextToken':'next'},
                 {'notices':[result('2-2026','N2')],'totalNoticeCount':2,'timedOut':False,'iterationNextToken':'unused'}]
        with patch('ted.post_json',side_effect=replies) as post:
            found=ted.search_day(DAY,TERMS,post)
        self.assertEqual(len(found),2)
        self.assertEqual(post.call_args_list[1].args[0]['iterationNextToken'],'next')

    def test_missing_page_or_timeout_fails(self):
        for response in [{'notices':[],'totalNoticeCount':1,'timedOut':False},
                         {'notices':[],'totalNoticeCount':0,'timedOut':True}]:
            with self.assertRaises(ted.IncompleteTED):
                ted.search_day(DAY,TERMS,lambda _:response)

    def test_failed_xml_does_not_advance_watermark_or_change_state(self):
        state={'ted':{'last_day':'2026-11-23','state':radar.initial_state()}}
        before=copy.deepcopy(state)
        def post(req):
            if '20261124' in req['query']:
                return {'notices':[result('123-2026')],'totalNoticeCount':1,'timedOut':False}
            return {'notices':[],'totalNoticeCount':0,'timedOut':False}
        with self.assertRaises(ValueError):
            ted.refresh(state,DAY,TERMS,radar.ingest,radar.initial_state,post,
                        lambda url: (_ for _ in ()).throw(ValueError('bad XML')))
        self.assertEqual(state,before)

    def test_missed_days_and_overlap(self):
        state={'ted':{'last_day':'2026-11-20','state':radar.initial_state()}}
        days=[]
        def post(req):
            days.append(req['query']);return {'notices':[],'totalNoticeCount':0,'timedOut':False}
        ted.refresh(state,DAY,TERMS,radar.ingest,radar.initial_state,post,lambda _:b'')
        self.assertEqual(len(days),7)
        self.assertIn('20261118',days[0]);self.assertIn('20261124',days[-1])
        self.assertEqual(state['ted']['last_day'],'2026-11-24')

    def test_ted_notice_xml_import_and_identity(self):
        s={}
        post=lambda _: {'notices':[result('123-2026')],'totalNoticeCount':1,'timedOut':False}
        ted.refresh(s,DAY,TERMS,radar.ingest,radar.initial_state,post,
                    lambda _:notice(), start=DAY)
        self.assertEqual(s['ted']['state']['notices']['N1']['entries'][0]['ted_url'],
                         'https://ted.europa.eu/de/notice/-/detail/123-2026')

    def test_untrusted_xml_link_rejected(self):
        with self.assertRaises(ted.IncompleteTED):ted.fetch_xml('https://evil.test/a.xml')


if __name__ == '__main__': unittest.main()
