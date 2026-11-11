import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from countersign.config import load_settings  # noqa: E402
from countersign.events import EventBus  # noqa: E402
from countersign.guard import Guard  # noqa: E402
from countersign.inbox import default_mandate, seed_items  # noqa: E402
from countersign.preview import PreviewLLM, PreviewPayPal  # noqa: E402
from countersign.store import Store  # noqa: E402


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def store(tmp_path, settings):
    store = Store(str(tmp_path / "test.db"))
    store.save_mandate(default_mandate(settings), actor="system")
    store.replace_inbox(seed_items(settings))
    return store


@pytest.fixture
def paypal():
    return PreviewPayPal()


@pytest.fixture
def guard(store, paypal):
    return Guard(store, paypal, PreviewLLM(), EventBus(), "http://localhost:8000")


def run(coroutine):
    return asyncio.run(coroutine)
