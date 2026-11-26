"""Append-only observations and explicit, event-level delivery receipts.

RSS remains the public inventory; absence from it does not mean cancellation.
Only a receipt with evidence changes captured events to reported events.
"""
import argparse
import copy
import datetime as dt
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
import unicodedata
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from optirent_filter import canonical_key

BASE = '45117bd9f935ae935ad7532fd75fedc1086b3aee'
FEED = 'docs/optirent-ausschreibungen.xml'
HISTORY = 'docs/optirent-ausschreibungen-historie.json'
DELTA = 'docs/optirent-ausschreibungen-neu.json'
BERLIN = ZoneInfo('Europe/Berlin')


def clean(value):
    return ' '.join(unicodedata.normalize('NFKC', html.unescape(value or '')).split())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def identity(guid, links):
    """Use explicit IDs only; never infer distinct lots from free text."""
    for link in links:
        p = urlsplit(link)
        q = parse_qs(p.query)
        if (p.hostname or '').lower() in ('oeffentlichevergabe.de', 'www.oeffentlichevergabe.de') and q.get('noticeId'):
            nid = clean(q['noticeId'][0]).lower()
            lot = clean(q.get('lotId', [''])[0]).upper()
            return 'ov:' + nid + ':' + lot, nid, lot
    if guid.lower().startswith('ov:'):
        _, nid, lot = guid.split(':', 2)
        return 'ov:' + nid.lower() + ':' + lot.upper(), nid.lower(), lot.upper()
    # Keep original service.bund identity; URL canonicalization is done upstream.
    return canonical_key({'link': clean(guid)}), '', ''


def parse_feed(data, state=None):
    root = ET.fromstring(data)
    if root.tag != 'rss' or root.find('channel') is None:
        raise ValueError('Expected valid RSS channel')
    rows = []
    for item in root.findall('./channel/item'):
        guid = clean(item.findtext('guid') or item.findtext('link'))
        if not guid:
            raise ValueError('RSS entry has no identity')
        soup = BeautifulSoup(item.findtext('description') or '', 'html.parser')
        links = sorted(set(filter(None, [item.findtext('link')] +
                                 [a.get('href') for a in soup.find_all('a', href=True)])))
        key, nid, lot = identity(guid, links)
        lines = soup.get_text('\n', strip=True)
        def field(label):
            match = re.search(re.escape(label) + r'\s*:\s*([^\n]+)', lines, re.I)
            return clean(match.group(1)) if match else None
        title = clean(item.findtext('title'))
        rating = re.match(r'^\[(.*?)\]\s*', title)
        rows.append(dict(key=key, notice_id=nid or None, lot_id=lot or None,
                         rss_guid=guid, title=title[rating.end():] if rating else title,
                         rating=rating.group(1) if rating else None,
                         buyer=field('Vergabestelle'), location=field('Erfüllungsort'), procedure_id=field('Vergabenummer'),
                         deadline=field('Angebotsfrist') or field('Teilnahmefrist'),
                         publication=clean(item.findtext('pubDate')) or None,
                         description=clean(soup.get_text(' ', strip=True)),
                         original_source=item.findtext('link'), sources=links,
                         status='in_feed'))
    if len({r['key'] for r in rows}) != len(rows):
        raise ValueError('Ambiguous duplicate normalized identities')
    for row in rows:
        notice = (state or {}).get('notices', {}).get(row['notice_id'], {})
        if not notice:
            notice = (state or {}).get('ted', {}).get('state', {}).get('notices', {}).get(row['notice_id'], {})
        entries = [e for e in notice.get('entries', [])
                   if clean(e.get('lot_id')).upper() == (row['lot_id'] or '')]
        if len(entries) == 1:
            entry = entries[0]
            row.update(procedure_id=entry.get('procedure_id') or None,
                       version=entry.get('version') or None,
                       deadline=entry.get('deadline') or row['deadline'],
                       buyer=entry.get('buyer') or row['buyer'],
                       source_status=entry.get('kind'),
                       corrects=notice.get('corrects', []))
    return rows


def initial():
    return dict(schema=1, records={}, events=[], snapshots=[], receipts=[])


