#!/usr/bin/env python
# encoding: utf-8
"""
zotero.py

Created by Stephan Hügel on 2011-02-28
"""

import sys
import os
import urllib
import urllib2
import httplib
import feedparser

def open_file(to_read):
    """ Open a text file for reading, and strip the newlines
        returns a list, one list item per line
    """
    try:
        with open(to_read, 'r') as opened:
            return [got_lines.rstrip('\n') for got_lines in opened.readlines()]
    except IOError:
        print "Couldn't read values from %s\nCan't continue." % to_read
        raise



class Zotero(object):
    """ Zotero API methods
        A full list of methods can be found here:
        http://www.zotero.org/support/dev/server_api
        Most of the methods return Atom feed documents, which can be parsed
        using feedparser (http://www.feedparser.org/docs/)
    """
    user_id = None
    user_key = None
    
    def __init__(self, user_id = None, user_key = None):
        """ Store Zotero credentials
        """
        self.endpoint = 'api.zotero.org'
        if user_id and user_key:
            self.user_id = user_id
            self.user_key = user_key
        # TODO: throw an error if we're missing either of the above

    def get_all_items(self):
        """ Send GET request to Zotero API endpoint, retrieve all library
            items
        """
        conn = httplib.HTTPSConnection(
        self.endpoint)
        conn.request('GET', '/users/%s/items' % self.user_id)
        response = conn.getresponse()
        print 'Status:', response.status, response.reason
        # We'll want to parse out the useful data using feedparser
        data = response.read()

        print data

    def get_toplevel_items(self):
        """ Send GET request to Zotero API endpoint, retrieve all top-leve
            items in the user's library
        """
        conn = httplib.HTTPSConnection(
        self.endpoint)
        conn.request('GET', '/users/%s/items/top' % self.user_id)
        response = conn.getresponse()
        print 'Status:', response.status, response.reason
        # We'll want to parse out the useful data using feedparser
        data = response.read()

        print data

    def get_topfive_items(self):
        """ Send GET request to Zotero API endpoint, retrieve first five items
            and format contents of <content> nodes as Chicago
        """
        # specify optional params here, start and limit can be used for slicing
        params = {
        'format': 'atom',
        'limit': '5',
        'start': '0',
        'content': 'bib',
        'style': 'chicago-note-bibliography'}
        # cobble together a query string
        querystring = '%s%s' % (
        '?',
        '&'.join(['%s=%s' % (k, v) for k, v in params.items()]))
        conn = httplib.HTTPSConnection(
        self.endpoint)
        conn.request('GET', '/users/%s/items/top?%s' % (
        self.user_id,
        querystring))
        response = conn.getresponse()
        data = response.read()

        print data

def main():
    """ main function
    """
    # Read a file from your cwd. Expects user id on line 1, key on line 2, LF
    auth_values = open_file(os.path.join(os.path.expanduser('~'),
    'zotero_keys.txt'))
    zot_id = auth_values[0]
    zot_key = auth_values[1]
    zot = Zotero(zot_id, zot_key)
    zot.get_topfive_items()

if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, SystemExit):
        # actually raise these, for a clean exit
        raise
    except Exception, error:
        # all other exceptions: display the error
        print error
    else:
        pass
    finally:
        # exit cleanly once we've done everything else
        sys.exit(0)