from copy import deepcopy
import datetime
from urlparse import urljoin

from django.conf import settings
from django.contrib.gis.db import models
from django.contrib.gis.geos import fromstr as geos_geom_from_string
from django.core import urlresolvers
from django.core.exceptions import ObjectDoesNotExist
from django.utils.translation import ugettext_lazy as _
from django.utils.timezone import utc

import dateutil.parser
from lxml import etree
from lxml.builder import E
import requests

from open511.fields import XMLField
from open511.utils.postgis import gml_to_ewkt
from open511.utils.serialization import (ELEMENTS, ELEMENTS_LOOKUP,
    geom_to_xml_element, XML_LANG, ATOM_LINK, XMLModelMixin)
from open511.utils.http import DEFAULT_ACCEPT_LANGUAGE


class _Open511Model(models.Model):

    created = models.DateTimeField(default=lambda: datetime.datetime.now(utc))
    updated = models.DateTimeField(default=lambda: datetime.datetime.now(utc))

    @property
    def url(self):
        if getattr(self, 'external_url', None):
            return self.external_url
        return self.get_absolute_url()

    @property
    def full_url(self):
        url = self.url
        if url.startswith('/'):
            return settings.OPEN511_BASE_URL + url
        return url

    def save(self, *args, **kwargs):
        self.updated = datetime.datetime.now(utc)
        return super(_Open511Model, self).save(*args, **kwargs)

    class Meta(object):
        abstract = True


class JurisdictionManager(models.Manager):

    def get_or_create_from_url(self, url):
        try:
            return self.get(external_url=url)
        except ObjectDoesNotExist:
            if url.startswith(settings.OPEN511_BASE_URL):
                slug = filter(None, url.split('/'))[-1]
                try:
                    return self.get(slug=slug)
                except ObjectDoesNotExist:
                    pass

        # Looks like we need to create a new jurisdiction
        req = requests.get(url)
        root = etree.fromstring(req.content)
        jur = root.xpath('jurisdiction')[0]
        self_url = jur.xpath('atom:link[@rel="self"]/@href', namespaces=jur.nsmap)[0]
        if self_url != url:
            return self.get_or_create_from_url(self_url)

        return self.update_or_create_from_xml(jur)

    def update_or_create_from_xml(self, xml_jurisdiction):
        self_link = xml_jurisdiction.xpath('atom:link[@rel="self"]',
            namespaces=xml_jurisdiction.nsmap)[0]
        try:
            jur = self.get(external_url=self_link.get('href'))
        except ObjectDoesNotExist:
            slug = filter(None, self_link.get('href').split('/'))[-1]
            if self.filter(slug=slug).exists():
                raise Exception(u"There's already a jurisdiction with slug %s, but with URL %s instead of %s."
                    % (slug, self.get(slug=slug).full_url, self_link.get('href')))
            jur = self.model(
                external_url=self_link.get('href'),
                slug=slug
            )

            try:
                created = xml_jurisdiction.xpath('creationDate/text()')[0]
                jur.created = dateutil.parser.parse(created)
            except IndexError:
                pass

        for path in ['status', 'creationDate', 'lastUpdate', 'atom:link[@rel="self"]']:
            for elem in xml_jurisdiction.xpath(path, namespaces=xml_jurisdiction.nsmap):
                xml_jurisdiction.remove(elem)
        jur.xml_elem = xml_jurisdiction
        jur.save()
        return jur


class Jurisdiction(_Open511Model, XMLModelMixin):

    slug = models.SlugField()

    external_url = models.URLField(blank=True)

    # geom = models.MultiPolygonField(blank=True, null=True)

    xml_data = XMLField(default='<jurisdiction />')

    objects = JurisdictionManager()

    def __unicode__(self):
        return self.slug

    def get_absolute_url(self):
        return urlresolvers.reverse('open511_jurisdiction', kwargs={'slug': self.slug})

    def save(self, force_insert=False, force_update=False, using=None):
        self.xml_data = etree.tostring(self.xml_elem)
        self.full_clean()
        super(Jurisdiction, self).save(force_insert=force_insert, force_update=force_update,
            using=using)

    def to_full_xml_element(self, accept_language=None):
        el = deepcopy(self.xml_elem)

        link = etree.Element(ATOM_LINK)
        link.set('rel', 'self')
        link.set('href', self.full_url)
        el.insert(0, link)

        el.append(E.creationDate(self.created.isoformat()))
        el.append(E.lastUpdate(self.updated.isoformat()))

        return el


