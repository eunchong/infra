# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import datetime

from components import utils
from testing_utils import testing

import model


class BuildTest(testing.AppengineTestCase):
  def test_regenerate_lease_key(self):
    build = model.Build(namespace='chromium')
    build.put()
    orig_lease_key = 0
    build.regenerate_lease_key()
    self.assertNotEqual(build.lease_key, orig_lease_key)

  def test_put_with_bad_tags(self):
    build = model.Build(namespace='1', tags=['x'])
    with self.assertRaises(AssertionError):
      build.put()