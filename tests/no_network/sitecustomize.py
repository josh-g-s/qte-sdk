"""Loaded by every example the tests run (`run_example` puts this directory on
PYTHONPATH): any urllib request for a host other than this machine fails as if the host
could not be reached, so the smoke test's update check never reads GitHub, whatever the
SDK under test was installed from. It also turns the automatic update check off, so a
session an example opens neither tries to read GitHub nor records the check in the
developer's cache folder."""

import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_open = urllib.request.OpenerDirector.open


def _loopback_only(self, request, *args, **kwargs):
    url = request if isinstance(request, str) else request.full_url
    if urlsplit(url).hostname not in ("127.0.0.1", "::1", "localhost"):
        raise urllib.error.URLError("the tests make no network requests")
    return _open(self, request, *args, **kwargs)


urllib.request.OpenerDirector.open = _loopback_only
os.environ["QTE_UPDATE_CHECK"] = "0"
