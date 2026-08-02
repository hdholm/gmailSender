Minimal stand-ins for the Google client libraries.

They exist so the test suite is hermetic: no network, no OAuth, no
credentials, and no need to `pip install` the real API client just to run
regression tests. `tests/support.py` puts this directory at the front of
sys.path, so these shadow the real packages even when those are installed.

Only the surface `gmail_insert.py` actually touches is implemented.
