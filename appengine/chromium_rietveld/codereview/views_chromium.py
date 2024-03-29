# Copyright 2008 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Views for Chromium port of Rietveld."""

import datetime
import logging
import random
import re

from google.appengine.api import app_identity
from google.appengine.api import datastore_errors
from google.appengine.api import taskqueue
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.datastore import datastore_query
from google.appengine.ext import ndb
from google.appengine.runtime import DeadlineExceededError

from django import forms
from django.core.urlresolvers import reverse
from django.http import HttpResponse, HttpResponseRedirect
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.http import HttpResponseServerError
from django.utils import simplejson as json

from codereview import buildbucket
from codereview import decorators as deco
from codereview import decorators_chromium as deco_cr
from codereview import models
from codereview import models_chromium
from codereview import responses
from codereview import views


### Forms ###


class EditFlagsForm(forms.Form):
  last_patchset = forms.IntegerField(widget=forms.HiddenInput())
  commit = forms.BooleanField(required=False)
  cq_dry_run = forms.BooleanField(required=False)
  builders = forms.CharField(max_length=16*1024, required=False)


class TryPatchSetForm(forms.Form):
  reason = forms.CharField(max_length=255)
  revision = forms.CharField(max_length=40, required=False)
  clobber = forms.BooleanField(required=False)
  master = forms.CharField(max_length=255, required=False)
  builders = forms.CharField(max_length=16*1024)
  category = forms.CharField(max_length=255, required=False)


### Utility functions ###


def string_to_datetime(text):
  """Parses a string into datetime including microseconds.

  It parses the standard str(datetime.datetime()) format.
  """
  items = text.split('.', 1)
  result = datetime.datetime.strptime(items[0], '%Y-%m-%d %H:%M:%S')
  if len(items) > 1:
    result = result.replace(microsecond=int(items[1]))
  return result


def unpack_result(result):
  """Buildbot may pack results with multiple layer of lists."""
  while isinstance(result, (list, tuple)):
    result = result[0]
  return result


def handle_build_started(base_url, timestamp, packet, payload):
  build = payload['build']
  # results should always be absent.
  result = build.get('results', [models.TryJobResult.STARTED])
  logging.info('handle_build_started result is %r', result)
  return inner_handle(
      build.get('reason', ''), base_url, timestamp, packet, result,
      build.get('properties', []))


def handle_build_finished(base_url, timestamp, packet, payload):
  build = payload['build']
  # results is omitted if success so insert it manually.
  result = build.get('results', [models.TryJobResult.SUCCESS])
  logging.info('handle_build_finished result is %r', result)
  return inner_handle(
      build.get('reason', ''), base_url, timestamp, packet, result,
      build.get('properties', []))


