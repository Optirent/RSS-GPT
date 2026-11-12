"""Daily OPTIRENT feed: service.bund.de RSS + official eForms exports.

An incomplete eForms import never advances the verified watermark. During eForms outages, service.bund.de may refresh with an explicit stale-source warning.
Persistent state is committed together with the feed by the workflow.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import configparser
import copy
import csv
import datetime as dt
from email.utils import format_datetime, parsedate_to_datetime
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import URLError
import xml.etree.ElementTree as ET
import zipfile

import feedparser
from bs4 import BeautifulSoup
from oeffentlichevergabe import BERLIN, NS, clean, txt, parse_notice, relevance
from optirent_filter import canonical_key, keep_entry, offer_deadline, source_content

API='https://oeffentlichevergabe.de/api/notice-exports'
PUBLIC='https://optirent.github.io/RSS-GPT/'
ACTIVE=('Aktuell','Status prüfen')

def initial_state():
    return {'schema':1,'last_day':None,'notices':{},'superseded':[], 'outcomes':{},'service':{}}

def request_bytes(url):
    for attempt in range(3):
        try:
            req=Request(url,headers={'User-Agent':'OPTIRENT-Ausschreibungsradar/1.0 (public procurement monitoring)'})
            with urlopen(req,timeout=180) as response:
                return response.read()
        except (URLError,TimeoutError):
            if attempt==2: raise
            time.sleep(2**attempt)

def version_key(version, publication):
    return (int(version) if str(version).isdigit() else 0, publication)

def ingest(state, xml, publication, root=None):
    if root is None:root=ET.fromstring(xml)
    nid,ver,proc,kind,entries=parse_notice(xml,publication,root)
    if not nid: raise ValueError('Notice without identifier')
    old=state['notices'].get(nid)
    if old and version_key(old['version'],old['publication']) > version_key(ver,publication):
        return
    refs={clean(e.text).rsplit('-',1)[0] for e in root.iter() if e.tag.split('}')[-1]=='ChangedNoticeIdentifier' and clean(e.text)}
    if refs-{nid}:
        state['superseded']=sorted(set(state['superseded']) | (refs-{nid}))
    # Retain an empty corrected record: disappearance of a keyword must remove the old entry.
    if entries or old:
        state['notices'][nid]={'version':ver,'publication':publication,'procedure':proc,'kind':kind,'entries':entries}
    if kind=='result' and proc:
        for result in root.findall('.//efac:LotResult',NS):
            lot=txt(result,'efac:TenderLot/cbc:ID')
            decision=txt(result,'cbc:TenderResultCode')
            if lot and decision in ('selec-w','clos-nw','open-nw'):
                key=proc+'|'+lot
                previous=state['outcomes'].get(key)
                if not previous or publication>=previous['publication']:
                    state['outcomes'][key]={'decision':decision,'publication':publication,'notice_id':nid}

class IncompleteExportError(ValueError):
    """The two official export formats do not describe the same notice set."""


def apply_export(state, csv_body, xml_body):
    with zipfile.ZipFile(io.BytesIO(csv_body)) as z:
        if not z.namelist(): pubs={}
        else:
            pubs={(r['noticeIdentifier'],r['noticeVersion'].lstrip('0') or '0'):r['publicationDate'] for r in csv.DictReader(io.TextIOWrapper(z.open('notice.csv'),encoding='utf-8-sig'))}
    seen=set()
    with zipfile.ZipFile(io.BytesIO(xml_body)) as z:
        for name in z.namelist():
            if not name.endswith('.xml'): continue
            data=z.read(name);root=ET.fromstring(data)
            key=(txt(root,'cbc:ID'),txt(root,'cbc:VersionID').lstrip('0') or '0')
            if key not in pubs: raise IncompleteExportError('XML notice missing from publication index: '+str(key))
            ingest(state,data,pubs[key],root);seen.add(key)
    if seen!=set(pubs): raise IncompleteExportError(f'Incomplete export: {len(pubs)} CSV notices, {len(seen)} XML notices; {len(set(pubs)-seen)} missing, {len(seen-set(pubs))} extra')
    return len(seen)

def refresh_ov(state, through, bootstrap_since, bootstrap_cache=None):
    """Atomically import only verified exports, retrying the same failed day."""
    imports=[]
    if state['last_day'] and dt.date.fromisoformat(state['last_day'])>through:
        raise ValueError('Stored watermark is ahead of the requested export day')
    if state['last_day']:
        # Replay three days: retries and late corrections remain idempotent.
        start=max(dt.date.fromisoformat(bootstrap_since),dt.date.fromisoformat(state['last_day'])-dt.timedelta(days=2))
        periods=[('pubDay',(start+dt.timedelta(days=i)).isoformat()) for i in range((through-start).days+1)]
    else:
        first=dt.date.fromisoformat(bootstrap_since).replace(day=1)
        periods=[]
        while first<=through:
            periods.append(('pubMonth',first.strftime('%Y-%m')))
            first=(first.replace(day=28)+dt.timedelta(days=4)).replace(day=1)
    working=copy.deepcopy(state)
    for param,period in periods:
        attempts=3 if param=='pubDay' else 1
        for attempt in range(attempts):
            candidate=copy.deepcopy(working)
            try:
                bodies=[]
                for fmt in ('csv.zip','eforms.zip'):
                    path=Path(bootstrap_cache)/f'{period}-{fmt}' if bootstrap_cache and param=='pubMonth' else None
                    bodies.append(path.read_bytes() if path and path.exists() else request_bytes(f'{API}?{param}={period}&format={fmt}'))
                count=apply_export(candidate,*bodies)
            except (IncompleteExportError, URLError, TimeoutError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
                if attempt+1==attempts: raise
                delay=(15,45)[attempt]
                print(f'{period}: {exc}; retrying same daily export in {delay}s '
                      f'(attempt {attempt+2}/{attempts})',flush=True)
                time.sleep(delay)
            else:
                break
        working.clear();working.update(candidate)
        imports.append({'period':period,'source_period':period,'notices':count,
                        'sha256':[hashlib.sha256(b).hexdigest() for b in bodies]})
        print(f'öffentlichevergabe: {period}, {count} Bekanntmachungen',flush=True)
    working['last_day']=through.isoformat()
    known_procedures={n['procedure'] for n in working['notices'].values() if n['procedure']}
    working['outcomes']={key:v for key,v in working['outcomes'].items() if key.rsplit('|',1)[0] in known_procedures}
    state.clear();state.update(working)
    return imports

def external_url(url):
    p=urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname:return ''
    params=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('fbclid','gclid')]
    return urlunsplit(('https',p.netloc.lower(),p.path.rstrip('/'),urlencode(sorted(params)),''))

def service_row(entry):
    body=source_content(entry); soup=BeautifulSoup(body,'html.parser')
    lines=soup.get_text('\n',strip=True)
    def field(label):
        m=re.search(re.escape(label)+r'\s*:\s*([^\n]+)',lines,re.I)
        return clean(m.group(1)) if m else ''
    end=offer_deadline(entry)
    pub=entry.get('published') or entry.get('updated') or ''
    try: pub=parsedate_to_datetime(pub).isoformat()
    except (ValueError,TypeError): pub=''
    return dict(title=clean(html.unescape(entry.get('title',''))),url=entry.get('link',''),buyer=field('Vergabestelle'),
        location=field('Erfüllungsort'),description=soup.get_text(' ',strip=True),deadline=end.isoformat() if end else '',
        deadline_kind='Teilnahmefrist' if 'Teilnahmefrist' in lines else 'Angebotsfrist',deadline_has_time=True,
        publication=pub,source='service.bund.de',groups=['service.bund.de'],kind='competition',lot_id='',notice_id='',
        documents=[a.get('href','') for a in soup.find_all('a',href=True)],service_body=body)

def fetch_service(url):
    body=request_bytes(url)
    # HTML error pages and malformed RSS must not count as a successful empty feed.
    root=ET.fromstring(body)
    if root.tag.split('}')[-1] not in ('rss','feed'):raise ValueError('Source returned no RSS/Atom feed')
    parsed=feedparser.parse(body)
    return [service_row(e) for e in parsed.entries if e.get('link') and external_url(e['link'])]

def refresh_service(state, urls, now, previous_feed):
    if not state['service'] and previous_feed.exists():
        for entry in feedparser.parse(previous_feed.read_bytes()).entries:
            if entry.get('link') and 'oeffentlichevergabe.de' not in entry.link:
                row=service_row(entry);state['service'][canonical_key({'link':row['url']})]=row
    with ThreadPoolExecutor(max_workers=5) as pool:
        # Propagate any source error. No persistent files have been touched yet.
        results=list(pool.map(fetch_service,urls))
    for rows in results:
        for row in rows:
            state['service'][canonical_key({'link':row['url']})]=row
    state['service']={key:row for key,row in state['service'].items() if row['deadline'] and dt.datetime.fromisoformat(row['deadline'])>now}
    print(f'service.bund.de: {len(urls)} Feeds gelesen, {len(state["service"])} offene Einträge gespeichert',flush=True)

def ov_rows(state, now):
    rows=[]
    replaced=set(state['superseded'])
    for nid,n in state['notices'].items():
        for original in n['entries']:
            row=dict(original);row['source']='öffentlichevergabe.de'
            row['rating'],row['reason']=relevance(row['title'],row['description'],row['kind'])
            # Apply established exclusions as well, except site logistics which remain reviewable.
            from optirent_filter import UNRELATED_TRADES
            if UNRELATED_TRADES.search(row['title']) and not re.search('baulogistik|baustelleneinrichtung',row['title'],re.I):
                row['rating']='Nicht passend';row['reason']='Separat ausgeschriebenes Nebengewerk.'
            end=dt.datetime.fromisoformat(row['deadline']) if row['deadline'] else None
            outcome=state['outcomes'].get(n['procedure']+'|'+row['lot_id'])
            if nid in replaced: row['status']='Ersetzt'
            elif row['kind'] not in ('competition','planning','consultation'):row['status']='Archiv'
            elif end and end<=now:row['status']='Abgelaufen'
            elif outcome and outcome['publication']>=row['publication'] and outcome['decision'] in ('selec-w','clos-nw'):
                row['status']='Abgeschlossen'
            elif not end:
                row['status']='Status prüfen';row['rating']='Prüfen';row['reason']+=' Keine auslesbare Angebots-/Teilnahmefrist.'
            else:row['status']='Aktuell'
            rows.append(row)
    return rows

def norm(s):
    return re.sub(r'[^\w]+','',unicodedata.normalize('NFKC',html.unescape(s)).casefold())

def signature(row):
    # No title-only matching, and no merging of different lots inside one source.
    if not row['buyer'] or not row['deadline']:return None
    return (norm(row['title']),norm(row['buyer']),dt.datetime.fromisoformat(row['deadline']).isoformat())

def merge_rows(ov, service, now):
    active=[dict(r,sources=[r['source']],links=[r['url']],guid='ov:'+r['notice_id']+':'+r['lot_id']) for r in ov if r['status'] in ACTIVE and r['rating']!='Nicht passend']
    by_signature={};by_document={}
    for row in active:
        if signature(row):by_signature.setdefault(signature(row),[]).append(row)
        for link in [row['url']]+row['documents']:
            if external_url(link):by_document.setdefault(external_url(link),[]).append(row)
    # Known closed/non-relevant original offers suppress exact stale cross-source copies.
    blocked={signature(r) for r in ov if r['status'] in ('Abgeschlossen','Abgelaufen') or r['rating']=='Nicht passend'}-{None}
    merged=0
    for source in service:
        row=dict(source)
        if not keep_entry({'title':row['title'],'link':row['url'],'description':row.get('service_body','')},now=now)[0]:continue
        if signature(row) in blocked and signature(row) not in by_signature:continue
        candidates=by_signature.get(signature(row),[]) if signature(row) else []
        if not candidates:
            found={id(r):r for url in [row['url']]+row['documents'] for r in by_document.get(external_url(url),[])}
            candidates=list(found.values())
        if len(candidates)==1:
            target=candidates[0]
            if row['url'] not in target['links']:target['links'].append(row['url'])
            if row['source'] not in target['sources']:target['sources'].append(row['source'])
            # Preserve the original RSS identity when enriching an existing service.bund item.
            target['guid']=canonical_key({'link':row['url']});merged+=1
        else:
            row.update(status='Aktuell',rating='Prüfen',reason='Treffer aus den bestehenden service.bund.de-Suchfeeds.',sources=[row['source']],links=[row['url']],guid=canonical_key({'link':row['url']}))
            active.append(row)
    active.sort(key=lambda r:(r['rating']!='Passend',r['deadline'] or '9999',r['title']))
    return active,merged

def deadline_label(r):
    if not r['deadline']:return 'Keine Frist angegeben – prüfen'
    value=dt.datetime.fromisoformat(r['deadline']).astimezone(BERLIN)
    return value.strftime('%d.%m.%Y %H:%M') if r['deadline_has_time'] else value.strftime('%d.%m.%Y')+' (ohne Uhrzeit)'

def description_html(r):
    e=html.escape
    parts=[f'<p><b>{e(r["rating"])}</b> – {e(r["reason"])}</p>',f'<p>Vergabestelle: {e(r["buyer"])}<br>Erfüllungsort: {e(r["location"])}<br>{e(r["deadline_kind"] or "Frist")}: {e(deadline_label(r))}</p>',f'<p>{e(r["description"])}</p>',f'<p>Quellen: {e(", ".join(r["sources"]))}<br>Suchgruppen: {e(", ".join(r["groups"]))}</p>']
    parts+=['<p><a href="'+e(url,quote=True)+'">Originalbekanntmachung</a></p>' for url in r['links'] if external_url(url)]
    return ''.join(parts)

def rss_bytes(rows, now, source_warning=''):
    root=ET.Element('rss',version='2.0');ch=ET.SubElement(root,'channel')
    ET.SubElement(ch,'title').text='OPTIRENT – Aktuelle Ausschreibungen'
    ET.SubElement(ch,'link').text=PUBLIC
    ET.SubElement(ch,'description').text=('Container- und Modulbau: service.bund.de und öffentlichevergabe.de. Prüffälle sind gekennzeichnet.'+(' '+source_warning if source_warning else ''))
    ET.SubElement(ch,'lastBuildDate').text=format_datetime(now)
    for r in rows:
        item=ET.SubElement(ch,'item');ET.SubElement(item,'title').text=f'[{r["rating"]}] {r["title"]}'
        ET.SubElement(item,'link').text=r['url'];ET.SubElement(item,'guid',isPermaLink='false').text=r['guid']
        ET.SubElement(item,'description').text=description_html(r)
        if r['publication']:
            ET.SubElement(item,'pubDate').text=format_datetime(dt.datetime.fromisoformat(r['publication'].replace('Z','+00:00')))
    data=ET.tostring(root,encoding='utf-8',xml_declaration=True);ET.fromstring(data)
    if len({r['guid'] for r in rows})!=len(rows):raise ValueError('Duplicate RSS identities')
    return data

def page_html(rows, status):
    e=html.escape
    warning=('<p role="alert"><strong>Achtung:</strong> Der aktuelle eForms-Import ist fehlgeschlagen. '
             'öffentlichevergabe.de ist nur bis zum '+e(str(status['through']))
             +' vollständig importiert. service.bund.de wurde neu geprüft.</p>'
             if status.get('ov_status')=='stale' else '')
    cards=''.join(f'<article data-rating="{e(r["rating"])}"><small>{e(r["rating"])} · {e(", ".join(r["sources"]))}</small><h2><a href="{e(r["url"],quote=True)}">{e(r["title"])}</a></h2><p><b>{e(r["buyer"])}</b> · {e(r["location"])}</p><p><b>{e(r["deadline_kind"] or "Frist")}: {e(deadline_label(r))}</b></p><details><summary>Details und Quellen</summary>{description_html(r)}</details></article>' for r in rows)
    return ('''<!doctype html><html lang="de"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>OPTIRENT Ausschreibungsradar</title><style>body{font:16px system-ui;background:#f1f5f7;color:#173442;margin:0}main{max-width:1100px;margin:auto;padding:28px}a{color:#00607c}article{padding:22px;background:white;border:1px solid #d1dfe6;border-radius:9px;margin:16px 0}h2{font-size:21px}input,select{font:inherit;padding:10px;margin:5px;border:1px solid #9badb4;border-radius:5px}input{width:50%;min-width:220px}small{color:#4b6875}details{overflow-wrap:anywhere}[hidden]{display:none}</style><main><h1>OPTIRENT Ausschreibungsradar</h1>'''+f'<p>Letzte erfolgreiche Aktualisierung: {e(status["updated_at_display"])} · {len(rows)} Chancen</p><p>öffentlichevergabe.de: Veröffentlichungen bis {e(status["through"])}. Neue Veröffentlichungen erscheinen im Export am Folgetag. Automatische Vorauswahl; Angaben in den Originalunterlagen prüfen.</p>'+warning+'''<p><a href="optirent-ausschreibungen.xml">RSS-Feed abonnieren</a> · <a href="radar-status.json">Abrufstatus</a></p><input id="search" aria-label="Treffer durchsuchen" placeholder="Titel, Ort oder Vergabestelle"><select id="rating" aria-label="Einstufung"><option value="">Alle Chancen</option><option>Passend</option><option>Prüfen</option></select><p id="count"></p>'''+cards+'''<footer>„Prüfen“ umfasst unklare Leistungen sowie Vorinformationen oder Einträge ohne gesicherte Frist. Ältere offene Bekanntmachungen vor dem erstmaligen Import können fehlen.</footer></main><script>const q=document.querySelector('#search'),s=document.querySelector('#rating');function apply(){let n=0;for(const a of document.querySelectorAll('article')){a.hidden=!!((s.value&&a.dataset.rating!==s.value)||(q.value&&!a.textContent.toLocaleLowerCase('de').includes(q.value.toLocaleLowerCase('de'))));if(!a.hidden)n++}document.querySelector('#count').textContent=n+' Treffer'}q.addEventListener('input',apply);s.addEventListener('change',apply);apply();</script></html>''').encode('utf-8')

def publish(state, rows, status, root):
    source_warning=('ACHTUNG: öffentlichevergabe.de ist nur bis zum '+str(status['through'])
                    +' vollständig importiert; service.bund.de ist aktuell.'
                    if status.get('ov_status')=='stale' else '')
    payloads={'docs/optirent-ausschreibungen.xml':rss_bytes(rows,dt.datetime.fromisoformat(status['updated_at']),source_warning),
              'docs/index.html':page_html(rows,status),
              'docs/radar-status.json':json.dumps(status,ensure_ascii=False,indent=2).encode(),
              'data/radar-state.json':json.dumps(state,ensure_ascii=False,separators=(',',':')).encode()}
    # Render and validate everything before replacement. Workflow commits only on total success.
    with tempfile.TemporaryDirectory(dir=root) as staging:
        for i,(name,data) in enumerate(payloads.items()):
            Path(staging,str(i)).write_bytes(data)
        for i,name in enumerate(payloads):
            target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
            os.replace(Path(staging,str(i)),target)

def run(root=Path('.'), bootstrap_cache=None):
    root=Path(root);now=dt.datetime.now(BERLIN);through=now.date()-dt.timedelta(days=1)
    cfg=configparser.ConfigParser();cfg.read(root/'config.ini',encoding='utf-8')
    urls=[u.strip() for u in cfg.get('source001','url').strip('"').split(',') if u.strip()]
    state_path=root/'data/radar-state.json'
    state=json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else initial_state()
    if state.get('schema')!=1:raise ValueError('Unsupported state schema')
    ov_candidate=copy.deepcopy(state)
    ov_error=None
    try:
        imports=refresh_ov(ov_candidate,through,'2026-09-01',bootstrap_cache)
    except (IncompleteExportError, URLError, TimeoutError, zipfile.BadZipFile, ET.ParseError, KeyError) as exc:
        # On the next run the unchanged watermark causes all missed days to replay.
        imports=[]
        ov_error=f'{type(exc).__name__}: {exc}'
        print(f'WARNUNG: öffentlichevergabe.de unvollständig: {ov_error}; '
              'verwende den letzten verifizierten Stand.',flush=True)
    else:
        state=ov_candidate
    # Fresh service.bund.de results must not be blocked by an eForms outage.
    # If service.bund.de also fails, publish nothing.
    refresh_service(state,urls,now,root/'docs/optirent-ausschreibungen.xml')
    rows,merged=merge_rows(ov_rows(state,now),state['service'].values(),now)
    status={'updated_at':now.isoformat(),'updated_at_display':now.strftime('%d.%m.%Y %H:%M %Z'),
            'through':state.get('last_day') or 'nicht verfügbar',
            'ov_target_through':through.isoformat(),'ov_status':'stale' if ov_error else 'complete',
            'ov_error':ov_error,
            'bootstrap_since':'2026-09-01','items':len(rows),'passend':sum(r['rating']=='Passend' for r in rows),
            'pruefen':sum(r['rating']=='Prüfen' for r in rows),
            'cross_source_duplicates_merged':merged,'sources':['service.bund.de','öffentlichevergabe.de'],
            'imports':imports,
            'limitations':['Not a complete pre-September-2026 active inventory',
                           'Uncertain cross-source duplicates remain separate',
                           'Not all withdrawal notices carry explicit lot outcomes']
                           +(['öffentlichevergabe.de import is stale; verified watermark was preserved'] if ov_error else [])}
    publish(state,rows,status,root)
    print(json.dumps(status,ensure_ascii=False,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--bootstrap-cache');args=parser.parse_args()
    run(bootstrap_cache=args.bootstrap_cache)
