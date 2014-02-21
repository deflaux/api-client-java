"""
Copyright 2014 Google Inc. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

Example Genomics Map Reduce
"""

import datetime
import jinja2
import json
import logging
import os
import re
import urllib
import webapp2

from google.appengine.ext import blobstore
from google.appengine.ext import db

from google.appengine.ext.webapp import blobstore_handlers

from google.appengine.api import files
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.api import urlfetch
from google.appengine.api.urlfetch_errors import DeadlineExceededError

from collections import defaultdict

from mapreduce import base_handler
from mapreduce import mapreduce_pipeline
from mapreduce import operation as op
from mapreduce import shuffler

from oauth2client import appengine

# Increase timeout to the maximum for all requests
urlfetch.set_default_fetch_deadline(60)

JINJA_ENVIRONMENT = jinja2.Environment(
  loader=jinja2.FileSystemLoader('templates'),
  autoescape=True,
  extensions=['jinja2.ext.autoescape'])

client_secrets = os.path.join(os.path.dirname(__file__), 'client_secrets.json')

decorator = appengine.oauth2decorator_from_clientsecrets(
  client_secrets,
  scope=[
    'https://www.googleapis.com/auth/genomics',
    'https://www.googleapis.com/auth/devstorage.read_write'
  ])


class GenomicsCoverageStatistics(db.Model):
  """A helper class that will hold metadata for the user's blobs.

  Specifially, we want to keep track of who uploaded it, where they uploaded it
  from (right now they can only upload from their computer, but in the future
  urlfetch would be nice to add), and links to the results of their MR jobs. To
  enable our querying to scan over our input data, we store keys in the form
  'user/date/blob_key', where 'user' is the given user's e-mail address, 'date'
  is the date and time that they uploaded the item on, and 'blob_key'
  indicates the location in the Blobstore that the item can be found at. '/'
  is not the actual separator between these values - we use '..' since it is
  an illegal set of characters for an e-mail address to contain.
  """

  __SEP = ".."
  __NEXT = "./"

  readsetId = db.StringProperty()
  sequenceName = db.StringProperty()
  sequence = db.IntegerProperty()
  coverage = db.IntegerProperty()
  date = db.DateTimeProperty()

  @staticmethod
  def getFirstKeyForReadsetId(readsetId):
    """Helper function that returns the first possible key a user could own.

    This is useful for table scanning, in conjunction with getLastKeyForReadsetId.

    Args:
      username: The given user's e-mail address.
    Returns:
      The internal key representing the earliest possible key that a user could
      own (although the value of this key is not able to be used for actual
      user data).
    """

    return db.Key.from_path("GenomicsCoverageStatistics", readsetId + GenomicsCoverageStatistics.__SEP)

  @staticmethod
  def getLastKeyForReadsetId(readsetId):
    """Helper function that returns the last possible key a user could own.

    This is useful for table scanning, in conjunction with getFirstKeyForUser.

    Args:
      username: The given user's e-mail address.
    Returns:
      The internal key representing the last possible key that a user could
      own (although the value of this key is not able to be used for actual
      user data).
    """

    return db.Key.from_path("GenomicsCoverageStatistics", readsetId + GenomicsCoverageStatistics.__NEXT)

  @staticmethod
  def getKeyName(readsetId, sequenceName, sequence):
    """Returns the internal key for a particular item in the database.

    Our items are stored with keys of the form 'user/date/blob_key' ('/' is
    not the real separator, but __SEP is).

    Args:
      username: The given user's e-mail address.
      date: A datetime object representing the date and time that an input
        file was uploaded to this app.
      blob_key: The blob key corresponding to the location of the input file
        in the Blobstore.
    Returns:
      The internal key for the item specified by (username, date, blob_key).
    """

    sep = GenomicsCoverageStatistics.__SEP
    return str(readsetId + sep + sequenceName + sep + str(sequence))

class ApiException(Exception):
  pass

