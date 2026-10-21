"""Run unit tests and real PostgreSQL scenarios in an isolated *_tests database."""

import os
import sys
import unittest
from pathlib import Path
from urllib.parse import quote, urlsplit

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "src"))
if not os.environ.get("ALPHA_TEST_DATABASE_URL") and "--unit-only" not in sys.argv:
    password = (root.parent / ".alpha/app-password").read_text(encoding="utf-8").strip()
    os.environ["ALPHA_TEST_DATABASE_URL"] = (
        f"postgresql://roleapp:{quote(password, safe='')}@127.0.0.1:55432/rolealpha_tests"
    )
if os.environ.get("ALPHA_TEST_DATABASE_URL"):
    database = urlsplit(os.environ["ALPHA_TEST_DATABASE_URL"]).path
    if not database.endswith("_tests"):
        raise SystemExit(
            "Refusing integration tests against a database without _tests suffix"
        )
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
if not result.wasSuccessful():
    raise SystemExit(1)
if result.skipped:
    print("Integration scenarios were skipped; this is NOT a full alpha validation.")
    if "--unit-only" not in sys.argv:
        raise SystemExit(1)
