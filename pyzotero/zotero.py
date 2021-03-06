#!/usr/bin/env python
# -*- coding: utf-8 -*-
# pylint: disable=R0904
"""
zotero.py

Created by Stephan Hügel on 2011-02-28
Copyright Stephan Hügel, 2011

This file is part of Pyzotero.

Pyzotero is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Pyzotero is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Pyzotero. If not, see <http://www.gnu.org/licenses/>.
"""

__author__ = 'urschrei@gmail.com'
__version__ = '0.9.7'


import urllib
import urllib2
import socket
import feedparser
import json
import uuid
import time
import datetime
import re
import pytz
from urlparse import urlparse
import xml.etree.ElementTree as et


import zotero_errors as ze


# Avoid hanging the application if there's no server response
timeout = 30
socket.setdefaulttimeout(timeout)


def ib64_patched(self, attrsD, contentparams):
    """ Patch isBase64 to prevent Base64 encoding of JSON content
    """
    if attrsD.get('mode', '') == 'base64':
        return 0
    if self.contentparams['type'].startswith('text/'):
        return 0
    if self.contentparams['type'].endswith('+xml'):
        return 0
    if self.contentparams['type'].endswith('/xml'):
        return 0
    if self.contentparams['type'].endswith('/json'):
        return 0
    return 0


def token():
    """ Return a unique 32-char write-token
    """
    return str(uuid.uuid4().hex)


def extract_links(doc):
    """ Extract self, first, next, last links from an Atom doc
    """
    extracted = dict()
    try:
        for link in doc['feed']['links'][:-1]:
            url = urlparse(link['href'])
            extracted[link['rel']] = '{0}?{1}'.format(url[2], url[4])
        return extracted
    except KeyError:
        # No links present, because it's a single item
        return None


def etags(incoming):
    """ Return a list of etags parsed out of the XML response
    """
    # Parse Atom as straight XML in order to get the etags FFS
    atom_ns = '{http://www.w3.org/2005/Atom}'
    tree = et.fromstring(incoming)
    try:
        return [entry.attrib['{http://zotero.org/ns/api}etag'] for
            entry in tree.findall('.//{0}content'.format(atom_ns))]
    except KeyError:
        pass


# Override feedparser's buggy isBase64 method until they fix it
feedparser._FeedParserMixin._isBase64 = ib64_patched


def cleanwrap(func):
    """ Wrapper for Zotero._cleanup
    """
    def enc(self, *args):
        """ Send each item to _cleanup() """
        return (func(self, item) for item in args)
    return enc


def retrieve(func):
    """
    Decorator for Zotero read API methods; calls _retrieve_data() and passes
    the result to the correct processor, based on a lookup
    """
    def wrapped_f(self, *args, **kwargs):
        """
        Returns result of _retrieve_data()

        func's return value is part of a URI, and it's this
        which is intercepted and passed to _retrieve_data:
        '/users/123/items?key=abc123'
        the atom doc returned by _retrieve_data is then
        passed to _etags in order to extract the etag attributes
        from each entry, then to feedparser, then to the correct processor
        """
        if kwargs:
            self.add_parameters(**kwargs)
        retrieved = self._retrieve_data(func(self, *args))
        # determine content and format, based on url params
        content = self.content.search(
                self.request.get_full_url()).group(0) if \
            self.content.search(
                    self.request.get_full_url()) else 'bib'
        fmt = self.fmt.search(
                self.request.get_full_url()).group(0) if \
            self.fmt.search(
                    self.request.get_full_url()) else 'atom'
        # step 1: process atom if it's atom-formatted
        if fmt == 'atom':
            parsed = feedparser.parse(retrieved)
            processor = self.processors.get(content)
            # step 2: if the content is JSON, extract its etags
            if processor == self._json_processor:
                self.etags = etags(retrieved)
            # extract next, previous, first, last links
            self.links = extract_links(parsed)
            return processor(parsed)
        # otherwise, just return the unparsed content as is
        else:
            return retrieved
    return wrapped_f



