"""Tests for the MigrationSwarm CLI."""

from typer.testing import CliRunner

from migrationswarm import __version__
from migrationswarm.cli.main import app

runner = CliRunner()


def test_version_command() -> None:
    """The version option reports the package version."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == f"MigrationSwarm {__version__}"