def inner_handle(reason, base_url, _timestamp, packet, result, properties):
  """Handles one event coming from HttpStatusPush and update the relevant
  TryJobResult object.

  Three cases can arrive:
  - A new try job was started, initiated from svn/http request so no
    TryJobResult exists. No key is passed through, a new entity must be created.
  - A new try job was started from a TryJobResult.result=TRYPENDING. Then the
    key is passed through since the TryJobResult object exists.
  - An existing try job is updated. Most frequent case.
  """
  logging.info('properties is %r', properties)
  issue_id = None
  patchset_id = None
  parent_buildername = None
  parent_buildnumber = None
  buildername = None
  buildnumber = None
  try:
    properties = dict((name, value) for name, value, _ in properties)
    revision = str(properties['revision'])
    mastername = properties.get('mastername')
    buildername = properties['buildername']
    buildnumber = int(properties['buildnumber'])
    slavename = properties['slavename']
    # The try job key property will only be present for try jobs started from
    # rietveld itself, either from the webui or from the try_patchset endpoint.
    try_job_key = properties.get('try_job_key')
    # Issue and patchset might be missing in triggered jobs.
    issue_id = int(properties.get('issue', 0))
    patchset_id = int(properties.get('patchset', 0))

    # Keep them last.
    # The parent_XXX means that this is a build triggered from another build,
    # for example a build that would create builds artifacts trigger another
    # build that run test on a separate slave.
    if (properties.get('parent_buildername') and
        properties.get('parent_buildnumber')):
      parent_buildnumber = int(properties['parent_buildnumber'])
      parent_buildername = properties['parent_buildername']
    project = packet['project']
  except (KeyError, TypeError, ValueError), e:
    logging.warn(
        'Failure when parsing properties: %s; i:%s/%s b:%s/%s' %
        (e, issue_id, patchset_id, buildername, buildnumber))

  # When issue or patchset are missing in a triggered job, try to
  # recover those from the parent build.  Note that this is not
  # reliable, as build numbers are not unique, and is not super
  # efficient since this adds yet another datastore request.
  if not (issue_id and patchset_id) and parent_buildername:
    logging.info(
        'Dereferencing from %s/%d' % (parent_buildername, parent_buildnumber))
    parent_build_key = models.TryJobResult.query(
      models.TryJobResult.builder == parent_buildername,
      models.TryJobResult.buildnumber == parent_buildnumber).get(keys_only=True)
    if parent_build_key:
      # Dereference the parent Patchset object. Luckily, this is in the key.
      patchset_key = parent_build_key.parent()
      if not patchset_id:
        patchset_id = patchset_key.id()
      if not issue_id:
        issue_id = patchset_key.parent().id()
      logging.info('Dereferenced %d/%d' % (issue_id, patchset_id))
      try_job_key = None
    else:
      logging.warn('Failed to find deferenced build')

  if not issue_id or not patchset_id:
    logging.warn('Bad packet, no issue or patchset: %r' % properties)
    return

  result = unpack_result(result)
  issue_key = ndb.Key(models.Issue, issue_id)
  patchset_key = ndb.Key(models.PatchSet, patchset_id, parent=issue_key)
  # Verify the key validity by getting the instance.
  if patchset_key.get() == None:
    logging.warn('Bad issue/patch id: %s/%s' % (issue_id, patchset_id))
    return

  # Used only for logging.
  keyname = '%s-%s-%s-%s' % (issue_id, patchset_id, buildername, buildnumber)

  requester = None
  if properties.get('requester'):
    try:
      requester = users.User(properties['requester'])
    except users.UserNotFoundError:
      pass

  def tx_try_job_result():
    if try_job_key:
      try_obj = ndb.Key(urlsafe=try_job_key).get()
      # If a key is given, then we must only update that try job.
      if not try_obj:
        logging.error('Try job not found by key=%s %s', try_job_key, keyname)
        return False
    else:
      try_obj = models.TryJobResult.query(
          models.TryJobResult.builder == buildername,
          models.TryJobResult.buildnumber == buildnumber,
          ancestor=patchset_key).get()

    if buildername and buildnumber >= 0:
      url = '%sbuilders/%s/builds/%s' % (
          base_url, buildername, buildnumber)
    else:
      url = ''
    if try_obj is None:
      try_obj = models.TryJobResult(
          parent=patchset_key,
          reason=reason,
          url=url,
          result=result,
          master=mastername,
          builder=buildername,
          parent_name=parent_buildername,
          slave=slavename,
          buildnumber=buildnumber,
          revision=revision,
          requester=requester,
          project=project,
          clobber=bool(properties.get('clobber')),
          tests=properties.get('testfilter') or [],
          build_properties=json.dumps(properties))
      logging.info('Creating instance %s' % keyname)
    else:
      # Update result only if relevant.
      if (models.TryJobResult.result_priority(result) >
          models.TryJobResult.result_priority(try_obj.result)):
        logging.info('Setting result: new=%s old=%s %s', result, try_obj.result,
                     keyname)
        try_obj.result = result
      else:
        logging.info('Result irrelevant: new=%s old=%s %s', result,
                     try_obj.result, keyname)
        return True

      if try_obj.project and try_obj.project != project:
        logging.critical(
            'Project for %s didn\'t match: was %s, setting %s' % (keyname,
              try_obj.project, project))
      try_obj.project = project
      # Use timestamp from AppEngine, which always uses UTC.
      # This makes it possible to reliably compare timestamps (including case
      # of calculating how old is the result stored in the datastore).
      try_obj.timestamp = datetime.datetime.now()
      # Update the rest unconditionally.
      try_obj.url = url
      try_obj.revision = revision
      try_obj.slave = slavename
      try_obj.buildnumber = buildnumber
      try_obj.build_properties = json.dumps(properties)
      logging.info(
          'Updated %s: %s' % (keyname, try_obj.result))
    try_obj.put()
    return True
  if not ndb.transaction(tx_try_job_result):
    logging.error('Failed to update %s' % keyname)
    return False
  return True


