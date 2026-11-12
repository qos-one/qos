"""The web Terminal must see the public host and, when set, the local-token path.

Caddy forwards the real Host. The unit loads deploy.env and then terminal.env.
Updates restart qos-api, then qos-mcp, then qos-terminal.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def _env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k] = v
    return out


def _run(script: str) -> None:
    subprocess.run(["bash", "-c", script], cwd=ROOT, check=True)


def test_envfile_writes_host_and_token_path(tmp_path):
    deploy, terminal = tmp_path / "deploy.env", tmp_path / "terminal.env"
    deploy.write_text("QOS_HOME=/srv/qos/home\nQOS_TRUSTED_ORIGINS=https://old.example\n")
    token = tmp_path / "secret dir" / "local_token"
    _run(
        f'. deploy/envfile.sh && persist_local_token_file {str(deploy)!r} {str(token)!r}'
        f' && write_terminal_host_env {str(deploy)!r} {str(terminal)!r} terminal.example.com'
        f' && write_terminal_host_env {str(deploy)!r} {str(terminal)!r} terminal.example.com'
    )
    got = _env(deploy)
    assert got["QOS_HOME"] == "/srv/qos/home"
    assert got["QOS_PUBLIC_TERMINAL"] == "1"
    assert got["QOS_TRUSTED_ORIGINS"] == "https://terminal.example.com"
    assert got["QOS_LOCAL_TOKEN_FILE"] == str(token)
    assert deploy.read_text().count("QOS_TRUSTED_ORIGINS=") == 1
    term = _env(terminal)
    assert term == {
        "QOS_TRUSTED_ORIGINS": "https://terminal.example.com",
        "QOS_TERMINAL_HOST": "terminal.example.com",
    }
    # a second write replaced the line; it did not append another
    assert terminal.read_text().count("QOS_TERMINAL_HOST=") == 1


def test_terminal_unit_loads_engine_env_before_its_own():
    text = (DEPLOY / "systemd" / "qos-terminal.service").read_text().splitlines()
    files = [line.strip() for line in text if line.strip().startswith("EnvironmentFile=")]
    assert files == [
        "EnvironmentFile=-/srv/qos/deploy.env",
        "EnvironmentFile=-/srv/qos/terminal.env",
    ]


def _restart_order(path: Path) -> list[str]:
    order = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for name in ("qos-api", "qos-mcp", "qos-terminal"):
            if f"restart {name}.service" in stripped:
                order.append(name)
    return order


def test_update_restarts_api_then_mcp_then_terminal():
    assert _restart_order(DEPLOY / "install.sh") == ["qos-api", "qos-mcp", "qos-terminal"]
    assert _restart_order(DEPLOY / "deploy-engine.sh") == ["qos-api", "qos-mcp", "qos-terminal"]
    setup = (DEPLOY / "terminal-setup.sh").read_text()
    assert "write_terminal_host_env" in setup
    install = (DEPLOY / "install.sh").read_text()
    assert "persist_local_token_file" in install and "write_terminal_host_env" in install