class BaseRequestHandler(webapp2.RequestHandler):
  def handle_exception(self, exception, debug_mode):
   if isinstance(exception, ApiException):
     # ApiExceptions are expected, and will return nice error messages to the client
     self.response.write(exception.message)
     self.response.set_status(400)
   else:
     # All other exceptions are unexpected and should crash properly
     return webapp2.RequestHandler.handle_exception(self, exception, debug_mode)

  def get_content(self, path, method='POST', body=None):
    http = decorator.http()
    try:
      response, content = http.request(
        uri="https://www.googleapis.com/genomics/v1beta/%s" % path,
        method=method, body=json.dumps(body) if body else None,
        headers={'Content-Type': 'application/json; charset=UTF-8'})
    except DeadlineExceededError:
      raise ApiException('API fetch timed out')

    # Log results to debug
    logging.debug("Response:")
    logging.debug(response)
    logging.debug("Content:")
    logging.debug(content)

    # Parse the content as json.
    content = json.loads(content)

    if response.status == 404:
      raise ApiException('API not found')
    elif response.status == 400:
      raise ApiException('API request malformed')
    elif response.status != 200:
      if 'error' in content:
        logging.error("Error Code: %s Message: %s" %
                     (content['error']['code'], content['error']['message']))
      raise ApiException("Something went wrong with the API call. "
                        "Please check the logs for more details.")
    return content

class MainHandler(BaseRequestHandler):
  """The main page that users will interact with, which presents users with
  the ability to upload new data or run MapReduce jobs on their existing data.
  """
  # taken from the python-client app. Maybe refactor to common place ???
  TARGETS = [
    {'name': "chr1", 'sequenceLength': 249250621},
    {'name': "chr2", 'sequenceLength': 243199373},
    {'name': "chr3", 'sequenceLength': 198022430},
    {'name': "chr4", 'sequenceLength': 191154276},
    {'name': "chr5", 'sequenceLength': 180915260},
    {'name': "chr6", 'sequenceLength': 171115067},
    {'name': "chr7", 'sequenceLength': 159138663},
    {'name': "chr8", 'sequenceLength': 146364022},
    {'name': "chr9", 'sequenceLength': 141213431},
    {'name': "chr10", 'sequenceLength': 135534747},
    {'name': "chr11", 'sequenceLength': 135006516},
    {'name': "chr12", 'sequenceLength': 133851895},
    {'name': "chr13", 'sequenceLength': 115169878},
    {'name': "chr14", 'sequenceLength': 107349540},
    {'name': "chr15", 'sequenceLength': 102531392},
    {'name': "chr16", 'sequenceLength': 90354753},
    {'name': "chr17", 'sequenceLength': 81195210},
    {'name': "chr18", 'sequenceLength': 78077248},
    {'name': "chr19", 'sequenceLength': 59128983},
    {'name': "chr20", 'sequenceLength': 63025520},
    {'name': "chr21", 'sequenceLength': 48129895},
    {'name': "chr22", 'sequenceLength': 51304566},
    {'name': "chrX", 'sequenceLength': 155270560},
    {'name': "chrY", 'sequenceLength': 59373566},
    ]

  # Provide default settings for the user that they can then override.
  DEFAULT_SETTINGS = {
    'readsetIds': ["CJ_ppJ-WCxD-2oXg667IhDM="],
    'sequenceName': "chr20",
    'sequenceStart': 68198,
    'sequenceEnd': 68199,
    }

  @decorator.oauth_aware
  def get(self):
    if decorator.has_credentials():
      username = users.User().nickname()

      template = JINJA_ENVIRONMENT.get_template('index.html')
      self.response.out.write(template.render({
        "username": username,
        "targets": MainHandler.TARGETS,
        "settings": MainHandler.DEFAULT_SETTINGS,
      }))
    else:
      template = JINJA_ENVIRONMENT.get_template('grantaccess.html')
      self.response.write(template.render({
        'url': decorator.authorize_url()
      }))

  @decorator.oauth_aware
  def post(self):
    body = None
    content = None
    coverage = None
    errorMessage = None

    if self.request.get("submitRead"):
      # Use the API to get the requested data.
      body = {
        'readsetIds': [self.request.get("readsetId")],
        'sequenceName': self.request.get('sequenceName'),
        'sequenceStart': max(0, int(self.request.get('sequenceStart'))),
        'sequenceEnd': int(self.request.get('sequenceEnd')),
        }

      logging.debug("Request Body:")
      logging.debug(body)

      # Make the call.
      try:
        content = self.get_content("reads/search", body=body)
      except ApiException as exception:
        errorMessage = exception.message

    elif self.request.get("submitReadSample"):
      # Read in local sample data which is based off of default settings.
      body = MainHandler.DEFAULT_SETTINGS
      path = os.path.join(os.path.split(__file__)[0], 'static/listRead_SampleData.json')
      file = open(path, 'r')
      content = file.read()
      file.close()
      content = json.loads(content)

    if content != None:
      # Calculate results
      coverage = compute_coverage(content, body["sequenceStart"], body["sequenceEnd"])
      store_coverage(body["readsetIds"][0], body["sequenceName"], coverage)

    # Render template with results or error.
    username = users.User().nickname()
    template = JINJA_ENVIRONMENT.get_template('index.html')
    self.response.out.write(template.render({
      "username": username,
      "targets": MainHandler.TARGETS,
      "settings": body,
      "errorMessage": errorMessage,
      "results": coverage,
    }))

      #pipeline = PhrasesPipeline(readset_id, seqname)

      #pipeline.start()
      #self.redirect(pipeline.base_path + "/status?root=" + pipeline.pipeline_id)

