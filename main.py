import webapp2
import jinja2
import os
import json
import logging
import urllib
import math
from google.appengine.ext import deferred
from google.appengine.api import memcache
from google.appengine.api import urlfetch
from settings import REDBOX_APIKEY, RT_APIKEY
from lxml import etree
from google.appengine.ext import ndb
from levenshtein import levenshtein
from datetime import date
import re
import copy
import unicodedata
from operator import itemgetter
from hashlib import md5

jinja_environment = jinja2.Environment(
    loader=jinja2.FileSystemLoader(os.path.dirname(__file__)))


class Movie(ndb.Expando):
    _default_indexed = False


class Response():
    def __init__(self, status_code, content):
        self.status_code = status_code
        self.content = content


# Cache all API calls for an hour
def fetch(url, **kwargs):
    response = memcache.get(md5(url).hexdigest())
    if response is None:
        api_response = urlfetch.fetch(url, **kwargs)
        response = Response(api_response.status_code, api_response.content)
        memcache.set(md5(url).hexdigest(), response, time=3600)
    return response


def download_movies():
    page = 0
    while True:
        page += 1
        url = "https://api.redbox.com/v3/products/movies?pageSize=10&pageNum=%s&apiKey=%s"\
            % (page, REDBOX_APIKEY)
        logging.info("Fetching products...")
        response = fetch(url, headers={'Accept': 'application/json'})
        logging.info("complete!")
        movies = json.loads(response.content)
        if 'Movie' not in movies['Products'] or \
                len(movies['Products']['Movie']) == 0:
            logging.info("Download complete!")
            return
        for obj in movies['Products']['Movie']:
            movie_id = obj['@productId']
            if Movie.get_by_id(movie_id) is not None:
                continue
            logging.info("Saving metadata for %s" % obj['Title'])
            movie = Movie(id=movie_id)
            properties = {}
            for key in obj:
                if type(obj[key]) != dict:
                    properties[key.lower()] = obj[key]
            movie.populate(**properties)
            if type(movie.title) != str and type(movie.title) != unicode:
                movie.title = unicode(movie.title)
            movie.put()

            # Then look up Rotten Tomatoes scores
            url = "http://api.rottentomatoes.com/api/public/v1.0/movies.json?q=%s&apikey=%s"\
                % (urllib.quote(unicodedata.normalize('NFKD', movie.title).encode('ascii', 'ignore')), RT_APIKEY)
            response = fetch(url)
            if response.status_code != 200:
                logging.error("Could not retrieve Rotten Tomatoes information for %s: %s" % (obj['Title'], url))
                content = '{"movies":{}}'
            else:
                content = response.content
            for result in json.loads(content.strip())['movies']:
                if not hasattr(movie, 'score') and \
                        levenshtein(movie.title, unicode(result['title']))/len(movie.title) < 0.2:
                    # This is where the magic happens
                    movie.critics_score = result['ratings']['critics_score']
                    movie.critics_consensus = result['critics_consensus'] if 'critics_consensus' in result else ''
                    movie.audience_score = result['ratings']['audience_score']
                    movie.score = int(sum([
                        result['ratings']['critics_score'],
                        result['ratings']['critics_score'],
                        result['ratings']['audience_score']
                    ])/3)
            if not hasattr(movie, 'score'):
                movie.score = 0

            # Save and return movie
            movie.put()


def fetch_inventory(zipcode):
    # Fetch inventory for all kiosks within 10 miles
    results = []
    logging.info("Fetching kiosks near %s" % zipcode)
    url = "https://api.redbox.com/stores/postalcode/%s?apiKey=%s"\
        % (zipcode, REDBOX_APIKEY)
    response = fetch(url)
    if response.status_code != 200:
            raise ValueError("Could not retrieve kiosks near %s" % zipcode)
    kiosks_root = etree.fromstring(response.content)
    kiosks = kiosks_root.iterchildren()
    num_kiosks = 0
    for kiosk in kiosks:
        num_kiosks += 1
        if num_kiosks > 5:
            continue
        store_id = kiosk.attrib['storeId']
        logging.info("Looking up inventory for store %s" % store_id)
        url = "https://api.redbox.com/v3/inventory/stores/%s?apiKey=%s"\
            % (store_id, REDBOX_APIKEY)
        response = fetch(url)
        if response.status_code != 200:
            logging.error("Could not retrieve inventory for store: %s" % store_id)
            continue
        inventory_root = etree.fromstring(response.content)
        for inventory in inventory_root.iterchildren().next().iterchildren():
            if inventory.attrib['inventoryStatus'] != "InStock":
                continue
            movie_id = inventory.attrib['productId']
            movie = Movie.get_by_id(movie_id)
            if movie is None:
                # TODO - queue creation
                continue
            if not hasattr(movie, 'score'):
                movie.key.delete()
                continue
            distance = float(kiosk\
                .find('{http://api.redbox.com/Stores/v2}DistanceFromSearchLocation')\
                .text)
            output = movie.to_dict()
            output['distance'] = distance
            output['reservation_link'] = "http://www.redbox.com/externalcart?titleID=%s&StoreGUID=%s" % (movie_id.lower(), store_id.lower())
            results.append(output)

    # Generate a unique list of titles, saving closest
    results_keys = {}
    for result in results:
        if result['title'] not in results_keys or \
                results_keys[result['title']]['distance'] > result['distance']:
            results_keys[result['title']] = result
    unique_results = []
    for result in results_keys:
        unique_results.append(results_keys[result])

    # Sort list by score, truncate list
    unique_results = sorted(unique_results, key=itemgetter('score'), reverse=True)[:50]

    # Persist list to memcache
    # memcache.set("movies-%s" % zipcode, results)
    return unique_results


class MainHandler(webapp2.RequestHandler):
    def get(self):
        # If zip code entered, without javascript working,
        # we'll receive the zip code here. Let's handle that
        # and redirect to the proper place:
        zip_code = self.request.GET.get('zip')  # Empty string default in webapp2.
        if zip_code and re.match(r'^\d{5}$', zip_code):
            # TODO: Default 302 okay? Otherwise, set 'permanent=True'.
            # TODO: URL/Route duplication. Need to use named route:
            return self.redirect('/{zip_code}'.format(zip_code=zip_code))
        template_values = {}
        template = jinja_environment.get_template('templates/index.html')

        self.response.out.write(template.render(template_values))


class ZIPHandler(webapp2.RequestHandler):
    def get(self, zipcode):
        #results = memcache.get("movies-%s" % zipcode)
        results = None
        if results is None:
            results = fetch_inventory(zipcode)

        template_values = {"results": results}
        template = jinja_environment.get_template('templates/zipcode.html')
        self.response.out.write(template.render(template_values))

class MoviesHandler(webapp2.RequestHandler):
    def get(self):
        deferred.defer(download_movies, _target='movies')
        logging.info("Inventory download queued")

#MovieInfoHandler(webapp2.RequestHandler):
#    def get(self, movie_id):


app = webapp2.WSGIApplication([
    ('/', MainHandler),
    (r'/(\d{5})', ZIPHandler),
    ('/movies/', MoviesHandler)
])