def validate(history):
    if history.get('schema') != 1:
        raise ValueError('Unsupported history schema')
    records, events = history['records'], history['events']
    ids = [e['id'] for e in events]
    referenced = [eid for r in records.values() for eid in r['event_ids']]
    if len(ids) != len(set(ids)) or sorted(ids) != sorted(referenced):
        raise ValueError('History event index is inconsistent')
    receipts = {r['report_id']: r for r in history['receipts']}
    if len(receipts) != len(history['receipts']):
        raise ValueError('Duplicate receipt IDs')
    for e in events:
        if e['key'] not in records or e['id'] not in records[e['key']]['event_ids']:
            raise ValueError('Missing history record')
        if e['reported_at'] and (e['receipt_id'] not in receipts or
                                e['id'] not in receipts[e['receipt_id']]['event_ids']):
            raise ValueError('Reported event has no receipt')


def capture(history, rows, observed_at, snapshot_id, metadata=None):
    validate(history)
    if any(s['id'] == snapshot_id for s in history['snapshots']):
        return
    stamp = dt.datetime.fromisoformat(observed_at)
    if stamp.tzinfo is None:
        raise ValueError('Observation must have timezone')
    if history['snapshots'] and stamp < dt.datetime.fromisoformat(history['snapshots'][-1]['observed_at']):
        raise ValueError('Cannot replay observations out of order')
    day = stamp.astimezone(BERLIN).date().isoformat()
    rows = copy.deepcopy(rows)
    for row in rows:
        aliases = [r for r in history['records'].values()
                   if row['rss_guid'] in r['aliases'] and
                   (not r['current'].get('lot_id') or r['current']['lot_id'] == row.get('lot_id'))]
        if row['key'] not in history['records'] and len(aliases) == 1:
            row['key'] = aliases[0]['key']
    current = {r['key'] for r in rows}
    if len(current) != len(rows):
        raise ValueError('Duplicate identities')
    def event(record, kind, before, after):
        number = len(record['event_ids']) + 1
        eid = digest([record['key'], number, kind, after])
        changed = sorted(k for k in set(before or {}) | set(after)
                         if (before or {}).get(k) != after.get(k))
        history['events'].append(dict(id=eid, key=record['key'], kind=kind,
            observed_at=observed_at, day=day, snapshot_id=snapshot_id,
            changed_fields=changed, before=before, after=copy.deepcopy(after),
            report_required=kind != 'left_feed' and not (kind == 'reappeared' and changed == ['status']),
            reported_at=None, receipt_id=None))
        record['event_ids'].append(eid)
    for row in rows:
        row = copy.deepcopy(row)
        key = row['key']
        record = history['records'].get(key)
        if record is None:
            record = dict(key=key, first_seen=observed_at, last_seen=observed_at,
                          event_ids=[], aliases=[], current=None)
            history['records'][key] = record
        old = record['current']
        if old != row:
            kind = 'new' if old is None else ('reappeared' if old['status'] == 'not_in_feed' else 'changed')
            if old is None and row.get('corrects'):
                kind = 'correction'
            event(record, kind, copy.deepcopy(old), row)
        record['current'] = row
        record['last_seen'] = observed_at
        record['aliases'] = sorted(set(record['aliases']) | {row['rss_guid']})
    for key, record in history['records'].items():
        if key not in current and record['current']['status'] != 'not_in_feed':
            old = copy.deepcopy(record['current'])
            record['current']['status'] = 'not_in_feed'
            event(record, 'left_feed', old, record['current'])
    history['snapshots'].append(dict(id=snapshot_id, observed_at=observed_at, day=day,
                                   entries=len(rows), **(metadata or {})))


def acknowledge(history, event_ids, report_id, evidence, reported_at):
    """A report is acknowledged only after delivery, never while drafting it."""
    if not event_ids or not report_id.strip() or not evidence.strip():
        raise ValueError('Explicit events, report ID and delivery evidence required')
    stamp = dt.datetime.fromisoformat(reported_at)
    if stamp.tzinfo is None:
        raise ValueError('Receipt requires timezone')
    events = {e['id']: e for e in history['events']}
    if set(event_ids) - events.keys():
        raise ValueError('Unknown event IDs; receipt not applied')
    if any(stamp < dt.datetime.fromisoformat(events[e]['observed_at']) for e in event_ids):
        raise ValueError('Receipt predates observation')
    receipt = dict(report_id=report_id, event_ids=sorted(set(event_ids)),
                   evidence=evidence, reported_at=reported_at)
    existing = next((r for r in history['receipts'] if r['report_id'] == report_id), None)
    if existing:
        if existing != receipt:
            raise ValueError('Conflicting receipt')
        return
    if any(events[e]['reported_at'] for e in event_ids):
        raise ValueError('Event already acknowledged by another receipt')
    history['receipts'].append(receipt)
    for eid in event_ids:
        events[eid].update(reported_at=reported_at, receipt_id=report_id)