class Zotero(object):
    """
    Zotero API methods
    A full list of methods can be found here:
    http://www.zotero.org/support/dev/server_api
    """

    def __init__(self, user_id = None, user_key = None):
        """ Store Zotero credentials
        """
        self.endpoint = 'https://api.zotero.org'
        if user_id and user_key:
            self.user_id = user_id
            self.user_key = user_key
        else:
            raise ze.MissingCredentials, \
            'Please provide both the user ID and the user key'
        self.url_params = None
        self.etags = None
        self.request = None
        # these aren't valid item fields, so never send them to the server
        self.temp_keys = set(['key', 'etag', 'group_id', 'updated'])
        # determine which processor to use for the parsed content
        self.fmt = re.compile('(?<=format=)\w+')
        self.content = re.compile('(?<=content=)\w+')
        self.processors = {
            'bib': self._bib_processor,
            'citation': self._bib_processor,
            'bibtex': self._bib_processor,
            'bookmarks': self._bib_processor,
            'coins': self._bib_processor,
            'csljson': self._csljson_processor,
            'mods': self._bib_processor,
            'refer': self._bib_processor,
            'rdf_bibliontology': self._bib_processor,
            'rdf_dc': self._bib_processor,
            'rdf_zotero': self._bib_processor,
            'ris': self._bib_processor,
            'tei': self._bib_processor,
            'wikipedia': self._bib_processor,
            'json': self._json_processor
            }
        self.links = None
        self.templates = {}

    def _cache(self, template, key):
        """
        Add a retrieved template to the cache for 304 checking
        accepts a dict and key name, adds the retrieval time, and adds both
        to self.templates as a new dict using the specified key
        """
        # cache template and retrieval time for subsequent calls
        thetime = datetime.datetime.utcnow().replace(
            tzinfo = pytz.timezone('GMT'))
        self.templates[key] = {
            'tmplt': template,
            'updated': thetime}
        return template

    @cleanwrap
    def _cleanup(self, to_clean):
        """ Remove keys we added for internal use
        """
        return dict([[k, v] for k, v in to_clean.iteritems()
                    if k not in self.temp_keys])

    def _retrieve_data(self, request = None):
        """
        Retrieve Zotero items via the API
        Combine endpoint and request to access the specific resource
        Returns an Atom document
        """
        full_url = '%s%s' % (self.endpoint, request)
        self.request = urllib2.Request(full_url)
        self.request.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            response = urllib2.urlopen(self.request)
            data = response.read()
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(self.request, error)
        # parse the result into Python data structures
        return data

    def _updated(self, url, payload, template = None):
        """
        Generic call to see if a template request returns 304
        accepts:
        - a string to combine with the API endpoint
        - a dict of format values, in case they're required by 'url'
        - a template name to check for
        As per the API docs, a template less than 1 hour old is
        assumed to be fresh, and will immediately return False if found
        """
        # If the template is more than an hour old, try a 304
        if abs(datetime.datetime.utcnow().replace(
            tzinfo = pytz.timezone('GMT')) -
            self.templates[template]['updated']).seconds > 3600:
            opener = urllib2.build_opener(NotModifiedHandler())
            query = self.endpoint + url.format(
                u = self.user_id, **payload)
            req = urllib2.Request(query)
            req.add_header(
                'If-Modified-Since',
                payload['updated'].strftime("%a, %d %b %Y %H:%M:%S %Z"))
            req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
            try:
                url_handle = opener.open(req)
                _ = url_handle.info()
            except (urllib2.HTTPError, urllib2.URLError), error:
                error_handler(req, error)
            return hasattr(url_handle, 'code') and url_handle.code == 304
        # Still plenty of life left in't
        return False

    def add_parameters(self, **params):
        """ Add URL parameters. Will always add the user key
        """
        self.url_params = None
        if params:
            params['key'] = self.user_key
        else:
            params = {'key': self.user_key}
        # always return json, unless different format is specified
        if 'content' not in params and 'format' not in params:
            params['content'] = 'json'
        self.url_params = urllib.urlencode(params)

    def _build_query(self, query_string):
        """
        Set request parameters. Will always add the user ID if it hasn't
        been specifically set by an API method
        """
        try:
            query = urllib.quote(query_string.format(u = self.user_id))
        except KeyError, err:
            raise ze.ParamNotPassed, \
            'There\'s a request parameter missing: %s' % err
        # Add the URL parameters and the user key, if necessary
        if not self.url_params:
            self.add_parameters()
        query = '%s?%s' % (query, self.url_params)
        return query

    # The following methods are Zotero Read API calls
    def num_items(self):
        """ Return the total number of top-level items in the library
        """
        query = self._build_query('/users/{u}/items/top')
        return self._totals(query)

    def num_collectionitems(self, collection):
        """ Return the total number of items in the specified collection
        """
        query = '/users/{u}/collections/{c}/items'.format(
            u = self.user_id, c = collection.upper())
        return self._totals(query)

    def num_groupitems(self, group):
        """ Return the total number of items in the specified group
        """
        query = '/groups/{g}/items'.format(
            g = group.upper())
        return self._totals(query)

    def num_tagitems(self, tag):
        """ Return the total number of items for the specified tag
        """
        query = '/users/{u}/tags/{t}/items'.format(
            u = self.user_id, t = tag)
        return self._totals(query)

    def _totals(self, query):
        """ General method for returning total counts
        """
        self.add_parameters(limit=1)
        data = self._retrieve_data(query)
        self.url_params = None
        parsed = feedparser.parse(data)
        # extract the 'total items' figure
        return int(parsed.feed['zapi_totalresults'].encode('utf8'))

    @retrieve
    def items(self, **kwargs):
        """ Get user items
        """
        query_string = '/users/{u}/items'
        return self._build_query(query_string)

    @retrieve
    def top(self, **kwargs):
        """ Get user top-level items
        """
        query_string = '/users/{u}/items/top'
        return self._build_query(query_string)

    @retrieve
    def trash(self, **kwargs):
        """ Get all items in the trash
        """
        query_string = '/users/{u}/items/trash'
        return self._build_query(query_string)

    @retrieve
    def item(self, item, **kwargs):
        """ Get a specific item
        """
        query_string = '/users/{u}/items/{i}'.format(
        u = self.user_id, i = item.upper())
        return self._build_query(query_string)

    @retrieve
    def children(self, item, **kwargs):
        """ Get a specific item's child items
        """
        query_string = '/users/{u}/items/{i}/children'.format(
        u = self.user_id, i = item.upper())
        return self._build_query(query_string)

    @retrieve
    def tag_items(self, tag, **kwargs):
        """ Get items for a specific tag
        """
        query_string = '/users/{u}/tags/{t}/items'.format(
        u = self.user_id, t = tag)
        return self._build_query(query_string)

    @retrieve
    def collection_items(self, collection, **kwargs):
        """ Get a specific collection's items
        """
        query_string = '/users/{u}/collections/{c}/items'.format(
        u = self.user_id, c = collection.upper())
        return self._build_query(query_string)

    @retrieve
    def group_items(self, group, **kwargs):
        """ Get a specific group's items
        """
        query_string = '/groups/{g}/items'.format(
        g = group.upper())
        return self._build_query(query_string)

    @retrieve
    def group_top(self, group, **kwargs):
        """ Get a specific group's top-level items
        """
        query_string = '/groups/{g}/items/top'.format(
        g = group.upper())
        return self._build_query(query_string)

    @retrieve
    def group_item(self, group, item, **kwargs):
        """ Get a specific group item
        """
        query_string = '/groups/{g}/items/{i}'.format(
        g = group.upper(),
        i = item.upper())
        return self._build_query(query_string)

    @retrieve
    def group_trash(self, group, **kwargs):
        """ Get the items in a specific group's trash
        """
        query_string = '/groups/{g}/trash'.format(
        g = group.upper())
        return self._build_query(query_string)

    @retrieve
    def group_item_children(self, group, item, **kwargs):
        """ Get a specific group item's child items
        """
        query_string = '/groups/{g}/items/{i}/children'.format(
        g = group.upper(),
        i = item.upper())
        return self._build_query(query_string)

    @retrieve
    def group_items_tag(self, group, tag, **kwargs):
        """ Get a specific group's items for a specific tag
        """
        query_string = '/groups/{g}/tags/{t}/items'.format(
        g = group.upper(),
        t = tag)
        return self._build_query(query_string)

    @retrieve
    def group_collection_items(self, group, collection, **kwargs):
        """ Get a specific group's items from a specific collection
        """
        query_string = '/groups/{g}/collections/{c}/items'.format(
        g = group.upper(),
        c = collection.upper())
        return self._build_query(query_string)

    @retrieve
    def group_collection_top(self, group, collection, **kwargs):
        """ Get a specific group's top-level items from a specific collection
        """
        query_string = '/groups/{g}/collections/{c}/items/top'.format(
        g = group.upper(),
        c = collection.upper())
        return self._build_query(query_string)

    @retrieve
    def group_collection_item(self, group, collection, item, **kwargs):
        """ Get a specific collection's item from a specific group
        """
        query_string = '/groups/{g}/collections/{c}/items/{i}'.format(
        g = group.upper(),
        c = collection.upper(),
        i = item.upper())
        return self._build_query(query_string)

    @retrieve
    def collections(self, **kwargs):
        """ Get user collections
        """
        query_string = '/users/{u}/collections'
        return self._build_query(query_string)

    @retrieve
    def collections_sub(self, collection, **kwargs):
        """ Get subcollections for a specific collection
        """
        query_string = '/users/{u}/collections/{c}/collections'.format(
        u = self.user_id, c = collection.upper())
        return self._build_query(query_string)

    @retrieve
    def group_collections(self, group, **kwargs):
        """ Get collections for a specific group
        """
        query_string = '/groups/{group}/collections'.format(
        u = self.user_id,
        g = group.upper())
        return self._build_query(query_string)

    @retrieve
    def group_collection(self, group, collection, **kwargs):
        """ Get a specific collection for a specific group
        """
        query_string = '/groups/{g}/collections/{c}'.format(
        g = group.upper(),
        c = collection.upper())
        return self._build_query(query_string)

    @retrieve
    def group_collection_sub(self, group, collection, **kwargs):
        """ Get collections for a specific group
        """
        query_string = '/groups/{g}/collections/{c}/collections'.format(
        g = group.upper(),
        c = collection.upper())
        return self._build_query(query_string)

    @retrieve
    def groups(self, **kwargs):
        """ Get user groups
        """
        query_string = '/users/{u}/groups'
        return self._build_query(query_string)

    @retrieve
    def tags(self, **kwargs):
        """ Get tags for a specific item
        """
        query_string = '/users/{u}/tags'
        return self._build_query(query_string)

    @retrieve
    def item_tags(self, item, **kwargs):
        """ Get tags for a specific item
        """
        query_string = '/users/{u}/items/{i}/tags'.format(
        u = self.user_id, i = item.upper())
        return self._build_query(query_string)

    @retrieve
    def group_tags(self, group, **kwargs):
        """ Get tags for a specific group
        """
        query_string = '/groups/{g}/tags'.format(
        g = group.upper())
        return self._build_query(query_string)

    @retrieve
    def group_item_tags(self, group, item, **kwargs):
        """ Get tags for a specific item in a specific group
        """
        query_string = '/groups/{g}/items/{i}/tags'.format(
        g = group.upper(),
        i = item.upper())
        return self._build_query(query_string)

    def all_top(self, **kwargs):
        """ Retrieve all top-level items
        """
        return self.everything(self.top(**kwargs))

    @retrieve
    def follow(self):
        """ Return the result of the call to the URL in the 'Next' link
        """
        if self.links:
            return self.links['next']
        else:
            return None

    def everything(self, query):
        """
        Retrieve all items in the library for a particular query
        This method will override the 'limit' parameter if it's been set
        """
        items = []
        items.extend(query)
        while not self.links['self'] == self.links['last']:
            items.extend(self.follow())
        return items

    def get_subset(self, subset):
        """
        Retrieve a subset of items
        Accepts a single argument: a list of item IDs
        """
        if len(subset) > 50:
            raise ze.TooManyItems, \
                    "You may only retrieve 50 items per call"
        # remember any url parameters that have been set
        params = self.url_params
        retr = []
        for itm in subset:
            retr.extend(self.item(itm))
            self.url_params = params
        # clean up URL params when we're finished
        self.url_params = None
        return retr

    # The following methods process data returned by Read API calls
    def _json_processor(self, retrieved):
        """ Format and return data from API calls which return Items
        """
        # send entries to _tags_data if there's no JSON
        try:
            items = [json.loads(e['content'][0]['value'])
                for e in retrieved.entries]
        except KeyError:
            return self._tags_data(retrieved)
        # try to add various namespaced values to the items
        zapi_keys = ['key']
        for zapi in zapi_keys:
            try:
                for key, _ in enumerate(items):
                    items[key][unicode(zapi)] = \
                        retrieved.entries[key][unicode('zapi_%s' % zapi)]
            except KeyError:
                pass
        for key, _ in enumerate(items):
            try:
        # add the etags
                items[key][u'etag'] = self.etags[key]
            except TypeError:
                pass
        # try to add the updated time in the same format the server expects it
            items[key][u'updated'] = \
                time.strftime(
                    "%a, %d %b %Y %H:%M:%S %Z",
                    retrieved.entries[key]['updated_parsed'])
        # Try to get a group ID, and add it to the dict
        try:
            group_id = [urlparse(g['links'][0]['href']).path.split('/')[2]
                for g in retrieved.entries]
            for k, val in enumerate(items):
                val[u'group_id'] = group_id[k]
        except KeyError:
            pass
        self.url_params = None
        return items

    def _csljson_processor(self, retrieved):
        """
        return a list of dicts which are dumped CSL JSON
        """
        items = []
        for csl in retrieved.entries:
            items.append(json.loads(csl['content'][0]['value']))
        self.url_params = None
        return items

    def _bib_processor(self, retrieved):
        """ Return a list of strings formatted as HTML bibliography entries
        """
        items = []
        for bib in retrieved.entries:
            items.append(bib['content'][0]['value'])
        self.url_params = None
        return items

    def _tags_data(self, retrieved):
        """ Format and return data from API calls which return Tags
        """
        tags = [t['title'] for t in retrieved.entries]
        self.url_params = None
        return tags

    # The following methods are Write API calls
    def item_template(self, itemtype):
        """ Get a template for a new item
        """
        # if we have a template and it hasn't been updated since we stored it
        template_name = 'item_template_' + itemtype
        query_string = '/items/new?itemType={i}'.format(
            i = itemtype)
        if self.templates.get(template_name) and not \
                self._updated(
                    query_string,
                    self.templates[template_name],
                    template_name):
            return self.templates[template_name]['tmplt']
        # otherwise perform a normal request and cache the response
        retrieved = self._retrieve_data(query_string)
        return self._cache(json.loads(retrieved), template_name)

    def add_tags(self, item, *tags):
        """
        Add one or more tags to a retrieved item,
        then update it on the server
        Accepts a dict, and one or more tags to add to it
        Returns the updated item from the server
        """
        # Make sure there's a tags field, or add one
        try:
            assert(item['tags'])
        except AssertionError:
            item['tags'] = list()
        for tag in tags:
            item['tags'].append({u'tag': u'%s' % tag})
        # make sure everything's OK
        assert(self.check_items([item]))
        return self.update_item(item)

    def check_items(self, items):
        """
        Check that items to be created contain no invalid dict keys
        Accepts a single argument: a list of one or more dicts
        The retrieved fields are cached and re-used until a 304 call fails
        """
        # check for a valid cached version
        if self.templates.get('item_fields') and not \
                self._updated(
                    '/itemFields',
                    self.templates['item_fields'],
                    'item_fields'):
            template = set(
                    t['field'] for t in self.templates['item_fields']['tmplt'])
        else:
            template = set(
                    t['field'] for t in self.item_fields())
        # add fields we know to be OK
        template = template | set(['tags', 'notes', 'itemType', 'creators'])
        template = template | set(self.temp_keys)
        for pos, item in enumerate(items):
            to_check = set(i for i in item.iterkeys())
            difference = to_check.difference(template)
            if difference:
                raise KeyError, \
