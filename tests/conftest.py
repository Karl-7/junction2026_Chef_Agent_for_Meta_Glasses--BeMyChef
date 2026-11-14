import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def scenario():
    return json.loads((ROOT / "scenarios" / "synthetic-stirfry.json").read_text())