HANDLER_MAP = {
  'buildStarted': handle_build_started,
  'buildFinished': handle_build_finished,
}


def process_status_push(packets_json, base_url):
  """Processes all the packets coming from HttpStatusPush."""
  packets = sorted(json.loads(packets_json),
                   key=lambda packet: string_to_datetime(packet['timestamp']))
  logging.info('Processing %d packets' % len(packets))
  done = 0
  try:
    for packet in packets:
      logging.info('packet is %r', packet)
      timestamp = string_to_datetime(packet['timestamp'])
      event = packet.get('event', '')
      if event not in HANDLER_MAP:
        logging.warn('Stop sending events of type %s' % event)
        continue
      if 'payload' not in packet:
        logging.warn('Invalid packet %r' % packet)
        continue
      HANDLER_MAP[event](base_url, timestamp, packet, packet.pop('payload'))
      done += 1
  finally:
    logging.info('Processed %d packets' % done)


def _is_job_valid(job):
  """Determines if a pending try job result is valid or not.

  Pending try job results are those with result is set to
  models.TryJobResult.TRYPENDING.  These jobs are invalid if:

  - their associated issue is already committed, or
  - their associated issue is marked private, or
  - their associated PatchSet is no longer the latest in the issue.

  Args:
    job: an instance of models.TryJobResult.

  Returns:
    True if the pending try job is invalid, False otherwise.
  """
  if job.result == models.TryJobResult.TRYPENDING:
    patchset_key = job.key.parent()
    issue_key = patchset_key.parent()
    issue_future = issue_key.get_async()
    last_patchset_key_future = models.PatchSet.query(ancestor=issue_key).order(
      -models.PatchSet.created).get_async(keys_only=True)

    issue = issue_future.get_result()
    if issue.closed or issue.private:
      return False

    last_patchset_key = last_patchset_key_future.get_result()
    if last_patchset_key != patchset_key:
      return False

  return True


### View handlers ###

