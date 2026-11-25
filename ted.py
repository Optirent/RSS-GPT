"""TED search import, with bounded time windows and all-or-nothing watermarks."""
import copy
import datetime as dt
import json
import re
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import xml.etree.ElementTree as ET

API = 'https://api.ted.europa.eu/v3/notices/search'


class IncompleteTED(ValueError):
    pass


def terms_from_urls(urls):
    terms = [parse_qs(urlsplit(u).query).get('templateQueryString', [''])[0] for u in urls]
    if len(terms) != 25 or any(not t for t in terms):
        raise ValueError('Expected 25 configured service.bund.de search terms')
    return terms


def query_for(day, terms):
    # Separate full text expressions avoid treating the set as a phrase.
    text = ' OR '.join('FT ~ "' + t.replace('"', '') + '"' for t in terms)
    return (f'place-of-performance = DEU AND publication-date = {day:%Y%m%d} AND '
            f'(classification-cpv = 442111* OR {text})')


def request_with_retry(request):
    for attempt in range(6):
        try:
            with urlopen(request, timeout=90) as response:
                data=response.read()
            time.sleep(0.5)
            return data
        except HTTPError as exc:
            if exc.code not in (429,500,502,503,504) or attempt==5: raise
            retry=exc.headers.get('Retry-After','')
            delay=min(60,int(retry)) if retry.isdigit() else min(30,2**(attempt+1))
            time.sleep(delay)


def post_json(payload):
    request = Request(API, json.dumps(payload).encode(),
                      {'Content-Type':'application/json', 'User-Agent':'OPTIRENT-Ausschreibungsradar/1.0'})
    return json.loads(request_with_retry(request))


def fetch_xml(url):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != 'ted.europa.eu' or not re.fullmatch(r'/en/notice/\d+-\d{4}/xml', parts.path):
        raise IncompleteTED('Unexpected TED XML URL')
    return request_with_retry(Request(url, headers={'User-Agent':'OPTIRENT-Ausschreibungsradar/1.0'}))


def search_day(day, terms, post=post_json):
    request = dict(query=query_for(day,terms), fields=['publication-number','publication-date',
                    'notice-identifier','notice-type'], scope='ALL', limit=250,
                   paginationMode='ITERATION')
    output=[]; tokens=set(); expected=None
    while True:
        result=post(request)
        if result.get('timedOut') is not False or not isinstance(result.get('notices'),list):
            raise IncompleteTED('TED search timed out or response incomplete')
        count=result.get('totalNoticeCount')
        if not isinstance(count,int) or (expected is not None and count != expected):
            raise IncompleteTED('TED count missing or changed during paging')
        expected=count
        output.extend(result['notices'])
        token=result.get('iterationNextToken')
        if len(output)==expected:
            break
        if len(output)>expected or not token or token in tokens:
            raise IncompleteTED('TED pagination incomplete or repeated')
        tokens.add(token);request['iterationNextToken']=token
    ids=[n.get('publication-number') for n in output]
    if not all(ids) or len(set(ids))!=len(ids):
        raise IncompleteTED('Missing or duplicate TED publication number')
    return output


def refresh(state, through, terms, ingest, initial_state, post=post_json, fetch=fetch_xml,
            start=dt.date(2026,11,6)):
    candidate=copy.deepcopy(state.get('ted') or {'last_day':None,'state':initial_state()})
    last=candidate['last_day']
    if last and through < dt.date.fromisoformat(last):
        raise IncompleteTED('Refusing to move TED watermark backwards')
    first=max(start,dt.date.fromisoformat(last)-dt.timedelta(days=2)) if last else start
    count=0
    for offset in range((through-first).days+1):
        day=first+dt.timedelta(days=offset)
        notices=search_day(day,terms,post)
        for notice in notices:
            nid=notice.get('notice-identifier'); pub=notice.get('publication-date','')
            xml_url=notice.get('links',{}).get('xml',{}).get('MUL')
            if not nid or not xml_url or pub[:10] != day.isoformat():
                raise IncompleteTED('TED metadata or publication date missing')
            data=fetch(xml_url); root=ET.fromstring(data)
            rawid=root.findtext('{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID')
            if not rawid or rawid.casefold()!=nid.casefold():
                raise IncompleteTED('TED XML identifier does not match search result')
            # Only advance after every search result and its XML are validated.
            ingest(candidate['state'],data,day.isoformat()+'T00:00:00+00:00',root,matched_by_source=True)
            n=candidate['state']['notices'].get(nid)
            xml_version=root.findtext('{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}VersionID') or ''
            if n and n['version']==xml_version and n['publication']==day.isoformat()+'T00:00:00+00:00':
                for row in n['entries']:
                    row['ted_url']='https://ted.europa.eu/de/notice/-/detail/'+notice['publication-number']
            count+=1
    candidate['last_day']=through.isoformat()
    state['ted']=candidate
    return count
