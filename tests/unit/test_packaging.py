"""Packaging, configuration, and environment-diagnosis contracts."""

from importlib.metadata import version as distribution_version
from pathlib import Path

import pytest
from typer.testing import CliRunner

from migrationswarm import __version__
from migrationswarm.cli.main import app
from migrationswarm.config.settings import Settings, get_settings
from migrationswarm.persistence.db.migrations import migration_directory

runner = CliRunner()


def test_package_metadata_version_matches_runtime_version() -> None:
    assert distribution_version("migrationswarm") == __version__


def test_doctor_reports_tools_without_printing_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("migrationswarm.cli.main._command_available", lambda _: True)
    monkeypatch.setenv("GROQ_API_KEY", "super-secret-test-key")
    monkeypatch.setenv("SILICONFLOW_API_KEY", "another-secret-test-key")
    get_settings.cache_clear()

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "MigrationSwarm Doctor" in result.stdout
    assert "configured" in result.stdout
    assert "super-secret-test-key" not in result.stdout
    assert "another-secret-test-key" not in result.stdout
    get_settings.cache_clear()


def test_config_check_accepts_missing_optional_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("SILICONFLOW_API_KEY", raising=False)
    monkeypatch.setattr(
        "migrationswarm.cli.main.get_settings",
        lambda: Settings(_env_file=None),  # type: ignore[call-arg]
    )
    get_settings.cache_clear()

    result = runner.invoke(app, ["config-check"])

    assert result.exit_code == 0
    assert "Configuration: valid" in result.stdout
    assert "groq=unconfigured" in result.stdout
    get_settings.cache_clear()


def test_config_check_reports_malformed_configuration_without_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MIGRATIONSWARM_PORT", "not-a-port")
    monkeypatch.setenv("GROQ_API_KEY", "secret-that-must-not-appear")
    get_settings.cache_clear()

    result = runner.invoke(app, ["config-check"])

    assert result.exit_code == 1
    assert "Configuration: invalid" in result.output
    assert "not-a-port" not in result.output
    assert "secret-that-must-not-appear" not in result.output
    get_settings.cache_clear()


def test_process_environment_overrides_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MIGRATIONSWARM_ENVIRONMENT=from-file\n", encoding="utf-8")
    monkeypatch.setenv("MIGRATIONSWARM_ENVIRONMENT", "from-process")

    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]

    assert settings.environment == "from-process"


def test_alembic_migrations_are_available() -> None:
    migrations = migration_directory()

    assert migrations.is_dir()
    assert (migrations / "env.py").is_file()
    assert (migrations / "versions" / "0001_initial_persistence.py").is_file()