@deco.issue_editor_required
@deco.xsrf_required
def edit_flags(request):
  """/<issue>/edit_flags - Edit issue's flags."""
  last_patchset = models.PatchSet.query(ancestor=request.issue.key).order(
    -models.PatchSet.created).get()
  if not last_patchset:
    return HttpResponseForbidden('Can only modify flags on last patchset',
        content_type='text/plain')
  if request.issue.closed:
    return HttpResponseForbidden('Can not modify flags for a closed issue',
        content_type='text/plain')

  if request.method == 'GET':
    # TODO(maruel): Have it set per project.
    initial_builders = 'win_rel, mac_rel, linux_rel'
    form = EditFlagsForm(initial={
        'last_patchset': last_patchset.key.id(),
        'commit': request.issue.commit,
        'builders': initial_builders})

    return responses.respond(request,
                         'edit_flags.html',
                         {'issue': request.issue, 'form': form})

  form = EditFlagsForm(request.POST)
  if not form.is_valid():
    return HttpResponseBadRequest('Invalid POST arguments',
        content_type='text/plain')
  if (form.cleaned_data['last_patchset'] != last_patchset.key.id()):
    return HttpResponseForbidden('Can only modify flags on last patchset',
        content_type='text/plain')

  if 'commit' in request.POST:
    if request.issue.private and form.cleaned_data['commit'] :
      return HttpResponseBadRequest(
        'Cannot set commit on private issues', content_type='text/plain')
    if not request.issue.is_cq_available and form.cleaned_data['commit']:
      return HttpResponseBadRequest(
        'Cannot set commit on an issue that does not have a commit queue',
        content_type='text/plain')
    request.issue.commit = form.cleaned_data['commit']
    request.issue.cq_dry_run = form.cleaned_data['cq_dry_run']
    user_email = request.user.email().lower()
    if user_email == views.CQ_SERVICE_ACCOUNT:
      user_email = views.CQ_COMMIT_BOT_EMAIL
    if (request.issue.commit and  # Add as reviewer if setting, not clearing.
        user_email != request.issue.owner.email() and
        user_email not in request.issue.reviewers and
        user_email not in request.issue.collaborator_emails()):
      request.issue.reviewers.append(request.user.email())
    # Update the issue with the checking/unchecking of the CQ box but reduce
    # spam by not emailing users.
    action = 'checked' if request.issue.commit else 'unchecked'
    commit_checked_msg = 'The CQ bit was %s by %s' % (action, user_email)
    if request.issue.cq_dry_run:
      commit_checked_msg += ' to run a CQ dry run'
      request.issue.cq_dry_run_last_triggered_by = user_email
      logging.info('CQ dry run has been triggered by %s for %d/%d', user_email,
                   request.issue.key.id(), last_patchset.key.id())
    # Mail just the owner if the CQ bit was unchecked by someone other than the
    # owner. More details in
    # https://code.google.com/p/skia/issues/detail?id=3093
    unchecked_by_non_owner = (user_email != request.issue.owner.email() and
                              not request.issue.commit)
    views.make_message(request, request.issue, commit_checked_msg,
                       send_mail=unchecked_by_non_owner, auto_generated=True,
                       email_to=[request.issue.owner.email()]).put()
    request.issue.put()
    if request.issue.commit and not request.issue.cq_dry_run:
      views.notify_approvers_of_new_patchsets(request, request.issue)

  if 'builders' in request.POST:
    new_builders = filter(None, map(unicode.strip,
                                    form.cleaned_data['builders'].split(',')))
    if request.issue.private and new_builders:
      return HttpResponseBadRequest(
        'Cannot add trybots on private issues', content_type='text/plain')

    builds = []
    for b in new_builders:
      master, builder = b.split(':', 1)
      builds.append({
        'master': master,
        'builder': builder,
      })
    buildbucket.schedule(request.issue, last_patchset.key.id(), builds)
  return HttpResponse('OK', content_type='text/plain')


@deco.login_required
@deco.xsrf_required
def conversions(request):
  """/conversions - Show and edit the list of base=>source code URL maps."""
  rules = models_chromium.UrlMap.query().order(
      models_chromium.UrlMap.base_url_template).fetch()
  if request.method != 'POST':
    return responses.respond(request, 'conversions.html', {
            'rules': rules})

  if not models_chromium.UrlMap.user_can_edit(request.user):
    # TODO(vbendeb) this domain name should be a configuration item. Or maybe
    # only admins should be allowed to modify the conversions table.
    warning = 'You are not authorized to modify the conversions table.'
    return responses.respond(request, 'conversions.html', {
        'warning': warning,
        'rules': rules,
        })

  for key, _ in request.POST.iteritems():
    if key.startswith('del '):
      del_key = key[4:]
      urlmap_query = models_chromium.UrlMap.query(
          models_chromium.UrlMap.base_url_template == del_key)
      urlmap = urlmap_query.fetch(keys_only=True)
      if not urlmap:
        logging.error('No map for %s found' % del_key)
        continue
      ndb.delete_multi(urlmap)
  base_url = request.POST.get('base_url_template')
  src_url = request.POST.get('source_code_url_template')
  if base_url and src_url:
    warning = ''
    try:
      re.compile(r'%s' % base_url)
    except re.error, err:
      warning = 'Regex error "%s"' % err
    if not warning:
      urlmap_query = models_chromium.UrlMap.query(
          models_chromium.UrlMap.base_url_template == base_url)
      if urlmap_query.count():
        warning = 'Attempt to add a duplicate Base Url'
    if warning:
      rules = models_chromium.UrlMap.query().order(
          models_chromium.UrlMap.base_url_template).fetch()
      return responses.respond(request, 'conversions.html', {
         'warning': warning,
         'rules': rules,
         'base_url': base_url,
         'src_url': src_url
         })

    new_map = models_chromium.UrlMap(
        base_url_template=base_url, source_code_url_template=src_url)
    logging.info(new_map)
    new_map.put()
  return HttpResponseRedirect(reverse(conversions))


