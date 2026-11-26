import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import radar_history as h
import radar
from test_radar import row, NOW


def sample(**changes):
    value = dict(key='ov:n1:LOT-0001', notice_id='n1', lot_id='LOT-0001',
                 rss_guid='ov:n1:LOT-0001', title='Container', status='in_feed',
                 deadline='2026-12-01', procedure_id='P1')
    value.update(changes)
    return value


class HistoryTests(unittest.TestCase):
    def test_ted_enrichment_retains_explicit_correction(self):
        import radar
        from test_radar import row, NOW
        r=dict(row(), guid='ov:N1:LOT-0001', sources=['TED'], links=['https://ted.europa.eu/de/notice/-/detail/1-2026'])
        entry=dict(notice_id='N1',lot_id='LOT-0001',procedure_id='P',version='01',deadline=r['deadline'],buyer=r['buyer'],kind='competition')
        state={'ted':{'state':{'notices':{'n1':{'entries':[entry],'corrects':['old-id']}}}}}
        parsed=h.parse_feed(radar.rss_bytes([r],NOW),state)
        self.assertEqual(parsed[0]['corrects'],['old-id'])

    def setUp(self):
        self.h = h.initial()

    def capture(self, rows, day=25, suffix=''):
        h.capture(self.h, rows, f'2026-11-{day:02}T08:00:00+01:00', str(day)+suffix)

    def test_missed_reports_survive_day_and_disappearance(self):
        self.capture([sample()])
        self.capture([], 26)
        self.assertEqual(len(h.delta(self.h)['pending_events']), 1)
        self.assertEqual(self.h['records'][sample()['key']]['current']['status'], 'not_in_feed')

    def test_reappearance_is_not_new_or_reportable_without_changes(self):
        self.capture([sample()])
        self.capture([], 26)
        self.capture([sample()], 27)
        self.assertEqual(self.h['events'][-1]['kind'], 'reappeared')
        self.assertFalse(self.h['events'][-1]['report_required'])
        self.assertEqual(h.delta(self.h)['counts']['new_entities'], 0)

    def test_changed_return_and_reused_id_are_retained(self):
        self.capture([sample()])
        self.capture([], 26)
        self.capture([sample(procedure_id='P2', deadline='2027-01-01')], 27)
        self.assertTrue(self.h['events'][-1]['report_required'])
        self.assertIn('procedure_id', self.h['events'][-1]['changed_fields'])
        self.assertEqual(len(h.delta(self.h)['pending_events']), 2)

    def test_revision_a_b_a_has_distinct_events(self):
        for day, value in [(25,'A'),(26,'B'),(27,'A')]:
            self.capture([sample(title=value)], day)
        self.assertEqual(len({e['id'] for e in self.h['events']}), 3)

    def test_day_accumulates_and_next_day_resets_only_daily(self):
        self.capture([sample()])
        self.capture([sample(title='Correction')], suffix='b')
        self.assertEqual(len(h.delta(self.h)['daily_events']), 2)
        self.capture([sample(title='Correction')], 26)
        self.assertEqual(h.delta(self.h)['daily_events'], [])
        self.assertEqual(len(h.delta(self.h)['pending_events']), 2)

    def test_receipt_is_exact_idempotent_and_preserves_newer_revision(self):
        self.capture([sample()])
        eid = self.h['events'][0]['id']
        self.capture([sample(title='Updated')], 26)
        args = ([eid], 'report1', 'Delivered message URL', '2026-11-26T09:00:00+01:00')
        h.acknowledge(self.h, *args)
        h.acknowledge(self.h, *args)
        self.assertEqual(len(h.delta(self.h)['pending_events']), 1)
        self.assertEqual(len(self.h['receipts']), 1)

    def test_invalid_receipt_does_not_partially_acknowledge(self):
        self.capture([sample()])
        before = copy.deepcopy(self.h)
        with self.assertRaises(ValueError):
            h.acknowledge(self.h, [self.h['events'][0]['id'], 'unknown'], 'R', 'proof', '2026-11-26T08:00:00Z')
        self.assertEqual(self.h, before)

    def test_explicit_correction_not_counted_as_new(self):
        self.capture([sample()])
        self.capture([sample(key='ov:n2:LOT-0001', notice_id='n2', rss_guid='ov:n2:LOT-0001', corrects=['n1'])], 26)
        self.assertEqual(h.delta(self.h)['counts']['new_entities'], 0)
        self.assertEqual(self.h['events'][1]['kind'], 'correction')

    def test_lots_remain_separate(self):
        self.capture([sample(), sample(key='ov:n1:LOT-0002', lot_id='LOT-0002', rss_guid='ov:n1:LOT-0002')])
        self.assertEqual(len(self.h['records']), 2)

    def test_source_enrichment_keeps_known_rss_identity(self):
        self.capture([sample(key='https://service.bund.de/a', rss_guid='https://service.bund.de/a', notice_id=None, lot_id=None)])
        self.capture([sample(rss_guid='https://service.bund.de/a')], 26)
        self.assertEqual(len(self.h['records']), 1)
        self.assertEqual(self.h['events'][-1]['kind'], 'changed')

    def test_ids_normalize_tracking_subreport_and_lots(self):
        self.assertEqual(h.identity('ov:ABC:lot-0001', []), ('ov:abc:LOT-0001','abc','LOT-0001'))
        self.assertEqual(h.identity('https://www.service.bund.de/subreport15/x?utm_x=y', [])[0],
                         'https://www.service.bund.de/subreport/x')

    def test_invalid_feed_and_duplicate_ids_fail(self):
        with self.assertRaises(ValueError):
            h.parse_feed(b'<html/>')
        with self.assertRaises(ValueError):
            self.capture([sample(), sample()])
        self.assertEqual(self.h, h.initial())

    def test_replay_same_snapshot_is_noop(self):
        self.capture([sample()])
        old = copy.deepcopy(self.h)
        self.capture([sample()])
        self.assertEqual(self.h, old)

    def test_publish_missing_history_fails_before_any_file_changes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            status = dict(updated_at=NOW.isoformat(), updated_at_display='test', through='2026-11-01', new_items=1)
            r = dict(row(), guid='N1', sources=['ov'], links=[row()['url']])
            with self.assertRaises(ValueError):
                radar.publish({}, [r], status, root, {'new_guids':['N1']})
            self.assertEqual(list(root.iterdir()), [])

    def test_publish_invalid_history_fails_before_replacing_feed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root/'docs').mkdir()
            (root/h.HISTORY).write_text('{broken')
            (root/h.FEED).write_bytes(b'old')
            status = dict(updated_at=NOW.isoformat(), updated_at_display='test', through='2026-11-01', new_items=1)
            r = dict(row(), guid='N1', sources=['ov'], links=[row()['url']])
            with self.assertRaises(ValueError):
                radar.publish({}, [r], status, root, {'new_guids':['N1']})
            self.assertEqual((root/h.FEED).read_bytes(), b'old')


if __name__ == '__main__':
    unittest.main()