class RoadEventManager(models.Manager):

    def update_or_create_from_xml(self, event,
            default_jurisdiction=None, default_lang=settings.LANGUAGE_CODE, base_url=''):
        # Identify the jurisdiction
        external_jurisdiction = event.xpath('atom:link[@rel="jurisdiction"]',
            namespaces=event.nsmap)
        if external_jurisdiction:
            jurisdiction = Jurisdiction.objects.get_or_create_from_url(external_jurisdiction[0].get('href'))
            event.remove(external_jurisdiction[0])
        elif default_jurisdiction:
            jurisdiction = default_jurisdiction
        else:
            raise Exception("No jurisdiction provided")

        self_link = event.xpath('atom:link[@rel="self"]',
            namespaces=event.nsmap)
        if self_link:
            external_url = urljoin(base_url, self_link[0].get('href'))
            id = filter(None, external_url.split('/'))[-1]
            event.remove(self_link[0])
        else:
            external_url = ''
            id = event.get('id')
        if not id:
            raise Exception("No ID provided")

        try:
            rdev = self.get(id=id, jurisdiction=jurisdiction)
        except RoadEvent.DoesNotExist:
            rdev = self.model(id=id, jurisdiction=jurisdiction, external_url=external_url)

        # Extract the geometry
        geometry = event.xpath('geometry')[0]
        gml = etree.tostring(geometry[0])
        ewkt = gml_to_ewkt(gml, force_2D=True)
        rdev.geom = geos_geom_from_string(ewkt)

        # And regenerate the GML so it's consistent with the PostGIS representation
        event.remove(geometry)
        event.append(E.geometry(geom_to_xml_element(rdev.geom)))

        # Remove the ID from the stored XML (we keep it in the table)
        if 'id' in event.attrib:
            del event.attrib['id']

        status = event.xpath('status')
        if status:
            if status[0].text == 'archived':
                rdev.active = False

        try:
            created = event.xpath('creationDate/text()')[0]
            created = dateutil.parser.parse(created)
            if (not rdev.created) or created < rdev.created:
                rdev.created = created
        except IndexError:
            pass

        for path in ['status', 'creationDate', 'lastUpdate']:
            for elem in event.xpath(path):
                event.remove(elem)

        # Push down the default language if necessary
        if not event.get(XML_LANG):
            event.set(XML_LANG, default_lang)

        rdev.xml_elem = event
        rdev.save()
        return rdev


class RoadEvent(_Open511Model, XMLModelMixin):

    internal_id = models.AutoField(primary_key=True)
    active = models.BooleanField(default=True)

    id = models.CharField(max_length=100, blank=True, db_index=True)
    jurisdiction = models.ForeignKey(Jurisdiction)

    external_url = models.URLField(blank=True, db_index=True)

    geom = models.GeometryField(verbose_name=_('Geometry'))
    xml_data = XMLField(default='<roadEvent />')

    objects = RoadEventManager()

    class Meta(object):
        unique_together = [
            ('id', 'jurisdiction')
        ]

    def save(self, force_insert=False, force_update=False, using=None):
        self.xml_data = etree.tostring(self.xml_elem)
        self.full_clean()
        super(RoadEvent, self).save(force_insert=force_insert, force_update=force_update,
            using=using)
        if not self.id:
            mgr = RoadEvent._default_manager
            if using:
                mgr = mgr.using(using)
            mgr.filter(internal_id=self.internal_id).update(
                id=self.internal_id
            )

    def __unicode__(self):
        return u"%s (%s)" % (self.id, self.jurisdiction)

    def get_absolute_url(self):
        return urlresolvers.reverse('open511_roadevent', kwargs={
            'jurisdiction_slug': self.jurisdiction.slug,
            'id': self.id}
        )

    def prune_languages(self, parent, accept=DEFAULT_ACCEPT_LANGUAGE):
        """Remove all free-text elements that don't find with the provided
        Accept-Language options.

        parent - an lxml Element from which items will be pruned
        accept - a webob AcceptLanguage object"""

        rejects = set()
        for child in parent:
            if len(child):
                self.prune_languages(child, accept=accept)
            elif (child not in rejects
                    and child.tag in ELEMENTS_LOOKUP
                    and ELEMENTS_LOOKUP[child.tag].type == 'TEXT'):
                options = self._get_text_elems(child.tag, root=parent)
                best_option = options.get(accept.best_match(options.keys()))
                rejects |= set(o for o in options.values() if o != best_option)
        for reject in rejects:
            parent.remove(reject)

    def to_full_xml_element(self, accept_language=None):
        el = deepcopy(self.xml_elem)

        el.insert(0, E.status('active' if self.active else 'archived'))

        link = etree.Element(ATOM_LINK)
        link.set('rel', 'jurisdiction')
        link.set('href', self.jurisdiction.full_url)
        el.insert(0, link)

        link = etree.Element(ATOM_LINK)
        link.set('rel', 'self')
        link.set('href', self.url)
        el.insert(0, link)

        el.append(E.creationDate(self.created.isoformat()))
        el.append(E.lastUpdate(self.updated.isoformat()))

        if accept_language:
            self.prune_languages(el, accept_language)

        return el

    @property
    def headline(self):
        return self.get_text_value('headline')
