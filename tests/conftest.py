import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest
from sim import fixtures

@pytest.fixture(scope="session", autouse=True)
def _fixtures():
    fixtures.build()

def has_silero() -> bool:
    try:
        import pysilero_vad  # noqa
        return True
    except Exception:
        return False