"Invalid keys present in item %s: %s" % (pos + 1,
        ' '.join(i for i in difference))
        return True

    def item_types(self):
        """ Get all available item types
        """
        # Check for a valid cached version
        if self.templates.get('item_types') and not \
                self._updated(
                    '/itemTypes',
                    self.templates['item_types'],
                    'item_types'):
            return self.templates['item_types']['tmplt']
        query_string = '/itemTypes'
        # otherwise perform a normal request and cache the response
        retrieved = self._retrieve_data(query_string)
        return self._cache(json.loads(retrieved), 'item_types')

    def creator_fields(self):
        """ Get localised creator fields
        """
        # Check for a valid cached version
        if self.templates.get('creator_fields') and not \
                self._updated(
                    '/creatorFields',
                    self.templates['creator_fields'],
                    'creator_fields'):
            return self.templates['creator_fields']['tmplt']
        query_string = '/creatorFields'
        # otherwise perform a normal request and cache the response
        retrieved = self._retrieve_data(query_string)
        return self._cache(json.loads(retrieved), 'creator_fields')

    def item_type_fields(self, itemtype):
        """ Get all valid fields for an item
        """
        # check for a valid cached version
        template_name = 'item_type_fields_' + itemtype
        query_string = '/itemTypeFields?itemType={i}'.format(
            i = itemtype)
        if self.templates.get(template_name) and not \
                self._updated(
                    query_string,
                    self.templates[template_name],
                    template_name):
            return self.templates[template_name]['tmplt']
        # otherwise perform a normal request and cache the response
        retrieved = self._retrieve_data(query_string)
        return self._cache(json.loads(retrieved), template_name)

    def item_fields(self):
        """ Get all available item fields
        """
        # Check for a valid cached version
        if self.templates.get('item_fields') and not \
                self._updated(
                    '/itemFields',
                    self.templates['item_fields'],
                    'item_fields'):
            return self.templates['item_fields']['tmplt']
        query_string = '/itemFields'
        # otherwise perform a normal request and cache the response
        retrieved = self._retrieve_data(query_string)
        return self._cache(json.loads(retrieved), 'item_fields')

    def item_creator_types(self, itemtype):
        """ Get all available creator types for an item
        """
        # check for a valid cached version
        template_name = 'item_creator_types_' + itemtype
        query_string = '/itemTypeFields?itemType={i}'.format(
            i = itemtype)
        if self.templates.get(template_name) and not \
                self._updated(
                    query_string,
                    self.templates[template_name],
                    template_name):
            return self.templates[template_name]['tmplt']
        # otherwise perform a normal request and cache the response
        retrieved = self._retrieve_data(query_string)
        return self._cache(json.loads(retrieved), template_name)

    def create_items(self, payload):
        """
        Create new Zotero items
        Accepts one argument, a list containing one or more item dicts
        """
        if len(payload) > 50:
            raise ze.TooManyItems, \
                    "You may only create up to 50 items per call"
        to_send = json.dumps({'items': [i for i in self._cleanup(*payload)]})
        req = urllib2.Request(self.endpoint
            + '/users/{u}/items'.format(u = self.user_id) + '?'
            + urllib.urlencode({'key': self.user_key}))
        req.add_data(to_send)
        req.add_header('X-Zotero-Write-Token', token())
        req.add_header('Content-Type', 'application/json' )
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            resp = urllib2.urlopen(req)
            data = resp.read()
            self.etags = etags(data)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return self._json_processor(feedparser.parse(data))

    def create_collection(self, payload):
        """
        Create a new Zotero collection
        Accepts one argument, a dict containing the following keys:

        'name': the name of the collection
        'parent': OPTIONAL, the parent collection to which you wish to add this
        """
        # no point in proceeding if there's no 'name' key
        if 'name' not in payload:
            raise ze.ParamNotPassed, \
                    "The dict you pass must include a 'name' key"
        # add a blank 'parent' key if it hasn't been passed
        if not 'parent' in payload:
            payload['parent'] = ''
        to_send = json.dumps(payload)
        req = urllib2.Request(
        self.endpoint + '/users/{u}/collections'.format(u = self.user_id) +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.add_data(to_send)
        req.add_header('X-Zotero-Write-Token', token())
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            urllib2.urlopen(req)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return True

    def update_collection(self, payload):
        """
        Update a Zotero collection
        Accepts one argument, a dict containing collection data retrieved
        using e.g. 'collections()'
        """
        tkn = payload['etag']
        key = payload['key']
        # remove any keys we've added
        to_send = (i for i in self._cleanup(payload))
        # Override urllib2 to give it a PUT verb
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        req = urllib2.Request(
        self.endpoint + '/users/{u}/collections/{c}'.format(
            u = self.user_id, c = key) +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.add_data(to_send)
        req.get_method = lambda: 'PUT'
        req.add_header('If-Match', tkn)
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            opener.open(req)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return True

    def update_item(self, payload):
        """
        Update an existing item
        Accepts one argument, a dict containing Item data
        """
        etag = payload['etag']
        ident = payload['key']
        to_send = json.dumps(*self._cleanup(payload))
        # Override urllib2 to give it a PUT verb
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        req = urllib2.Request(self.endpoint + '/users/{u}/items/'.format(
            u = self.user_id) + ident +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.get_method = lambda: 'PUT'
        req.add_data(to_send)
        req.add_header('If-Match', etag)
        req.add_header('Content-Type', 'application/json')
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            resp = opener.open(req)
            data = resp.read()
            self.etags = etags(data)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return self._json_processor(feedparser.parse(data))

    def addto_collection(self, collection, payload):
        """
        Add one or more items to a collection
        Accepts two arguments:
        The collection ID, and a list containing one or more item dicts
        """
        # create a string containing item IDs
        to_send = ' '.join([p['key'].encode('utf8') for p in payload])

        req = urllib2.Request(
        self.endpoint + '/users/{u}/collections/{c}/items'.format(
            u = self.user_id, c = collection.upper()) +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.add_data(to_send)
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            urllib2.urlopen(req)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return True

    def deletefrom_collection(self, collection, payload):
        """
        Delete an item from a collection
        Accepts two arguments:
        The collection ID, and a dict containing one or more item dicts
        """
        ident = payload['key']
        # Override urllib2 to give it a DELETE verb
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        req = urllib2.Request(
        self.endpoint + '/users/{u}/collections/{c}/items/'.format(
            u = self.user_id, c = collection.upper()) + ident +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.get_method = lambda: 'DELETE'
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            opener.open(req)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return True

    def delete_item(self, payload):
        """
        Delete an Item from a Zotero library
        Accepts a single argument: a dict containing item data
        """
        etag = payload['etag']
        ident = payload['key']
        # Override urllib2 to give it a DELETE verb
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        req = urllib2.Request(self.endpoint + '/users/{u}/items/'.format(
            u = self.user_id) + ident +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.get_method = lambda: 'DELETE'
        req.add_header('If-Match', etag)
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            opener.open(req)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return True

    def delete_collection(self, payload):
        """
        Delete a Collection from a Zotero library
        Accepts a single argument: a dict containing item data
        """
        etag = payload['etag']
        ident = payload['key']
        # Override urllib2 to give it a DELETE verb
        opener = urllib2.build_opener(urllib2.HTTPHandler)
        req = urllib2.Request(self.endpoint + '/users/{u}/collections/{c}'
            .format(u = self.user_id, c = ident) +
            '?' + urllib.urlencode({'key': self.user_key}))
        req.get_method = lambda: 'DELETE'
        req.add_header('If-Match', etag)
        req.add_header('User-Agent', 'Pyzotero/%s' % __version__)
        try:
            opener.open(req)
        except (urllib2.HTTPError, urllib2.URLError), error:
            error_handler(req, error)
        return True



def error_handler(req, error):
    """ Error handler for HTTP requests
    """
    # Distinguish between URL errors and HTTP status codes of 400+
    if hasattr(error, 'reason'):
        raise ze.CouldNotReachURL, \
"Could not reach server. Reason: %s\nURL: %s" % (error, req.get_full_url())
    elif hasattr(error, 'code'):
        if error.code == 401 or error.code == 403:
            raise ze.UserNotAuthorised, \
"Not authorised: (%s)\nURL: %s\nType: %s\nData: %s" % (
    error.code, req.get_full_url(), req.get_method(), req.get_data())
        elif error.code == 400:
            raise ze.UnsupportedParams, \
"Invalid request, probably due to unsupported parameters: %s\n%s\n%s" % \
            (req.get_full_url(), req.get_method(), req.get_data())
        elif error.code == 404:
            raise ze.ResourceNotFound, \
"No results for the following query:\n%s" % req.get_full_url()
        elif error.code == 409:
            raise ze.Conflict, \
"The target library is locked"
        elif error.code == 412:
            raise ze.PreConditionFailed, \
"The item was already submitted, or has changed since you retrieved it"
        else:
            raise ze.HTTPError, \
"HTTP Error %s (%s)\nURL: %s\nData: %s" % (
            error.msg, error.code, req.get_full_url(), req.get_data())



class NotModifiedHandler(urllib2.BaseHandler):
    """
    304 Not Modified handler for urllib2
    http://goo.gl/2fhI3
    """
    def __init__(self):
        pass
    def http_error_304(self, req, f_p, code, message, headers):
        """ The actual handler """
        addinfourl = urllib2.addinfourl(f_p, headers, req.get_full_url())
        addinfourl.code = code
        return addinfourl

