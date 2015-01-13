# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Implementation of package repository service.

Definitions:
  * Package: a named set of files that can be deployed to a client. Package name
    is a path-like string, e.g. "infra/tools/cipd".
  * Package instance: concrete incarnation of a package, identified by SHA1 of
    the package file content. This hex SHA1 digest is referred to as
    "instance ID". Package files are deterministically built (i.e. same inputs
    produce exact same outputs) and thus instance IDs also depend only on the
    contents of the package.

Package instances are stored in the following way:
  * Package file itself is stored in CAS (implemented on top of Google Storage),
    using SHA1 content hash (i.e. instance ID) as identifier.
  * Package existence (as well as some metadata like when the package was
    registered) is stored in the Datastore in read-only entity (see
    PackageInstance class).

All PackageInstance that belong to the same package are stored in the same
entity group (with root key derived from package name, see Package entity).

Package entity (even though it is empty) is also instantiated in the datastore
to make possible querying for a list of known packages.
"""

import re

from google.appengine.ext import ndb

from components import auth
from components import utils

import cas


# Regular expression for a package name: <word>/<word/<word>. Package names must
# be lower case.
PACKAGE_NAME_RE = re.compile(r'^([a-z0-9_\-]+/)*[a-z0-9_\-]+$')

# Hash algorithm used to derive package instance ID from package data.
DIGEST_ALGO = 'SHA1'


class RepoService(object):
  """Package repository service."""

  def __init__(self, cas_service):
    self.cas_service = cas_service

  def is_fetch_configured(self):
    """True if 'generate_fetch_url' has enough configuration to work."""
    return self.cas_service.is_fetch_configured()

  def get_instance(self, package_name, instance_id):
    """Returns PackageInstance entity if such instance is registered.

    Args:
      package_name: name of the package, e.g. 'infra/tools/cipd'.
      instance_id: identified of the package instance (SHA1 of package content).

    Returns:
      PackageInstance or None.
    """
    return package_instance_key(package_name, instance_id).get()

  def get_package(self, package_name):
    """Returns Package entity if it exists.

    Args:
      package_name: name of the package, e.g. 'infra/tools/cipd'.

    Returns:
      Package or None.
    """
    return package_key(package_name).get()

  @ndb.transactional
  def register_package(self, package_name, caller, now=None):
    """Ensures a given package is registered.

    Can be used by callers with OWNER role to create a package, without
    uploading any concrete instances. Such empty packages later are populated
    with instances by callers with WRITER role.

    Args:
      package_name: name of the package, e.g. 'infra/tools/cipd'.
      caller: auth.Identity that issued the request.
      now: datetime when the request was made (or None for current time).

    Returns:
      Tuple (Package entity, True if registered or False if existed).
    """
    return self._register_package(package_name, caller, now)

  @ndb.transactional
  def register_instance(self, package_name, instance_id, caller, now=None):
    """Makes new PackageInstance entity if it is not yet there.

    Caller must verify that package data is already uploaded to CAS (by using
    is_instance_file_uploaded method).

    Args:
      package_name: name of the package, e.g. 'infra/tools/cipd'.
      instance_id: identifier of the package instance (SHA1 of package content).
      caller: auth.Identity that issued the request.
      now: datetime when the request was made (or None for current time).

    Returns:
      Tuple (PackageInstance entity, True if registered or False if existed).
    """
    key = package_instance_key(package_name, instance_id)
    inst = key.get()
    if inst is not None:
      return inst, False
    now = now or utils.utcnow()
    self._register_package(package_name, caller, now)
    inst = PackageInstance(
        key=key,
        registered_by=caller,
        registered_ts=now)
    inst.put()
    return inst, True

  def generate_fetch_url(self, instance):
    """Given PackageInstance returns signed URL to a package file.

    Args:
      instance: existing PackageInstance entity.

    Returns:
      Signed URL that can be used by a client to fetch package file.
    """
    assert self.is_fetch_configured()
    return self.cas_service.generate_fetch_url(
        DIGEST_ALGO, instance.instance_id)

  def is_instance_file_uploaded(self, package_name, instance_id):
    """Returns True if package instance file is uploaded to CAS.

    Args:
      package_name: name of the package, e.g. 'infra/tools/cipd'.
      instance_id: identifier of the package instance (SHA1 of package content).

    Returns:
      True or False.
    """
    assert is_valid_package_path(package_name), package_name
    assert is_valid_instance_id(instance_id), instance_id
    return self.cas_service.is_object_present(DIGEST_ALGO, instance_id)

  def create_upload_session(self, package_name, instance_id, caller):
    """Opens a new session for data upload to CAS.

    Args:
      package_name: name of the package, e.g. 'infra/tools/cipd'.
      instance_id: identifier of the package instance (SHA1 of package content).
      caller: auth.Identity of whoever is opening an upload session.

    Returns:
      (upload URL to upload data to, upload session ID to pass to CAS API).
    """
    assert is_valid_package_path(package_name), package_name
    assert is_valid_instance_id(instance_id), instance_id
    upload_session, upload_session_id = self.cas_service.create_upload_session(
        DIGEST_ALGO, instance_id, caller)
    return upload_session.upload_url, upload_session_id

  def _register_package(self, package_name, caller, now=None):
    """Implementation of register_package, see its docstring.

    Expected to be called in a transaction. Reused from register_instance.
    """
    assert ndb.in_transaction()
    key = package_key(package_name)
    pkg = key.get()
    if pkg:
      return pkg, False
    pkg = Package(
        key=key,
        registered_by=caller,
        registered_ts=now or utils.utcnow())
    pkg.put()
    return pkg, True


def is_valid_package_path(package_name):
  """True if string looks like a valid package name."""
  return bool(PACKAGE_NAME_RE.match(package_name))


def is_valid_instance_id(instance_id):
  """True if string looks like a valid package instance ID."""
  return cas.is_valid_hash_digest(DIGEST_ALGO, instance_id)


def get_repo_service():
  """Factory method that returns configured RepoService instance.

  If the service is not configured, returns None. Also acts as a mocking point
  for unit tests.
  """
  cas_service = cas.get_cas_service()
  return RepoService(cas_service) if cas_service else None


################################################################################


class Package(ndb.Model):
  """Entity root for PackageInstance entities for some particular package.

  Id is a package name.
  """
  # Who registered the package.
  registered_by = auth.IdentityProperty()
  # When the package was registered.
  registered_ts = ndb.DateTimeProperty()

  @property
  def package_name(self):
    """Name of the package."""
    return self.key.string_id()


class PackageInstance(ndb.Model):
  """Represents some uploaded package instance.

  ID is package instance ID (SHA1 hex digest of package body).
  Parent entity is Package(id=package_name).
  """
  # Who registered the instance.
  registered_by = auth.IdentityProperty()
  # When the instance was registered.
  registered_ts = ndb.DateTimeProperty()

  @property
  def package_name(self):
    """Name of the package this instance belongs to."""
    return self.key.parent().string_id()

  @property
  def instance_id(self):
    """Package instance ID (SHA1 of package file content)."""
    return self.key.string_id()


def package_key(package_name):
  """Returns ndb.Key corresponding to particular Package entity."""
  assert is_valid_package_path(package_name), package_name
  return ndb.Key(Package, package_name)


def package_instance_key(package_name, instance_id):
  """Returns ndb.Key corresponding to particular PackageInstance."""
  assert is_valid_instance_id(instance_id), instance_id
  return ndb.Key(PackageInstance, instance_id, parent=package_key(package_name))