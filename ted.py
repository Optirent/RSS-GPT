"""TED search import, with bounded time windows and all-or-nothing watermarks."""
import copy
import datetime as dt
import json
import io
import re
import tarfile
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
    for attempt in range(4):
        body=request_with_retry(Request(url, headers={'User-Agent':'OPTIRENT-Ausschreibungsradar/1.0'}))
        try:
            ET.fromstring(body)
            return body
        except ET.ParseError as exc:
            if attempt==3:
                raise IncompleteTED(f'Invalid or empty TED XML for {url}: {exc}') from exc
            time.sleep(3*(attempt+1))


def search_day(day, terms, post=post_json):
    request = dict(query=query_for(day,terms), fields=['publication-number','publication-date',
                    'notice-identifier','notice-type','ojs-number'], scope='ALL', limit=250,
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


def package_xml(notice, cache):
    """Read one exact notice from the official daily archive, without extraction."""
    edition=notice.get('ojs-number','')
    number=notice.get('publication-number','')
    match=re.fullmatch(r'(\d{1,5})/(\d{4})', edition)
    pub=re.fullmatch(r'(\d{1,8})-(\d{4})', number)
    if not match or not pub or match.group(2)!=pub.group(2):
        raise IncompleteTED('Missing or invalid TED edition/publication number')
    key=match.group(2)+match.group(1).zfill(5)
    if key not in cache:
        url='https://ted.europa.eu/packages/daily/'+key
        body=request_with_retry(Request(url,headers={'User-Agent':'OPTIRENT-Ausschreibungsradar/1.0'}))
        if not body or len(body)>150_000_000:
            raise IncompleteTED('TED daily package empty or too large: '+key)
        cache[key]=body
    filename=pub.group(1).zfill(8)+'_'+pub.group(2)+'.xml'
    try:
        with tarfile.open(fileobj=io.BytesIO(cache[key]),mode='r:gz') as archive:
            matches=[m for m in archive.getmembers() if m.isfile() and m.name.rsplit('/',1)[-1]==filename]
            if len(matches)!=1 or matches[0].size>20_000_000:
                raise IncompleteTED('Missing, duplicate or oversized TED XML in daily package: '+number)
            data=archive.extractfile(matches[0]).read()
            ET.fromstring(data)
            return data
    except (tarfile.TarError,EOFError,OSError,ET.ParseError) as exc:
        raise IncompleteTED('Invalid TED daily package or XML: '+key) from exc


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
        package_cache={}; use_package=False
        for notice in notices:
            nid=notice.get('notice-identifier'); pub=notice.get('publication-date','')
            xml_url=notice.get('links',{}).get('xml',{}).get('MUL')
            if not nid or not xml_url or pub[:10] != day.isoformat():
                raise IncompleteTED('TED metadata or publication date missing')
            if use_package:
                data=package_xml(notice,package_cache)
            else:
                try:
                    data=fetch(xml_url); ET.fromstring(data)
                except IncompleteTED:
                    data=package_xml(notice,package_cache)
                    use_package=True
            root=ET.fromstring(data)
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
