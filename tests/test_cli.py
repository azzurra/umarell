"""Exercise the public CLI without opening network connections."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli_files(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "irc": {"host": "irc.invalid", "channels": ["#team"]},
                "hooks": {
                    "team": {
                        "secret": "sentinel-private-credential",
                        "repositories": [{"id": 123, "channels": ["#team"]}],
                    }
                },
            }
        )
    )
    config.chmod(0o600)
    return config


def run_cli(*arguments, executable=None):
    command = executable or [sys.executable, "-m", "umarell"]
    return subprocess.run(
        [*command, *map(str, arguments)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_module_help():
    result = run_cli("--help")
    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--secrets" not in result.stdout
    assert "--check-config" in result.stdout


def test_required_arguments():
    result = run_cli()
    assert result.returncode == 2
    assert "--config" in result.stderr
    assert "--secrets" not in result.stderr


def test_check_config_without_network(cli_files):
    config = cli_files
    result = run_cli("--config", config, "--check-config")
    assert result.returncode == 0
    assert result.stdout.strip() == "Configuration valid"
    assert result.stderr == ""
    assert "sentinel-private-credential" not in result.stdout + result.stderr


def test_invalid_config_safe_error(cli_files):
    config = cli_files
    config.write_text("irc: secret-input-value\n")
    result = run_cli("--config", config, "--check-config")
    assert result.returncode == 2
    assert "Configuration error:" in result.stderr
    assert "Traceback" not in result.stderr
    assert "secret-input-value" not in result.stderr
    assert "sentinel-private-credential" not in result.stdout + result.stderr


def test_missing_file_safe_error(cli_files):
    config = cli_files
    result = run_cli(
        "--config",
        config.parent / "private-name.yaml",
        "--check-config",
    )
    assert result.returncode == 2
    assert "private-name" not in result.stderr
    assert "Traceback" not in result.stderr


def test_installed_entrypoint(cli_files):
    executable = Path(sys.executable).parent / "umarell"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        pytest.skip("Console entrypoint is not installed in this environment")
    config = cli_files
    result = run_cli(
        "--config",
        config,
        "--check-config",
        executable=[str(executable)],
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "Configuration valid"


def test_legacy_secrets_option_is_rejected(cli_files):
    result = run_cli(
        "--config", cli_files, "--secrets", "legacy.yaml", "--check-config"
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr
