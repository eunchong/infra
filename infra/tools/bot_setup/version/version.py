
# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Returns the canonical version of bot setup scripts for hostname and image."""

import fnmatch


LKGR = '22613effa47a3569fd4f4fc0b0d48d9143d48772'
DISABLED_BUILDERS = [
    'test_disabled_slave'
]

CANARY_SLAVES = (
    ['swarm%d-c4' % i for i in xrange(1, 10)] +
    ['slave%d-c4' % i for i in xrange(250, 260)] +
    ['winslave1-c4', 'swarm-win1-c4'] +
    ['win12-c1', 'win13-c1'])

CANARY_SLAVES_GLOBS = ['swarm-canary-*']


class BuilderDisabled(Exception):
  """This is raised when a builder should be disabled.

  By raising an exception, the startup sequence becomes interrupted.
  """
  pass


def is_canary_slave(slave_name):
  if slave_name in CANARY_SLAVES:
    return True
  if any(fnmatch.fnmatch(slave_name, p) for p in CANARY_SLAVES_GLOBS):
    return True
  return False


def get_version(slave_name=None, _image_name=None):
  if slave_name and slave_name in DISABLED_BUILDERS:
    raise BuilderDisabled()
  if not slave_name or is_canary_slave(slave_name):
    return 'origin/master'
  return LKGR
