import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolate_local_token(tmp_path, monkeypatch):
    """Keep the admin token file out of the repo data directory."""
    monkeypatch.setenv("QOS_LOCAL_TOKEN_FILE", str(tmp_path / "qos-local-token"))


@pytest.fixture(autouse=True)
def _reset_risk_state():
    """Risk STATE is a module-level variable (one node = one process) → reset per test so nothing leaks between tests"""
    from qx_strategies.risk import STATE
    STATE.reset()
    yield
    STATE.reset()


@pytest.fixture(scope="session")
def ml_example(tmp_path_factory):
    """An ML strategy package made from the SDK template (qos new --ml), with a trained model.joblib."""
    from qos.cli import new_package
    return new_package("ml_example", ml=True, parent=tmp_path_factory.mktemp("ml"), log=lambda *_: None)
