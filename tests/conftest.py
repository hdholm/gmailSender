"""
pytest bootstrap.

The suite is plain zero-argument functions with no pytest dependency, so it
also runs under `python3 tests/run_tests.py`.  This file only guarantees
that `import support` resolves under pytest whatever the rootdir happens to
be; support.py does the real work of putting the stubs and the repository
root on sys.path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support                                              # noqa: E402,F401