def delta(history):
    latest = history['snapshots'][-1] if history['snapshots'] else None
    day = latest['day'] if latest else None
    daily = [e for e in history['events'] if e['day'] == day]
    pending = [e for e in history['events'] if e['report_required'] and e['reported_at'] is None]
    new = [e for e in daily if e['kind'] == 'new']
    procedures = {e['after'].get('procedure_id') for e in new} - {None, ''}
    def compact(event):
        # The report input stays small; complete before/after content is in history.
        return {**{k:v for k,v in event.items() if k not in ('before','after')},
                'current_status': history['records'][event['key']]['current']['status'],
                'first_seen': history['records'][event['key']]['first_seen'],
                'last_seen': history['records'][event['key']]['last_seen'],
                'after': {k:v for k,v in event['after'].items() if k != 'description'}}
    return dict(schema=1, day=day, snapshot=latest, daily_events=list(map(compact,daily)),
                pending_events=list(map(compact,pending)), counts=dict(daily_events=len(daily),
                pending_events=len(pending), captured_entities=len(history['records']),
                new_rss_entries=len({e['after']['rss_guid'] for e in new if e['after'].get('rss_present',True)}),
                new_entities=len(new), identified_procedures_among_new=len(procedures),
                new_with_unknown_procedure=sum(not e['after'].get('procedure_id') for e in new),
                explicit_lot_ids_among_new=len({(e['after'].get('notice_id'),e['after']['lot_id'])
                                               for e in new if e['after'].get('lot_id')})),
                migration=history.get('migration'),
                limitations=['LOT-0000 is a source lot ID, not evidence of separate specialist lots.',
                             'Feed absence is not cancellation; procedure IDs may be unknown.',
                             'Captured is not reported. Unknown historical delivery remains pending.'])


def payloads(history):
    validate(history)
    return {name: (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
            for name, data in [(HISTORY, history), (DELTA, delta(history))]}


def migrate(root, base=BASE, head='HEAD'):
    def git(*args):
        return subprocess.check_output(['git', '-C', str(root), *args])
    git('merge-base', '--is-ancestor', base, head)
    commits = [base] + git('rev-list', '--reverse', '--first-parent',
                           base + '..' + head, '--', FEED).decode().splitlines()
    history = initial()
    for sha in commits:
        data = git('show', sha + ':' + FEED)
        when = git('show', '-s', '--format=%cI', sha).decode().strip()
        blob = git('rev-parse', sha + ':' + FEED).decode().strip()
        state = None
        exists = subprocess.run(['git', '-C', str(root), 'cat-file', '-e',
                                 sha + ':data/radar-state.json'], capture_output=True)
        if exists.returncode == 0:
            state = json.loads(git('show', sha + ':data/radar-state.json'))
        capture(history, parse_feed(data, state), when, sha, dict(feed_blob=blob, feed_sha256=hashlib.sha256(data).hexdigest()))
    history['migration'] = dict(base_commit=base, head_commit=git('rev-parse', head).decode().strip(),
        snapshots=len(commits), complete=True, reporting_status='unknown_pending',
        policy='No historical event is marked reported without a delivery receipt.')
    if base == BASE:
        baseline = [e for e in history['events'] if e['snapshot_id'] == base]
        if len(baseline) != 82 or history['snapshots'][0]['feed_blob'] != '78d9116ef6faee08b41dc5dada6c60e97cd26278':
            raise ValueError('Baseline does not match the explicitly reported 82 entries')
        acknowledge(history, [e['id'] for e in baseline], 'baseline-2026-11-06',
                    'Existing automation 6ae8879c8a748191acf204ba2ef41b12 explicitly declares these exact 82 baseline entries reported; configuration inspected 2026-11-25.',
                    history['snapshots'][0]['observed_at'])
        history['migration']['baseline_reported'] = 82
    return history


def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['migrate', 'ack'])
    p.add_argument('--root', type=Path, default=Path('.'))
    p.add_argument('--base', default=BASE)
    p.add_argument('--head', default='HEAD')
    p.add_argument('--receipt', type=Path)
    args = p.parse_args()
    path = args.root / HISTORY
    if args.command == 'migrate':
        if path.exists():
            raise ValueError('Refusing to overwrite existing history and receipts')
        history = migrate(args.root, args.base, args.head)
    else:
        history = json.loads(path.read_text(encoding='utf-8'))
        validate(history)
        acknowledge(history, **json.loads(args.receipt.read_text(encoding='utf-8')))
    for name, data in payloads(history).items():
        target = args.root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    print(json.dumps(delta(history)['counts']))


if __name__ == '__main__':
    main()