@deco_cr.key_required
def status_listener(request):
  """Receives Buildbot events and keeps the try jobs results.

  Defer the actual work to a defer to keep this handler very fast.
  """
  packets = request.POST.get('packets')
  if not packets:
    logging.error('No packets given')
    return HttpResponseBadRequest('No packets given')
  base_url = request.POST.get('base_url')
  if not base_url:
    logging.error('No base url given')
    return HttpResponseBadRequest('No base url given')
  # Using deferred means that we could lose some packets if processing fails.
  # Until a good solution is found for this problem, process the packets
  # synchronously.
  #deferred.defer(process_status_push, packets, base_url)
  process_status_push(packets, base_url)
  return HttpResponse('OK')


@deco_cr.binary_required
def download_binary(request):
  """/<issue>/binary/<patchset>/<patch>/<content>

  Return patch's binary content.  If the patch is not binary, an empty stream
  is returned.  <content> may be 0 for the base content or 1 for the new
  content.  All other values are invalid.
  """
  response = HttpResponse(request.content.data, content_type=request.mime_type)
  filename = re.sub(
      r'[^\w\.]', '_', request.patch.filename.encode('ascii', 'replace'))
  response['Content-Disposition'] = 'attachment; filename="%s"' % filename
  return response


def update_cq_list(_request):
  """/restricted/update_cq_list - Updates list of known CQs."""
  scope = 'https://www.googleapis.com/auth/userinfo.email'
  url = 'https://commit-queue.appspot.com/api/list'
  auth_token, _ = app_identity.get_access_token(scope)
  response = urlfetch.fetch(
      url, headers={'Authorization': 'Bearer ' + auth_token}, deadline=60)
  if response.status_code != 200:
    msg = 'Received non-200 status code when loading %s' % url
    return HttpResponseServerError(msg, content_type='text/plain')

  try:
    cq_names = json.loads(response.content)
  except ValueError as e:
    msg = 'Failed to parse CQ list from %s: %s' % (url, e)
    return HttpResponseServerError(msg, content_type='text/plain')

  @ndb.transactional
  def update():
    key = ndb.Key(models.CQList, 'singleton')
    cq_list = key.get() or models.CQList(key=key)
    cq_list.names = cq_names
    cq_list.put()

  try:
    update()
  except datastore_errors.TransactionFailedError as e:
    msg = 'Failed update CQ list in transaction: %s' % e
    return HttpResponseServerError(msg, content_type='text/plain')

  return HttpResponse('success', content_type='text/plain')


def update_default_builders(_request):
  """/restricted/update_default_builders - Updates list of default builders."""
  models_chromium.TryserverBuilders.refresh()
  return HttpResponse('success', content_type='text/plain')


def delete_old_pending_jobs(_request):
  """/restricted/delete_old_pending_jobs

  Trigger task to delete old pending jobs.
  """
  # Skip jobs older than 5 days regardless of whether they are still valid.
  cutoff_date = datetime.datetime.utcnow() - datetime.timedelta(days=5)
  cutoff_date_str = cutoff_date.strftime("DATETIME(%Y-%m-%d %H:%M:%S)")
  cursor = ''
  limit = 100
  offset = 0
  taskqueue.add(
      url=reverse(delete_old_pending_jobs_task),
      params={
        'cursor': cursor,
        'cutoff_date': cutoff_date_str,
        'limit': str(limit),
        'offset': str(offset),
      },
      queue_name='delete-old-pending-jobs-task')
  msg = 'Trigger done at %s' % cutoff_date_str
  logging.info(msg)
  return HttpResponse(msg, content_type='text/plain')


