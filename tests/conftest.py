import glob
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

FIXTURE_DIR = os.path.join(PROJECT_ROOT, "tests", "fixtures")
FIXTURES = sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.note")))


def fixture_ids():
    return [os.path.basename(p) for p in FIXTURES]


@pytest.fixture(params=FIXTURES, ids=fixture_ids())
def note_path(request):
    return request.param
