#!/usr/bin/env python3
"""
Run the gmail_insert regression suite.

    python3 tests/run_tests.py              # everything
    python3 tests/run_tests.py retry spool  # only matching modules
    python3 tests/run_tests.py -k backoff   # only matching test names
    python3 tests/run_tests.py -v           # show tracebacks for failures

No dependencies beyond the standard library, so this runs anywhere the
script itself does.  The tests are plain zero-argument functions, so
`pytest tests/` works too if you prefer it.

Exit status is 0 only if every test passes.
"""

import argparse
import importlib
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import support                                              # noqa: E402,F401

TEST_MODULES = ["test_retry", "test_spool", "test_cli"]

GREEN, RED, YELLOW, DIM, RESET = (
    ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m")
    if sys.stdout.isatty() else ("", "", "", "", "")
)


def collect(module_filters, name_filter):
    """(module, name, callable) for every test function that matches."""
    selected = []
    for module_name in TEST_MODULES:
        if module_filters and not any(f in module_name for f in module_filters):
            continue
        module = importlib.import_module(module_name)
        for name in sorted(vars(module)):
            if not name.startswith("test_"):
                continue
            if name_filter and name_filter not in name:
                continue
            func = getattr(module, name)
            if callable(func):
                selected.append((module_name, name, func))
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="*",
                        help="only run modules whose name contains one of these")
    parser.add_argument("-k", dest="name_filter", metavar="SUBSTRING",
                        help="only run tests whose name contains SUBSTRING")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="print a traceback for each failure")
    args = parser.parse_args()

    tests = collect(args.modules, args.name_filter)
    if not tests:
        print("No tests matched.")
        return 1

    passed, failures = 0, []
    current_module = None
    started = time.time()

    for module_name, name, func in tests:
        if module_name != current_module:
            current_module = module_name
            print(f"\n{DIM}{module_name}{RESET}")

        # Each test is isolated: its own temp dirs, its own subprocesses.
        try:
            func()
        except AssertionError as exc:
            failures.append((module_name, name, exc, traceback.format_exc()))
            print(f"  {RED}FAIL{RESET} {name}\n       {exc}")
        except Exception as exc:                            # noqa: BLE001
            failures.append((module_name, name, exc, traceback.format_exc()))
            print(f"  {RED}ERROR{RESET} {name}\n       "
                  f"{type(exc).__name__}: {exc}")
        else:
            passed += 1
            print(f"  {GREEN}pass{RESET} {name}")

    elapsed = time.time() - started
    print(f"\n{'-' * 60}")
    if failures:
        if args.verbose:
            for module_name, name, _, tb in failures:
                print(f"\n{YELLOW}{module_name}.{name}{RESET}\n{tb}")
        print(f"{RED}{len(failures)} failed{RESET}, {passed} passed "
              f"in {elapsed:.1f}s")
        for module_name, name, exc, _ in failures:
            print(f"  - {module_name}.{name}: {exc}")
        return 1

    print(f"{GREEN}{passed} passed{RESET} in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
