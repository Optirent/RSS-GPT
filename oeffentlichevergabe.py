"""eForms parsing and the five OPTIRENT public-procurement search filters."""
import argparse
import collections
import csv
import datetime as dt
import hashlib
import html
import io
import json
from pathlib import Path
import re
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo('Europe/Berlin')
GROUPS = {
    '1 Miet-/Modulcontainer': 'Mietcontainer Modulcontainer Containergebäude Raummodule Interimsgebäude Kindergartencontainer'.split(),
    '2 Containeranlagen': 'Containeranlage Containeranlagen Bürocontainer Schulcontainer Sanitärcontainer Raumcontainer'.split(),
    '3 Interimscontainer': 'Interimscontainer Containerdorf Raumzellen Interimsschule'.split(),
    '4 Interimsgebäude': 'Interimsgebäude Ersatzgebäude Behelfsgebäude Interimslösung'.split(),
}
NS = {'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
      'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
      'efac': 'http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1'}

def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()

def txt(el, path):
    return clean(el.findtext(path, '', NS)) if el is not None else ''

def fulltext(el):
    return clean(' '.join(el.itertext())) if el is not None else ''

def matching_groups(text, cpvs):
    text = text.casefold()
    found = [name for name, words in GROUPS.items() if any(w.casefold() in text for w in words)]
    if any(code.startswith('442111') for code in cpvs):
        found.append('5 CPV 442111')
    return found

def deadline(process):
    """Never use opening dates, validity dates or contract end as bid deadline."""
    values = []
    for tag, label in [('ParticipationRequestReceptionPeriod', 'Teilnahmefrist'), ('TenderSubmissionDeadlinePeriod', 'Angebotsfrist')]:
        for p in process.findall('.//cac:' + tag, NS) if process is not None else []:
            date, clock = txt(p, 'cbc:EndDate'), txt(p, 'cbc:EndTime')
            if not date:
                continue
            precision = bool(clock)
            offset = re.search(r'(Z|[+-]\d\d:\d\d)$', clock or date)
            value = date[:10] + 'T' + (clock[:8] if clock else '23:59:59')
            if offset:
                value += offset.group(1)
            try:
                parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=BERLIN)
                values.append((parsed, label, precision))
            except ValueError:
                continue
    return min(values, key=lambda x:x[0]) if values else (None, '', False)

ANCILLARY = re.compile(r'objektplanung|tragwerksplanung|freianlagenplanung|projektsteuerung|gebäudeautomation|\belektro\b|elektroplanung|tga.planung|hls.planung|medientechnik|schadstoffsanierung|rohbauarbeiten|bodenbelagsarbeiten|akustikdecken|(?:lieferung|montage).*inventar|erd.und.gründungsarbeiten', re.I)
CONTAINER = re.compile(r'(?:büro|schul|sanitär|sozial|raum|miet|modul|spül|wc.)?container|raummodul|raumzelle|modulgebäude|modulbau', re.I)
ACTION = re.compile(r'miet|liefer|erricht|aufstell|kauf|beschaff|containersystem', re.I)

def relevance(title, description, kind):
    if kind == 'planning':
        return 'Prüfen', 'Vorinformation/Markterkundung: frühe Projektchance, keine gesicherte Angebotsphase.'
    if ANCILLARY.search(title):
        return 'Nicht passend', 'Titel bezeichnet ein separates Planungs- oder Nebengewerk; manuell übersteuerbar.'
    if re.search(r'transport.container|recycling|sortierstation', title, re.I):
        return 'Nicht passend', 'Titel beschreibt Transport- oder Verfahrenstechnik statt Raumgebäuden.'
    if re.search(r'baustelleneinrichtung|baulogistik', title, re.I):
        return 'Prüfen', 'Baustelleneinrichtung kann relevante Containerleistungen enthalten.'
    if CONTAINER.search(title) and ACTION.search(title):
        return 'Passend', 'Container-/Modulgebäude und Lieferung, Miete oder Errichtung im Titel.'
    return 'Prüfen', 'Suchbegriff oder CPV passt; konkrete Raumcontainerleistung muss geprüft werden.'

def parse_notice(data, publication='', root=None):
    if root is None:
        root = ET.fromstring(data)
    nid, version = txt(root, 'cbc:ID'), txt(root, 'cbc:VersionID')
    typ = root.find('cbc:NoticeTypeCode', NS)
    kind = typ.get('listName', '') if typ is not None else ''
    procedure = txt(root, 'cbc:ContractFolderID')
    orgs = {}
    for org in root.findall('.//efac:Company', NS):
        orgs[txt(org, 'cac:PartyIdentification/cbc:ID')] = txt(org, 'cac:PartyName/cbc:Name')
    buyers = [orgs.get(txt(p, 'cac:Party/cac:PartyIdentification/cbc:ID'), '') or txt(p, 'cac:Party/cac:PartyName/cbc:Name') for p in root.findall('cac:ContractingParty', NS)]
    project = root.find('cac:ProcurementProject', NS)
    lots = root.findall('cac:ProcurementProjectLot', NS)
    # Full-notice search follows the user's broad search intent. Preserve lot-level scope.
    notice_text = fulltext(root)
    project_cpvs = [clean(e.text) for e in project.findall('.//cbc:ItemClassificationCode', NS)] if project is not None else []
    results = []
    for lot in lots or [root]:
        lotid = txt(lot, 'cbc:ID') if lots else ''
        lp = lot.find('cac:ProcurementProject', NS)
        title = txt(lp, 'cbc:Name') or txt(project, 'cbc:Name')
        description = txt(lp, 'cbc:Description') or txt(project, 'cbc:Description')
        codes = sorted(set(project_cpvs + [clean(e.text) for e in lot.findall('.//cbc:ItemClassificationCode', NS)]))
        groups = matching_groups(notice_text, codes)
        if not groups:
            continue
        process = lot.find('cac:TenderingProcess', NS)
        if process is None:
            process = root.find('cac:TenderingProcess', NS)
        end, endkind, exact = deadline(process)
        score, reason = relevance(title, description, kind)
        addresses = lp.findall('cac:RealizedLocation/cac:Address', NS) if lp is not None else []
        if not addresses and project is not None:
            addresses = project.findall('cac:RealizedLocation/cac:Address', NS)
        location = '; '.join(filter(None, [clean(' '.join([txt(a,'cbc:PostalZone'),txt(a,'cbc:CityName'),txt(a,'cbc:CountrySubentityCode')])) for a in addresses]))
        docs = [clean(e.text) for e in lot.findall('.//cac:CallForTendersDocumentReference//cbc:URI', NS)]
        params = {'noticeId': nid}
        if lotid:
            params['lotId'] = lotid
        results.append(dict(notice_id=nid, version=version, procedure_id=procedure, lot_id=lotid,
            publication=publication, kind=kind, notice_type=clean(typ.text) if typ is not None else '',
            title=title, buyer='; '.join(filter(None,buyers)), location=location, description=description,
            deadline=end.isoformat() if end else '', deadline_kind=endkind, deadline_has_time=exact,
            groups=groups, cpv=codes, rating=score, reason=reason, documents=docs,
            url='https://oeffentlichevergabe.de/ui/de/search/details?'+urllib.parse.urlencode(params),
            fingerprint=hashlib.sha256(data).hexdigest()))
    return nid, version, procedure, kind, results
