# Copyright 2014 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from google.appengine.ext import ndb

from model.base_build_model import BaseBuildModel
from model import wf_analysis_status


class WfAnalysis(BaseBuildModel):
  """Represents an analysis of a build of a builder in a Chromium waterfall.

  'Wf' is short for waterfall.
  """

  @staticmethod
  def _CreateKey(master_name, builder_name, build_number):  # pragma: no cover
    return ndb.Key('WfAnalysis',
                   BaseBuildModel.CreateBuildId(
                       master_name, builder_name, build_number))

  @staticmethod
  def Create(master_name, builder_name, build_number):  # pragma: no cover
    return WfAnalysis(
        key=WfAnalysis._CreateKey(master_name, builder_name, build_number))

  @staticmethod
  def Get(master_name, builder_name, build_number):  # pragma: no cover
    return WfAnalysis._CreateKey(master_name, builder_name, build_number).get()

  @property
  def completed(self):
    return self.status in (
        wf_analysis_status.ANALYZED, wf_analysis_status.ERROR)

  @property
  def failed(self):
    return self.status == wf_analysis_status.ERROR

  def Reset(self):  # pragma: no cover
    """Resets to the state as if no analysis is run."""
    self.pipeline_status_path = None
    self.status = wf_analysis_status.PENDING
    self.start_time = None

  # Information of the analyzed build.
  build_start_time = ndb.DateTimeProperty(indexed=True)

  # Information of analysis processing.
  pipeline_status_path = ndb.StringProperty(indexed=False)
  status = ndb.IntegerProperty(
      default=wf_analysis_status.PENDING, indexed=False)
  start_time = ndb.DateTimeProperty(indexed=False)
  updated_time = ndb.DateTimeProperty(indexed=False, auto_now=True)

  # Analysis result.
  result = ndb.JsonProperty(indexed=False, compressed=True)