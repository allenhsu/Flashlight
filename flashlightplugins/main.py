#!/usr/bin/env python
#
# Copyright 2007 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import urllib
import urllib2
import zipfile
import StringIO
import json
import os
import base64
import hashlib

import webapp2
from google.appengine.ext import blobstore
from util import *
from google.appengine.ext.webapp import blobstore_handlers
from file_storage import upload_file_and_get_url
from google.appengine.api import images
from google.appengine.api import memcache
from google.appengine.api import users
import bs4
import model
from model import Plugin
from search import search_plugins


def send_upload_form(request, message=None):
    request.response.write(template("upload.html",
                                    {"upload_url": blobstore.create_upload_url('/post_upload'),
                                     "message": message,
                                     "admin": users.is_current_user_admin()}))


class MainHandler(webapp2.RequestHandler):
    def get(self):
        self.response.write(template("index.html"))


class UploadHandler(webapp2.RequestHandler):
    def get(self):
        send_upload_form(self)


def resize_and_store(data, size):
    img = images.Image(data)
    img.resize(size, size)
    data = img.execute_transforms(output_encoding=images.PNG)
    return upload_file_and_get_url(data, 'image/png')


def read_plugin_info(plugin, zip_data):
    file = StringIO.StringIO(zip_data)
    archive = zipfile.ZipFile(file)
    has_info = False
    for name in archive.namelist():
        if name.endswith('/info.json'):
            data = json.load(archive.open(name))
            plugin.name = data['name']
            plugin.info_json = json.dumps(data)
            plugin.categories = data.get('categories', ['Other'])
            has_info = True
        elif name.endswith('/Icon.png'):
            data = archive.open(name).read()
            plugin.icon_url = resize_and_store(data, 128)
        elif name.endswith('/Screenshot.png'):
            screenshot = archive.open(name).read()
            plugin.screenshot_url = resize_and_store(screenshot, 800)
    return has_info

def language_suffixes(languages):
    for lang in languages:
        while True:
            yield "_" + lang if lang != 'en' else ''
            if '-' in lang:
                lang = lang[:lang.rfind('-')]
            else:
                break
    yield ''


def get_localized_key(dict, name, languages, default=None):
    for suffix in language_suffixes(languages):
        key = name + suffix
        if key in dict:
            return dict[key]
    return default


class PostUploadHandler(blobstore_handlers.BlobstoreUploadHandler):
    def post(self):
        secret = self.request.get('secret', '')
        is_update = False
        if len(secret):
            plugins = Plugin.query(Plugin.secret == secret).fetch()
            if len(plugins) == 0:
                send_upload_form(self,
                                 "No plugin could be found that matches that "
                                 "secret.")
                return
            else:
                plugin = plugins[0]
            is_update = True
        else:
            plugin = Plugin()
        plugin.zip_url = 'http://' + os.environ['HTTP_HOST'] + '/serve/' + str(
            self.get_uploads('zip')[0].key())
        zip_data = urllib2.urlopen(plugin.zip_url).read()
        if not read_plugin_info(plugin, zip_data):
            send_upload_form(self,
                             "We couldn't find a valid info.json file in your "
                             "zip.")
            return

        console_key = self.request.get('console_key', None)

        plugin.secret = base64.b64encode(os.urandom(128))
        plugin.notes = self.request.get('notes', '')

        plugin.zip_md5 = hashlib.md5(zip_data).hexdigest()

        admin = users.is_current_user_admin() or \
            (console_key and console_key_is_valid(console_key))
        if admin:
            existing = Plugin.by_name(plugin.name)
            if existing:
                plugin.downloads += existing.downloads
                plugin.put()
                existing.disable()
                existing.downloads = 0
                existing.put()
        plugin.put()
        if admin:
            plugin.enable()

        if console_key is not None:
            self.response.write({"success": True})
        else:
            approval_msg = " It'll be public after it's been approved." if not is_update else ""
            message = "Your plugin was uploaded!" + approval_msg
            self.response.write(template("uploaded.html", {"message": message,
                                                           "plugin": plugin}))


def arrays_overlap(a1, a2):
  return sum([1 for a in a1 if a in a2]) > 0

def locales_overlap(l1, l2):
  normalize_locale = lambda locale: locale.split('-')[0]
  return arrays_overlap(map(normalize_locale, l1), map(normalize_locale, l2))

def group_plugins(plugin_dicts, languages, languages_were_specified):
  if languages_were_specified:
    plugins_for_other_locales = [p for p in plugin_dicts if ('preferred_locales' in p and not locales_overlap(p['preferred_locales'], languages))]
    plugins_for_other_locales_names = set([p['name'] for p in plugins_for_other_locales])
    native_plugins = [p for p in plugin_dicts if p['name'] not in plugins_for_other_locales_names]
    groups = [
      {"plugins": native_plugins},
      {"plugins": plugins_for_other_locales, "header": "Plugins for other regions", "class": "other_locales"}
    ]
  else:
    groups = [{"plugins": plugin_dicts}]
  return [g for g in groups if len(g['plugins'])]

