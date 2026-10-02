"""Build and smoke-test a MigrationSwarm wheel in a fresh virtual environment."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def run(command: list[str], *, cwd: Path) -> None:
    """Run one validation command and preserve a useful failure boundary."""
    print("$", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="migrationswarm-clean-") as temporary:
        work = Path(temporary)
        dist = work / "dist"
        dist.mkdir()
        run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--sdist",
                "--outdir",
                str(dist),
                str(repo_root),
            ],
            cwd=repo_root,
        )
        wheels = sorted(dist.glob("*.whl"))
        sdists = sorted(dist.glob("*.tar.gz"))
        if len(wheels) != 1:
            raise RuntimeError(f"Expected one wheel, found {len(wheels)}")
        if len(sdists) != 1:
            raise RuntimeError(f"Expected one source distribution, found {len(sdists)}")

        forbidden = (".env", ".migrationswarm", ".venv", ".git/", "artifacts/")
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
        unexpected = [name for name in names if any(token in name for token in forbidden)]
        if unexpected:
            raise RuntimeError(f"Forbidden package entries: {unexpected}")
        with tarfile.open(sdists[0], "r:gz") as archive:
            source_names = archive.getnames()
        source_unexpected = [
            name for name in source_names if any(token in name for token in forbidden)
        ]
        if source_unexpected:
            raise RuntimeError(f"Forbidden source-distribution entries: {source_unexpected}")

        virtualenv = work / ".venv"
        run([sys.executable, "-m", "venv", str(virtualenv)], cwd=repo_root)
        python_name = "python.exe" if os.name == "nt" else "python"
        installed_python = virtualenv / ("Scripts" if os.name == "nt" else "bin") / python_name
        run([str(installed_python), "-m", "pip", "install", str(wheels[0])], cwd=work)
        cli_name = "migrationswarm.exe" if os.name == "nt" else "migrationswarm"
        cli = installed_python.parent / cli_name
        for arguments in (("--version",), ("--help",), ("models",), ("doctor",), ("config-check",)):
            run([str(cli), *arguments], cwd=work)
        demo_fixture = work / "demo-commerce-monolith"
        shutil.copytree(
            repo_root / "examples" / "demo-commerce-monolith",
            demo_fixture,
            ignore=shutil.ignore_patterns("target", ".migrationswarm", ".git"),
        )
        run([str(cli), "demo", str(demo_fixture), "--offline"], cwd=work)
        migration_check = (
            "from pathlib import Path; "
            "from migrationswarm.persistence.db.migrations import migration_directory; "
            "p = migration_directory(); "
            "assert (Path(p) / 'versions' / '0001_initial_persistence.py').is_file()"
        )
        run(
            [
                str(installed_python),
                "-c",
                migration_check,
            ],
            cwd=work,
        )
        run([str(installed_python), "-m", "pip", "check"], cwd=work)
    print("clean installation validation: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
