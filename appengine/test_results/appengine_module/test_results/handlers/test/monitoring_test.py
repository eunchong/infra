# Copyright 2016 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import json

from appengine_module.testing_utils import testing

from appengine_module.test_results import main
from appengine_module.test_results.handlers.monitoring import EventMonUploader
from appengine_module.test_results.model.testfile import TestFile


TEST_JSON = {
    'tests': {
      'web-animations-api': {
        'animation-state-changes-negative-playback-rate.html': {
          'expected': 'PASS',
          'actual': 'PASS',
          'has_stderr': True,
          'time': 0.1
        }
      }
    },
    'interrupted': False,
    'version': 3,
    'seconds_since_epoch': 1457612314,
}


class EventMonUploaderTest(testing.AppengineTestCase):
  app_module = main.app

  def test_creates_task_for_upload(self):
    EventMonUploader.upload('master', 'builder', 123, 'ui_tests', TEST_JSON)

    tasks = self.taskqueue_stub.get_filtered_tasks()
    self.assertEqual(len(tasks), 1)
    self.assertEqual(tasks[0].url, '/internal/monitoring/upload')
    params = tasks[0].extract_params()
    self.assertEqual(params['master'], 'master')
    self.assertEqual(params['builder'], 'builder')
    self.assertEqual(params['build_number'], '123')
    self.assertEqual(params['test_type'], 'ui_tests')

  def test_creates_event_mon_event_correctly(self):
    TestFile.add_file(
        'master', 'builder', 'ui_tests', 123, 'full_results.json',
        json.dumps(TEST_JSON))
    response = self.test_app.post('/internal/monitoring/upload', {
      'master': 'master',
      'builder': 'builder',
      'build_number': '123',
      'test_type': 'ui_tests',
    })

    self.assertEqual(200, response.status_int)
    # TODO(sergiyb): Check that event is correctly created.

  def test_does_not_crash_on_incorrect_tests_structure(self):
    EventMonUploader.upload('', '', '', '', {'tests': ['foo', 'bar']})
