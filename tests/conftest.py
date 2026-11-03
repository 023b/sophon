import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# every test runs against a throwaway home: no config, runs or keys of the person running the tests are touched
os.environ["SOPHON_HOME"] = tempfile.mkdtemp(prefix="sophon-test-")
os.environ.pop("SOPHON_CONFIG", None)
os.environ.pop("OPENROUTER_API_KEY", None)


@pytest.fixture(scope="session")
def sw():
    import sophon.api
    return sophon.api