def delete_old_pending_jobs_task(request):
  """/restricted/delete_old_pending_jobs_task - Deletes old pending jobs.

  Delete invalid pending try jobs older than a day old.
  """
  encoded_cursor = request.POST.get('cursor')
  cutoff_date_str = request.POST.get('cutoff_date')
  cutoff_date = datetime.datetime.strptime(
      cutoff_date_str, "DATETIME(%Y-%m-%d %H:%M:%S)")
  limit = int(request.POST.get('limit'))
  offset = int(request.POST.get('offset'))

  q = models.TryJobResult.query(
      models.TryJobResult.result == models.TryJobResult.TRYPENDING).order(
      models.TryJobResult.timestamp)
  cursor = None
  if encoded_cursor:
    cursor = datastore_query.Cursor(urlsafe=encoded_cursor)

  logging.info('cutoffdate=%s, limit=%d, offset=%d cursor=%s', cutoff_date_str,
      limit, offset, cursor)
  items, next_cursor, _ = q.fetch_page(limit, start_cursor=cursor)
  if not items:
    msg = 'Iteration done'
    logging.info(msg)
    return HttpResponse(msg, content_type='text/plain')

  # Enqueue the next one right away.
  taskqueue.add(
      url=reverse(delete_old_pending_jobs_task),
      params={
        'cursor': next_cursor.urlsafe() if next_cursor else '',
        'cutoff_date': cutoff_date_str,
        'limit': str(limit),
        'offset': str(offset + len(items)),
      },
      queue_name='delete-old-pending-jobs-task')

  count = 0
  for job in items:
    if job.timestamp <= cutoff_date or not _is_job_valid(job):
      job.result = models.TryJobResult.SKIPPED
      job.put()
      count += 1
  msg = '%d pending jobs purged out of %d' % (count, len(items))
  logging.info(msg)
  return HttpResponse(msg, content_type='text/plain')


@deco.require_methods('POST')
@deco.xsrf_required
@deco.patchset_required
@deco.json_response
def try_patchset(request):
  """/<issue>/try/<patchset> - Add a try job for the given patchset."""
  # Only allow trying the last patchset of an issue.
  last_patchset_key = models.PatchSet.query(ancestor=request.issue.key).order(
    -models.PatchSet.created).get(keys_only=True)
  if last_patchset_key != request.patchset.key:
    content = (
        'Patchset %d/%d invalid: Can only try the last patchset of an issue.' %
        (request.issue.key.id(), request.patchset.key.id()))
    logging.info(content)
    return HttpResponseBadRequest(content, content_type='text/plain')

  form = TryPatchSetForm(request.POST)
  if not form.is_valid():
    return HttpResponseBadRequest('Invalid POST arguments',
                                  content_type='text/plain')
  reason = form.cleaned_data['reason']
  revision = form.cleaned_data['revision']
  clobber = form.cleaned_data['clobber']
  master = form.cleaned_data['master']
  category = form.cleaned_data.get('category', 'cq')

  try:
    builders = json.loads(form.cleaned_data['builders'])
  except json.JSONDecodeError:
    content = 'Invalid json for builder spec: ' + form.cleaned_data['builders']
    logging.error(content)
    return HttpResponseBadRequest(content, content_type='text/plain')

  if not isinstance(builders, dict):
    content = 'Invalid builder spec: ' + form.cleaned_data['builders']
    logging.error(content)
    return HttpResponseBadRequest(content, content_type='text/plain')

  logging.debug(
      'clobber=%s\nrevision=%s\nreason=%s\nmaster=%s\nbuilders=%s\category=%s',
      clobber, revision, reason, master, builders, category)

  builds = []
  for builder, tests in builders.iteritems():
    props = {
      'reason': reason,
      'testfilter': tests,
    }
    if clobber:
      # clobber property is checked for presence. Its value is ignored.
      props['clobber'] = True
    builds.append({
      'builder': builder,
      'category': category,
      'master': master,
      'properties': props,
      'revision': revision,
    })

  scheduled_builds = buildbucket.schedule(
      request.issue, request.patchset.key.id(), builds)
  logging.info('Started %d jobs: %r', len(scheduled_builds), scheduled_builds)
  return {
    'jobs': scheduled_builds,
  }

