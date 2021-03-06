"""Utility functions"""

import datetime

def parseDateTime(dt_str):  # pragma: no cover
  if dt_str is None:
    return None
  dt, _, us = dt_str.partition(".")
  dt = datetime.datetime.strptime(dt, "%Y-%m-%dT%H:%M:%S")
  if us:
    us = int(us.rstrip("Z"), 10)
    return dt + datetime.timedelta(microseconds=us)
  else:
    return dt
