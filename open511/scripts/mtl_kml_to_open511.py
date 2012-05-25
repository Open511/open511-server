#coding: utf-8
"""
A script to convert Ville de Montreal roadwork KML files, e.g. from
http://depot.ville.montreal.qc.ca/info-travaux/data.kml
to an Open511 XML file.
"""

import datetime
import hashlib
import re
import sys

from django.contrib.gis.gdal import DataSource
from lxml import etree
import lxml.html

from open511.utils.serialization import roadevent_to_xml_element, get_base_open511_element

JURISDICTION = 'converted.ville.montreal.qc.ca'

ids_seen = set()

class DummyRoadEvent(object):
    pass

def feature_to_open511_element(feature):
    """Transform an OGR Feature from the KML input into an XML Element for a RoadEvent."""
    rdev = DummyRoadEvent()

    rdev.geom = feature.geom

    # Using a hash of the geometry for an ID. For proper production use,
    # there'll probably have to be some code in the importer
    # that compares to existing entries in the DB to determine whether
    # this is new or modified...
    geom_hash = hashlib.md5(feature.geom.wkt).hexdigest()
    rdev.source_id = JURISDICTION + ':' + geom_hash
    while rdev.source_id in ids_seen:
        rdev.source_id += 'x'
    ids_seen.add(rdev.source_id)

    rdev.title = feature.get('Name').decode('utf8')

    blob = lxml.html.fragment_fromstring(feature.get('Description').decode('utf8'),
        create_parent='content')

    description_label = blob.xpath('//strong[text()="Description"]')
    if description_label:
        description_bits = []
        el = description_label[0].getnext()
        while el.tag == 'p':
            description_bits.append(_get_el_text(el))
            el = el.getnext()
        rdev.description = '\n\n'.join(description_bits)

    localisation = blob.cssselect('div#localisation p')
    if localisation:
        rdev.affected_roads = '\n\n'.join(_get_el_text(el) for el in localisation)
        
    try:
        rdev.external_url = blob.cssselect('#avis_residants a, #en_savoir_plus a')[0].get('href')
    except IndexError:
        pass
        
    facultatif = blob.cssselect('div#itineraire_facult p')
    if facultatif:
        rdev.detour = '\n\n'.join(_get_el_text(el) for el in facultatif)

    if blob.cssselect('div#dates strong'):
        try:
            start_date = blob.xpath(u'div[@id="dates"]/strong[text()="Date de d\xe9but"]')[0].tail
            end_date = blob.xpath(u'div[@id="dates"]/strong[text()="Date de fin"]')[0].tail
            if start_date and end_date:
                rdev.start_date = _fr_string_to_date(start_date)
                rdev.end_date = _fr_string_to_date(end_date)
        except IndexError:
            pass

    return roadevent_to_xml_element(rdev)

def kml_file_to_open511_element(filename):
    """Transform a Montreal KML file, at filename, into an Element
    for the top-level <open511> element."""
    ds = DataSource(filename)
    base_element = get_base_open511_element()
    for layer in ds:
        for feature in layer:
            base_element.append(feature_to_open511_element(feature))
    return base_element

def _get_el_text(el):
    t = el.text if el.text else ''
    for subel in el:
        t += _get_el_text(subel)
        if subel.tail:
            t += subel.tail
    return t

FR_MONTHS = {
    'janvier': 1,
    u'février': 2,
    'mars': 3,
    'avril': 4,
    'mai': 5,
    'juin': 6,
    'juillet': 7,
    u'août': 8,
    'septembre': 9,
    'octobre': 10,
    'novembre': 11,
    u'décembre': 12
}

fr_date_re = re.compile(ur'(\d\d?) (%s) (\d{4})' % '|'.join(FR_MONTHS.keys()))

def _fr_string_to_date(s):
    match = fr_date_re.search(s)
    if not match:
        return None
    return datetime.date(
        int(match.group(3)),
        FR_MONTHS[match.group(2)],
        int(match.group(1))
    )


def main():
    filename = sys.argv[1]
    el = kml_file_to_open511_element(filename)
    print etree.tostring(el, pretty_print=True)

if __name__ == '__main__':
    main()