@deco.json_response
def get_pending_try_patchsets(request):
  limit = int(request.GET.get('limit', '10'))
  if limit > 1000:
    limit = 1000

  master = request.GET.get('master', None)
  encoded_cursor = request.GET.get('cursor', None)
  cursor = None
  if encoded_cursor:
    cursor = datastore_query.Cursor(urlsafe=encoded_cursor)

  def MakeJobDescription(job):
    patchset_future = job.key.parent().get_async()
    issue_future = job.key.parent().parent().get_async()
    patchset = patchset_future.get_result()
    issue = issue_future.get_result()
    owner = issue.owner

    # The job description is the basically the job itself with some extra
    # data from the patchset and issue.
    description = job.to_dict()
    description['name'] = '%d-%d: %s' % (issue.key.id(), patchset.key.id(),
                                         patchset.message)
    description['user'] = owner.nickname()
    description['email'] = owner.email()
    if ('chromium/blink' in issue.base
        or (issue.base.startswith('svn:')
            and issue.base.endswith('blink/trunk'))):
      description['root'] = 'src/third_party/WebKit'
    elif ('native_client/src/native_client' in issue.base
        or (issue.base.startswith('svn:')
            and issue.base.endswith('native_client/trunk/src/native_client'))):
      description['root'] = 'native_client'
    elif ('external/gyp' in issue.base
        or (issue.base.startswith('http://gyp.')
            and issue.base.endswith('svn/trunk'))
        or (issue.base.startswith('https://gyp.')
            and issue.base.endswith('svn/trunk'))):
      description['root'] = 'trunk'
    else:
      description['root'] = 'src'
    description['patch_project'] = issue.project
    description['patchset'] = patchset.key.id()
    description['issue'] = issue.key.id()
    description['baseurl'] = issue.base
    return description

  # This uses eventual consistency and cannot be made strongly consistent.
  q = models.TryJobResult.query(
      models.TryJobResult.result == models.TryJobResult.TRYPENDING)
  if master:
    q = q.filter(models.TryJobResult.master == master)
  q = q.order(models.TryJobResult.timestamp)

  # We do not simply use fetch_page() because we do some post-filtering which
  # could lead to under-filled pages.   Instead, we iterate, filter and keep
  # going until we have enough post-filtered results, then return those along
  # with the cursor after the last item.
  jobs = []  # List of dicts to return as JSON. One dict for each TryJobResult.
  total = 0
  next_cursor = None
  query_iter = q.iter(start_cursor=cursor, produce_cursors=True)
  for job in query_iter:
    total += 1
    if _is_job_valid(job):
      jobs.append(MakeJobDescription(job))
      if len(jobs) >= limit:
        break

  # If we stopped because we hit the limit, then there are probably more to
  # get with next_cursor.  This could be wrong in the case where all
  # TryJobResults with later timestamps are not valid, but that is rare and
  # it is harmless for the client to request the next page and get zero results.
  has_more = (len(jobs) >= limit)

  # If any jobs are returned, also include a cursor to try to get more.
  # If no jobs, keep the same cursor to allow 'tail -f' behavior.
  if jobs:
    next_cursor = query_iter.cursor_after()
  else:
    next_cursor = cursor

  logging.info('Found %d entries, returned %d' % (total, len(jobs)))
  return {
    'has_more': has_more,
    'cursor': next_cursor.urlsafe() if next_cursor else '',
    'jobs': jobs
    }
