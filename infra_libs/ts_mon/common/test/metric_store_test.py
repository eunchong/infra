# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import functools
import time
import unittest

import mock

from infra_libs.ts_mon.common import errors
from infra_libs.ts_mon.common import metric_store
from infra_libs.ts_mon.common import metrics
from infra_libs.ts_mon.test import stubs


class InProcessMetricStoreTest(unittest.TestCase):
  def setUp(self):
    self.mock_time = mock.create_autospec(time.time, spec_set=True)
    self.state = stubs.MockState(store_ctor=functools.partial(
        metric_store.InProcessMetricStore, time_fn=self.mock_time))
    self.store = self.state.store

    self.metric = mock.create_autospec(
        metrics.Metric, spec_set=True, instance=True)
    self.metric.name = 'foo'
    self.state.metrics['foo'] = self.metric

  def test_sets_start_time(self):
    self.metric.start_time = None
    self.mock_time.return_value = 1234

    self.store.set('foo', (('field', 'value'),), 42)
    self.store.set('foo', (('field', 'value2'),), 43)

    self.assertEqual(1234, self.store.get_all()['foo'][0])
    self.mock_time.assert_called_once_with()

  def test_uses_start_time_from_metric(self):
    self.metric.start_time = 5678

    self.store.set('foo', (('field', 'value'),), 42)
    self.store.set('foo', (('field', 'value2'),), 43)

    self.assertEqual(5678, self.store.get_all()['foo'][0])
    self.assertFalse(self.mock_time.called)

  def test_get(self):
    self.store.set('foo', (('field', 'value'),), 42)
    self.store.set('foo', (('field', 'value2'),), 43)

    self.assertEquals(42, self.store.get('foo', (('field', 'value'),)))
    self.assertEquals(43, self.store.get('foo', (('field', 'value2'),)))
    self.assertIsNone(self.store.get('foo', (('field', 'value3'),)))
    self.assertIsNone(self.store.get('foo', ()))
    self.assertEquals(44, self.store.get('foo', (('field', 'value3'),),
                                         default=44))

    self.assertIsNone(self.store.get('bar', ()))

  def test_set_enforce_ge(self):
    self.store.set('foo', (('field', 'value'),), 42, enforce_ge=True)
    self.store.set('foo', (('field', 'value'),), 43, enforce_ge=True)

    with self.assertRaises(errors.MonitoringDecreasingValueError):
      self.store.set('foo', (('field', 'value'),), 42, enforce_ge=True)

  def test_incr(self):
    self.store.set('foo', (('field', 'value'),), 42)
    self.store.incr('foo', (('field', 'value'),), 4)

    self.assertEquals(46, self.store.get('foo', (('field', 'value'),)))

    with self.assertRaises(errors.MonitoringDecreasingValueError):
      self.store.incr('foo', (('field', 'value'),), -1)

  def test_incr_modify_fn(self):
    modify_fn = mock.Mock()
    modify_fn.return_value = 7

    self.store.set('foo', (('field', 'value'),), 42)
    self.store.incr('foo', (('field', 'value'),), 3, modify_fn=modify_fn)

    self.assertEquals(7, self.store.get('foo', (('field', 'value'),)))
    modify_fn.assert_called_once_with(42, 3)

  def test_reset_for_unittest(self):
    self.store.set('foo', (('field', 'value'),), 42)
    self.store.reset_for_unittest()
    self.assertIsNone(self.store.get('foo', (('field', 'value'),)))

  def test_reset_for_unittest_name(self):
    self.store.set('foo', (('field', 'value'),), 42)
    self.store.reset_for_unittest(name='bar')
    self.assertEquals(42, self.store.get('foo', (('field', 'value'),)))

    self.store.reset_for_unittest(name='foo')
    self.assertIsNone(self.store.get('foo', (('field', 'value'),)))