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

_EXAMPLES_DIR = Path(__import__("os").environ.get("QOS_EXAMPLES") or Path(__file__).resolve().parents[2] / "qos-examples")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    """Tests that copy example packages need a qos-examples checkout (CI sets QOS_EXAMPLES).
    Without one, report them as skipped with a hint instead of failing."""
    report = yield
    exc = call.excinfo
    if (report.failed and exc is not None and exc.errisinstance(FileNotFoundError)
            and not _EXAMPLES_DIR.exists() and str(_EXAMPLES_DIR) in str(exc.value)):
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0,
                           "Skipped: needs a qos-examples checkout; clone it next to qos or set QOS_EXAMPLES")
    return report