def directory_html(category=None, search=None, languages=None, browse=False,
                   name=None):
    languages_specified = languages != None
    if not languages_specified:
      languages = ['en']
    if category:
        plugins = list(Plugin.query(Plugin.categories == category,
                                    Plugin.approved == True))
        plugins = stable_daily_shuffle(plugins)
    elif search:
        plugins = search_plugins(search)
    elif name:
        plugin = Plugin.by_name(name)
        plugins = [plugin] if plugin else []
    else:
        plugins = []
    plugin_dicts = []
    for p in plugins:
        plugin = info_dict_for_plugin(p, languages)
        plugin_dicts.append(plugin)
    groups = group_plugins(plugin_dicts, languages, languages_specified)
    return template("directory.html",
                    {"groups": groups, "browse": browse,
                     "search": search})


def info_dict_for_plugin(p, languages=['en']):
    plugin = json.loads(p.info_json)
    plugin['displayName'] = get_localized_key(plugin, "displayName", languages,
                                              "")
    plugin['description'] = get_localized_key(plugin, "description", languages,
                                              "")
    plugin['examples'] = get_localized_key(plugin, "examples", languages, [])
    plugin['model'] = p
    plugin['install_url'] = 'install://_?' + \
                            urllib.urlencode([("zip_url", p.zip_url),
                                              ("name", p.name.encode('utf8'))])
    return plugin


class Directory(webapp2.RequestHandler):
    def get(self):
        languages = self.request.get('languages', '').split(',') + ['en'] if self.request.get('languages') else None
        category = self.request.get('category', None)
        search = self.request.get('search', None)
        browse = self.request.get('browse', '') != ''
        name = self.request.get('name', None)
        self.response.write(directory_html(category, search, languages,
                                           browse, name))


class ServeHandler(blobstore_handlers.BlobstoreDownloadHandler):
    def get(self, resource):
        resource = str(urllib.unquote(resource))
        blob_info = blobstore.BlobInfo.get(resource)
        self.send_blob(blob_info)


def compute_categories():
    categories = set()
    for p in Plugin.query(Plugin.approved == True):
        for c in p.categories:
            categories.add(c)
    return categories


def categories():
    categories = memcache.get("categories")
    if not categories:
        categories = compute_categories()
        memcache.set("categories", categories, time=10 * 60)  # 10 min
    return categories


class Categories(webapp2.RequestHandler):
    def get(self):
        self.response.write(json.dumps(list(categories())))


class LogInstall(webapp2.RequestHandler):
    def get(self):
        model.increment_download_count(self.request.get('name'))


class Login(webapp2.RequestHandler):
    def get(self):
        self.redirect(users.create_login_url('/'))


class LatestDownload(webapp2.RequestHandler):
    def get(self):
        url = memcache.get("download_url", None)
        if url is None:
            data = urllib2.urlopen(
                "https://raw.githubusercontent.com/nate-parrott/Flashlight/"
                "master/Appcast.xml").read()
            soup = bs4.BeautifulSoup(data)
            url = soup.find_all("enclosure")[0]["url"]
            memcache.set("download_url", url, time=60 * 10)
        self.redirect(url.encode('utf8'))


class GenerateConsoleKey(webapp2.RequestHandler):
    def post(self):
        key = base64.b64encode(os.urandom(64))
        memcache.set(key, True, time=60 * 60)
        message = "For 60 minutes, the following key will be valid for " \
                  "uploading plugins via the command line:\n\n{0}".format(key)
        self.response.write(template("message.html", {"message": message}))


def console_key_is_valid(key):
    return memcache.get(key) is not None


class ConsoleUpload(webapp2.RequestHandler):
    def get(self, name):
        plugin = Plugin.by_name(name)
        existing_md5 = plugin.zip_md5 if plugin else None
        url = blobstore.create_upload_url('/post_upload')
        self.response.write(json.dumps({"md5": existing_md5,
                                        "upload_url": url}))


class BrowseHandler(webapp2.RequestHandler):
    def get(self):
        cats = list(categories())
        if 'Featured' in cats:
            cats.remove('Featured')
        cats.insert(0, 'Featured')
        self.response.write(template("browse.html",
                                     {"categories": cats,
                                      "initial_html": directory_html(
                                          category='Featured', browse=True)}))


class PluginPageHandler(webapp2.RequestHandler):
    def get(self, name):
        plugin = Plugin.by_name(name)
        if not plugin:
            self.error(404)
            return
        self.response.write(template("plugin_page.html",
                                     {"plugin": info_dict_for_plugin(plugin)}))


app = webapp2.WSGIApplication([('/', MainHandler),
                               ('/browse', BrowseHandler),
                               ('/plugin/(.+)', PluginPageHandler),
                               ('/upload', UploadHandler),
                               ('/post_upload', PostUploadHandler),
                               ('/directory', Directory),
                               ('/serve/(.+)', ServeHandler),
                               ('/categories', Categories),
                               ('/log_install', LogInstall),
                               ('/login', Login),
                               ('/latest_download', LatestDownload),
                               ('/generate_console_key', GenerateConsoleKey),
                               ('/console_upload/(.+)', ConsoleUpload)],
                              debug=True)