def compute_coverage(content, sequenceStart, sequenceEnd):
  """Takes the json results from the Genomics API call and computes coverage. """
  coverage = defaultdict(int)
  for read in content["reads"]:
    # Check the read against every sequence.
    for sequence in range(sequenceStart, sequenceEnd + 1):
      # If the position is in the range then count it as being covered by that read.
      if sequence >= read["position"] and sequence < read["position"] + len(read["alignedSequence"]):
        coverage[sequence] += 1

  logging.debug("Processed: %d reads." % len(content["reads"]))
  return coverage

def store_coverage(readsetId, sequenceName, dict):
  for sequence, coverage in dict.iteritems():
    key = GenomicsCoverageStatistics.getKeyName(readsetId, sequenceName, sequence)
    s = GenomicsCoverageStatistics(key_name = key)
    s.readsetId = readsetId
    s.sequenceName = sequenceName
    s.sequence = sequence
    s.coverage = coverage
    s.date = datetime.datetime.now()
    s.put()

def split_into_sentences(s):
  """Split text into list of sentences."""
  s = re.sub(r"\s+", " ", s)
  s = re.sub(r"[\\.\\?\\!]", "\n", s)
  return s.split("\n")


def split_into_words(s):
  """Split a sentence into list of words."""
  s = re.sub(r"\W+", " ", s)
  s = re.sub(r"[_0-9]+", " ", s)
  return s.split()


def word_count_map(data):
  """Word count map function."""
  (entry, text_fn) = data
  text = text_fn()

  logging.debug("Got %s", entry.filename)
  for s in split_into_sentences(text):
    for w in split_into_words(s.lower()):
      yield (w, "")


def word_count_reduce(key, values):
  """Word count reduce function."""
  yield "%s: %d\n" % (key, len(values))

class WordCountPipeline(base_handler.PipelineBase):
  """A pipeline to run Word count demo.

  Args:
    blobkey: blobkey to process as string. Should be a zip archive with
      text files inside.
  """

  def run(self, filekey, blobkey):
    logging.debug("filename is %s" % filekey)
    output = yield mapreduce_pipeline.MapreducePipeline(
      "word_count",
      "main.word_count_map",
      "main.word_count_reduce",
      "mapreduce.input_readers.BlobstoreZipInputReader",
      "mapreduce.output_writers.BlobstoreOutputWriter",
      mapper_params={
        "blob_key": blobkey,
        },
      reducer_params={
        "mime_type": "text/plain",
        },
      shards=16)
    yield StoreOutput(filekey, output)


class StoreOutput(base_handler.PipelineBase):
  """A pipeline to store the result of the MapReduce job in the database.

  Args:
    mr_type: the type of mapreduce job run (e.g., WordCount, Index)
    encoded_key: the DB key corresponding to the metadata of this job
    output: the blobstore location where the output of the job is stored
  """

  def run(self, encoded_key, coverage):
    logging.debug("Coverage is %d" % coverage)
    key = db.Key(encoded=encoded_key)
    s = GenomicsCoverageStatistics.get(key)
    s.coverage = coverage
    s.date = datetime.datetime.now()
    s.put()

app = webapp2.WSGIApplication(
  [
    ('/', MainHandler),
    (decorator.callback_path, decorator.callback_handler()),
   ],
  debug=True)
