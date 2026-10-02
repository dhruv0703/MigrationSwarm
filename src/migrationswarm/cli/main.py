"""MigrationSwarm command-line interface."""

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from uuid import UUID, uuid4

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from migrationswarm import __version__
from migrationswarm.agents.architecture_analysis import (
    ARCHITECTURE_REPORT_ARTIFACT,
    JAVA_DEPENDENCY_DOT_ARTIFACT,
    ArchitectureAnalysisAgent,
    ArchitectureAnalysisError,
)
from migrationswarm.agents.build_verification import (
    VERIFICATION_RESULTS_DIR,
    BuildVerificationAgent,
    BuildVerificationError,
    BuildVerificationResult,
)
from migrationswarm.agents.debug import DebugAgent
from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    DependencyAnalysisAgent,
    DependencyAnalysisError,
)
from migrationswarm.agents.migration_planning import (
    MIGRATION_PLAN_JSON_ARTIFACT,
    MigrationPlan,
    MigrationPlanningAgent,
    MigrationPlanningError,
)
from migrationswarm.agents.refactor import (
    RefactorAgent,
    RefactorError,
)
from migrationswarm.agents.repository_analysis import (
    INVENTORY_ARTIFACT,
    RepositoryAnalysisAgent,
    RepositoryAnalysisError,
)
from migrationswarm.agents.service_boundary import (
    SERVICE_BOUNDARIES_JSON_ARTIFACT,
    ServiceBoundaryAgent,
    ServiceBoundaryAnalysisError,
)
from migrationswarm.agents.service_extraction import (
    ServiceExtractionAgent,
    ServiceExtractionError,
)
from migrationswarm.config import get_settings
from migrationswarm.core.agents import AgentRegistry
from migrationswarm.core.agents.models import AgentContext
from migrationswarm.core.git import GitRepository, GitRepositoryError, GitWorktreeManager
from migrationswarm.core.models import (
    ModelCapability,
    ModelProviderError,
    UnknownProviderError,
)
from migrationswarm.core.models.registry import default_model_registry
from migrationswarm.core.observability.models import MigrationBenchmark
from migrationswarm.core.orchestrator import (
    MigrationOrchestrator,
    MigrationRunStatus,
    MultiServiceMigrationError,
    MultiServiceMigrationOrchestrator,
    MultiServiceMigrationRun,
    MultiServiceMigrationStatus,
)
from migrationswarm.core.orchestrator.approvals import load_approvals, update_approval
from migrationswarm.core.recovery import RecoveryInspector
from migrationswarm.core.security import PathSafetyError, redact_text, safe_join
from migrationswarm.core.tasks.enums import TaskStatus, TaskType
from migrationswarm.core.tasks.models import Task
from migrationswarm.core.validation import ArtifactConsistencyValidator
from migrationswarm.core.verification import (
    VerificationCoordinator,
    VerificationCoordinatorError,
    VerificationEvidence,
)
from migrationswarm.demo import DemoError, DemoResult, run_demo
from migrationswarm.evaluation import discover_fixtures, run_benchmark
from migrationswarm.performance import run_performance_benchmark
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.migrations import current_revision, upgrade_database
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    MigrationRunRepository,
    ProjectRepository,
    RepairAttemptRepository,
    TaskRepository,
)
from migrationswarm.persistence.exceptions import PersistenceError, RedisCoordinationError
from migrationswarm.persistence.redis import AgentHeartbeat, ReadyTaskQueue, RedisClient, TaskLock
from migrationswarm.persistence.service import MigrationStateService
from migrationswarm.providers import default_provider_registry
from migrationswarm.workers import SwarmCoordinator, WorkerConfig, WorkerExecutionError

app = typer.Typer(add_completion=False)
console = Console()


def _safe_error(error: Exception) -> str:
    """Render concise CLI errors without echoing credentials or URLs."""
    return redact_text(str(error))[:500]


def _default_swarm_registry() -> AgentRegistry:
    """Register existing deterministic agents without introducing model calls."""
    registry = AgentRegistry()
    for agent in (
        RepositoryAnalysisAgent(),
        DependencyAnalysisAgent(),
        ArchitectureAnalysisAgent(),
        BuildVerificationAgent(),
        DebugAgent(),
    ):
        registry.register(agent)
    return registry


@app.command("db-status")
def db_status() -> None:
    """Show database configuration, connectivity, and Alembic revision."""
    settings = get_settings()
    typer.echo(f"configured: {'yes' if settings.database_url else 'no'}")
    if not settings.database_url:
        typer.echo("connectivity: unavailable")
        typer.echo("migration: unknown")
        raise typer.Exit(code=1) from None
    database = Database(settings.database_url)
    try:
        database.check_connectivity()
        revision = current_revision(settings.database_url)
    except PersistenceError:
        typer.echo("connectivity: unavailable")
        typer.echo("migration: unknown")
        raise typer.Exit(code=1) from None
    finally:
        database.dispose()
    typer.echo("connectivity: ok")
    typer.echo(f"migration: {revision or 'not upgraded'}")


@app.command("db-upgrade")
def db_upgrade() -> None:
    """Apply committed Alembic migrations to the configured database."""
    settings = get_settings()
    try:
        upgrade_database(settings.database_url)
    except PersistenceError as error:
        console.print(f"[red]Database upgrade failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    typer.echo("database migration: upgraded")


@app.command("redis-status")
def redis_status() -> None:
    """Show Redis transient-coordination connectivity without printing its URL."""
    settings = get_settings()
    typer.echo(f"configured: {'yes' if settings.redis_url else 'no'}")
    if not settings.redis_url:
        typer.echo("connectivity: unavailable")
        raise typer.Exit(code=1) from None
    client = RedisClient(settings.redis_url)
    try:
        client.ping()
    except RedisCoordinationError:
        typer.echo("connectivity: unavailable")
        raise typer.Exit(code=1) from None
    finally:
        client.close()
    typer.echo("connectivity: ok")


@app.command("workers")
def list_workers(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Repository path associated with the worker pool."
    ),
) -> None:
    """List live worker heartbeats without exposing Redis configuration."""
    settings = get_settings()
    if not settings.redis_url:
        typer.echo("configured: no")
        raise typer.Exit(code=1) from None
    client = RedisClient(settings.redis_url)
    try:
        live = AgentHeartbeat(client.client).list_heartbeats()
    except RedisCoordinationError as error:
        console.print(f"[red]Worker status failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    finally:
        client.close()
    typer.echo(f"repository: {path.resolve()}")
    if not live:
        typer.echo("workers: 0")
        return
    typer.echo(f"workers: {len(live)}")
    for heartbeat in live:
        typer.echo(
            f"worker={heartbeat.worker_id or heartbeat.agent_name} "
            f"status={heartbeat.status} pid={heartbeat.process_id or 'n/a'} "
            f"task={heartbeat.task_id or 'none'}"
        )


@app.command("recovery-status")
def recovery_status(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Registered repository whose durable task state should be inspected."
    ),
) -> None:
    """Inspect interrupted work conservatively without replaying or mutating tasks."""
    settings = get_settings()
    if not settings.database_url:
        typer.echo("database: unavailable", err=True)
        raise typer.Exit(code=1)
    database = Database(settings.database_url)
    redis_client = RedisClient(settings.redis_url) if settings.redis_url else None
    try:
        project = ProjectRepository(database.session_factory).find_by_repository_path(
            str(path.expanduser().resolve())
        )
        if project is None:
            typer.echo("project not found for repository path", err=True)
            raise typer.Exit(code=1)
        tasks = TaskRepository(database.session_factory).list(project_id=project.id)
        active_task_ids = set()
        if redis_client is not None:
            try:
                active_task_ids = {
                    heartbeat.task_id
                    for heartbeat in AgentHeartbeat(redis_client.client).list_heartbeats()
                    if heartbeat.task_id is not None
                }
            except RedisCoordinationError:
                active_task_ids = set()
        root = path.expanduser().resolve()
        preserved = {
            task.id
            for task in tasks
            if (root / ".migrationswarm" / "worktrees" / str(task.id)).is_dir()
        }
        findings = RecoveryInspector().inspect(
            tasks,
            active_task_ids=active_task_ids,
            preserved_worktree_ids=preserved,
        )
    except (PersistenceError, OSError) as error:
        console.print(f"[red]Recovery inspection failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    finally:
        if redis_client is not None:
            redis_client.close()
        database.dispose()
    typer.echo("automatic_recovery=no")
    typer.echo(f"findings={len(findings)}")
    for finding in findings:
        typer.echo(
            f"task={finding.task_id} status={finding.status.value} "
            f"recommendation={finding.recommendation.value} "
            f"heartbeat={'yes' if finding.active_heartbeat else 'no'} "
            f"worktree={'yes' if finding.worktree_preserved else 'no'}"
        )


@app.command("security-check")
def security_check() -> None:
    """Run local security policy checks without network calls or secrets."""
    checks: list[tuple[str, bool]] = []
    try:
        safe_join(Path.cwd(), "security-check.txt")
        checks.append(("path_containment", True))
    except PathSafetyError:
        checks.append(("path_containment", False))
    package_root = Path(__file__).resolve().parents[1]
    source_text = "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in package_root.rglob("*.py")
    )
    shell_marker = "shell=" + "True"
    checks.append(("subprocess_shell_policy", shell_marker not in source_text))
    checks.append(
        (
            "destructive_git_policy",
            not any(
                token in source_text
                for token in (
                    "git reset " + "--hard",
                    "git clean " + "-fd",
                    "git " + "push",
                )
            ),
        )
    )
    checks.append(
        (
            "secret_scan",
            "sk-" + "live-" not in source_text and "Bearer " + "sk-" not in source_text,
        )
    )
    for name, passed in checks:
        typer.echo(f"{name}={'ok' if passed else 'failed'}")
    typer.echo("recovery=manual_only")
    if not all(passed for _, passed in checks):
        raise typer.Exit(code=1)
    typer.echo("status=ok")


@app.command("validate-run")
def validate_run(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Repository whose generated run artifacts are inspected."
    ),  # noqa: B008
    run_id: UUID | None = typer.Option(  # noqa: B008
        None, "--run-id", help="Validate one run UUID; otherwise use the latest run."
    ),  # noqa: B008
) -> None:
    """Validate run artifact lineage without repairing or regenerating anything."""
    report = ArtifactConsistencyValidator().validate(path, run_id=run_id)
    typer.echo(f"repository={report.repository}")
    typer.echo(f"run_id={report.run_id or 'latest'}")
    typer.echo(f"checked_files={len(report.checked_files)}")
    typer.echo(f"status={'ok' if report.valid else 'invalid'}")
    for finding in report.findings[:10]:
        location = f" path={finding.path}" if finding.path else ""
        typer.echo(f"finding={finding.category}:{finding.message}{location}", err=True)
    if not report.valid:
        raise typer.Exit(code=1)


@app.command("release-check")
def release_check(
    full: bool = typer.Option(
        False, "--full", help="Include repeatability, soak, and expensive checks."
    ),
    skip_expensive: bool = typer.Option(False, "--skip-expensive"),
    skip_soak: bool = typer.Option(False, "--skip-soak"),
) -> None:
    """Run the repository's bounded release-candidate validation harness."""
    script = Path(__file__).resolve().parents[3] / "scripts" / "validate_release_candidate.py"
    command = [sys.executable, str(script)]
    if full:
        command.append("--full")
    if skip_expensive:
        command.append("--skip-expensive")
    if skip_soak:
        command.append("--skip-soak")
    try:
        result = subprocess.run(command, cwd=script.parents[1], check=False, shell=False)
    except OSError as error:
        console.print(f"[red]Release check failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    if result.returncode:
        raise typer.Exit(code=result.returncode)


@app.command("run-swarm")
def run_swarm(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to a registered MigrationSwarm repository."
    ),
    workers: int = typer.Option(2, "--workers", min=1, max=4),
    verification_workers: int = typer.Option(1, "--verification-workers", min=1, max=2),
    poll_interval: float = typer.Option(0.05, "--poll-interval", min=0.0),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show durable task counts without promotion, execution, or model calls.",
    ),
) -> None:
    """Run one finite, bounded local worker swarm for a registered project."""
    settings = get_settings()
    if not settings.database_url or not settings.redis_url:
        typer.echo("database and Redis must both be configured")
        raise typer.Exit(code=1) from None
    database = Database(settings.database_url)
    redis_client = RedisClient(settings.redis_url)
    try:
        project_repository = ProjectRepository(database.session_factory)
        repository_path = str(path.resolve())
        project = project_repository.find_by_repository_path(repository_path)
        if project is None:
            project = project_repository.find_by_repository_path(str(path))
        if project is None:
            typer.echo("project not found for repository path")
            raise typer.Exit(code=1) from None
        task_repository = TaskRepository(database.session_factory)
        execution_repository = AgentExecutionRepository(database.session_factory)
        repair_repository = RepairAttemptRepository(database.session_factory)
        coordinator = SwarmCoordinator(
            task_repository,
            execution_repository,
            ReadyTaskQueue(redis_client.client),
            TaskLock(redis_client.client),
            AgentHeartbeat(redis_client.client),
            _default_swarm_registry(),
            max_workers=workers,
            verification_workers=verification_workers,
            worker_config=WorkerConfig(poll_interval_seconds=poll_interval),
            workspace_path=path.resolve(),
            state_service=MigrationStateService(
                project_repository,
                task_repository,
                MigrationRunRepository(database.session_factory),
                execution_repository,
                repair_repository,
            ),
            repair_repository=repair_repository,
        )
        result = (
            coordinator.dry_run(project.id)
            if dry_run
            else coordinator.run(project.id, poll_interval_seconds=poll_interval)
        )
    except (PersistenceError, RedisCoordinationError, WorkerExecutionError) as error:
        console.print(f"[red]Swarm failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    finally:
        redis_client.close()
        database.dispose()

    latest = result.dispatches[-1]
    verification_latest = result.verification_dispatches[-1]
    typer.echo(f"project={result.project_id}")
    typer.echo(f"pending={len(result.pending_task_ids)}")
    typer.echo(f"ready={len(result.ready_task_ids)}")
    typer.echo(f"enqueued={latest.enqueued_count}")
    typer.echo(f"workers={workers}")
    typer.echo(f"verification_eligible={verification_latest.eligible_count}")
    typer.echo(f"verification_workers={verification_workers}")
    latest_debug = result.debug_dispatches[-1] if result.debug_dispatches else None
    if latest_debug is not None:
        typer.echo(f"debug_eligible={latest_debug.eligible_count}")
        typer.echo(f"debug_repair_dispatches={latest_debug.enqueued_count}")
    typer.echo(f"execution={'no' if dry_run else 'yes'}")
    typer.echo(f"verification_mutation={'no' if dry_run else 'yes'}")
    typer.echo("model_calls=no" if dry_run else "model_calls=only through assigned agents")


@app.command("swarm-status")
def swarm_status(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to a registered MigrationSwarm repository."
    ),
) -> None:
    """Show durable task counts and available transient worker heartbeats."""
    settings = get_settings()
    if not settings.database_url:
        typer.echo("database: unavailable")
        raise typer.Exit(code=1) from None
    database = Database(settings.database_url)
    redis_client = RedisClient(settings.redis_url) if settings.redis_url else None
    try:
        projects = ProjectRepository(database.session_factory)
        project = projects.find_by_repository_path(str(path.resolve()))
        if project is None:
            project = projects.find_by_repository_path(str(path))
        if project is None:
            typer.echo("project not found for repository path")
            raise typer.Exit(code=1) from None
        tasks = TaskRepository(database.session_factory).list(project_id=project.id)
        for status in TaskStatus:
            count = sum(task.status is status for task in tasks)
            typer.echo(f"{status.name}={count}")
        repair_repository = RepairAttemptRepository(database.session_factory)
        attempts = [
            attempt for task in tasks for attempt in repair_repository.list_for_original(task.id)
        ]
        pending_repairs = sum(attempt.status.value == "pending" for attempt in attempts)
        exhausted_repairs = sum(
            attempt.status.value in {"exhausted", "human_review"} for attempt in attempts
        )
        typer.echo(f"REPAIR_PENDING={pending_repairs}")
        running_repairs = sum(attempt.status.value == "running" for attempt in attempts)
        typer.echo(f"REPAIR_RUNNING={running_repairs}")
        typer.echo(f"REPAIR_EXHAUSTED={exhausted_repairs}")
        if redis_client is None:
            typer.echo("heartbeats=unavailable")
        else:
            try:
                heartbeats = AgentHeartbeat(redis_client.client).list_heartbeats()
            except RedisCoordinationError:
                typer.echo("heartbeats=unavailable")
            else:
                typer.echo(f"heartbeats={len(heartbeats)}")
                for heartbeat in heartbeats:
                    typer.echo(
                        f"worker={heartbeat.worker_id or heartbeat.agent_name} "
                        f"status={heartbeat.status} pid={heartbeat.process_id or 'n/a'} "
                        f"task={heartbeat.task_id or 'none'}"
                    )
    except PersistenceError as error:
        console.print(f"[red]Swarm status failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    finally:
        if redis_client is not None:
            redis_client.close()
        database.dispose()


@app.command("repair-status")
def repair_status(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to a registered MigrationSwarm repository."
    ),
) -> None:
    """Show bounded repair attempts without printing paths, prompts, or secrets."""
    settings = get_settings()
    if not settings.database_url:
        typer.echo("database: unavailable")
        raise typer.Exit(code=1) from None
    database = Database(settings.database_url)
    try:
        projects = ProjectRepository(database.session_factory)
        project = projects.find_by_repository_path(str(path.resolve()))
        if project is None:
            project = projects.find_by_repository_path(str(path))
        if project is None:
            typer.echo("project not found for repository path")
            raise typer.Exit(code=1) from None
        tasks = TaskRepository(database.session_factory).list(project_id=project.id)
        repository = RepairAttemptRepository(database.session_factory)
        attempts = [attempt for task in tasks for attempt in repository.list_for_original(task.id)]
        typer.echo(f"attempts={len(attempts)}")
        for attempt in attempts:
            typer.echo(
                f"original_task={attempt.original_task_id} "
                f"debug_task={attempt.debug_task_id} "
                f"attempt={attempt.attempt_number} "
                f"category={attempt.failure_category} "
                f"status={attempt.status.value} "
                f"provider={attempt.model_provider or 'none'} "
                f"model={attempt.model_name or 'none'}"
            )
    except PersistenceError as error:
        console.print(f"[red]Repair status failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    finally:
        database.dispose()


def version_callback(value: bool) -> None:
    """Print the installed package version and exit."""
    if value:
        typer.echo(f"MigrationSwarm {__version__}")
        raise typer.Exit


@app.callback(invoke_without_command=True)
def cli_callback(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the MigrationSwarm version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """MigrationSwarm command-line tools."""


def _safe_settings_error(error: ValidationError) -> str:
    """Render validation locations and messages without echoing input values."""
    details: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item.get("loc", ())) or "settings"
        details.append(f"{location}: {item.get('msg', 'invalid value')}")
    return "; ".join(details) or "invalid configuration"


def _command_available(command: str) -> bool:
    return shutil.which(command) is not None


@app.command("doctor")
def doctor() -> None:
    """Diagnose the local environment without making network or database calls."""
    typer.echo("MigrationSwarm Doctor")
    typer.echo("Component      Scope      Value                 Status")
    required_failed = False

    python_ok = sys.version_info >= (3, 11)
    required_failed |= not python_ok
    typer.echo(
        f"Python         REQUIRED   {sys.version.split()[0]:<20} "
        f"{'ok' if python_ok else 'unsupported'}"
    )
    for label, command in (("Git", "git"), ("Java", "java"), ("Maven", "mvn")):
        available = _command_available(command)
        required_failed |= not available
        typer.echo(
            f"{label:<14} {'REQUIRED':<10} {('available' if available else 'unavailable'):<20} "
            f"{'ok' if available else 'missing'}"
        )

    docker_available = _command_available("docker")
    typer.echo(
        f"{'Docker':<14} {'OPTIONAL':<10} "
        f"{('available' if docker_available else 'unavailable'):<20} "
        f"{'ok' if docker_available else 'optional'}"
    )

    try:
        settings = get_settings()
    except ValidationError as error:
        settings = None
        typer.echo(
            f"{'Configuration':<14} {'REQUIRED':<10} {'invalid':<20} {_safe_settings_error(error)}"
        )
        required_failed = True

    if settings is not None:
        database_value = "configured" if settings.database_url else "unconfigured"
        redis_value = "configured" if settings.redis_url else "unconfigured"
        groq_value = "configured" if settings.groq_api_key else "unavailable"
        siliconflow_value = "configured" if settings.siliconflow_api_key else "unavailable"
        for label, value in (
            ("Database", database_value),
            ("Redis", redis_value),
            ("Groq", groq_value),
            ("SiliconFlow", siliconflow_value),
        ):
            status = "ok" if value == "configured" else "optional"
            typer.echo(f"{label:<14} {'OPTIONAL':<10} {value:<20} {status}")

    typer.echo(f"{'Project':<14} {'REQUIRED':<10} {__version__:<20} ok")
    if required_failed:
        raise typer.Exit(code=1)


@app.command("config-check")
def config_check() -> None:
    """Validate local settings without printing URLs, keys, or other secrets."""
    try:
        settings = get_settings()
    except ValidationError as error:
        typer.echo("Configuration: invalid", err=True)
        typer.echo(_safe_settings_error(error), err=True)
        raise typer.Exit(code=1) from error

    typer.echo("Configuration: valid")
    typer.echo(f"app_name={settings.app_name}")
    typer.echo(f"environment={settings.environment}")
    typer.echo(f"database={'configured' if settings.database_url else 'unconfigured'}")
    typer.echo(f"redis={'configured' if settings.redis_url else 'unconfigured'}")
    typer.echo(f"groq={'configured' if settings.groq_api_key else 'unconfigured'}")
    typer.echo(f"siliconflow={'configured' if settings.siliconflow_api_key else 'unconfigured'}")
    typer.echo(
        "cross_provider_fallback="
        f"{'enabled' if settings.allow_cross_provider_fallback else 'disabled'}"
    )


@app.command("analyze-repo")
def analyze_repo(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the local repository to analyze."
    ),
) -> None:
    """Analyze a local repository and write its structured inventory."""
    agent = RepositoryAnalysisAgent()
    task = Task(
        project_id=uuid4(),
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="Analyze repository",
        description="Create a deterministic repository inventory.",
        status=TaskStatus.READY,
        assigned_agent=agent.name,
    )
    context = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(path),
    )
    try:
        result = agent.execute(task, context)
    except RepositoryAnalysisError as error:
        console.print(f"[red]Repository analysis failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    inventory = result.metadata["inventory"]
    table = Table(title="Repository Inventory")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Repository", escape(str(inventory["repository_root"])))
    table.add_row("Files", str(inventory["total_file_count"]))
    table.add_row("Directories", str(inventory["total_directory_count"]))
    table.add_row("Languages", ", ".join(inventory["detected_languages"]) or "None")
    table.add_row("Java versions", ", ".join(inventory["java_version_hints"]) or "Unknown")
    table.add_row("Tests", str(inventory["test_file_count"]))
    table.add_row("Frameworks", ", ".join(inventory["detected_frameworks"]) or "None")
    console.print(table)
    console.print(f"Inventory artifact: [cyan]{INVENTORY_ARTIFACT}[/cyan]")


@app.command("analyze-dependencies")
def analyze_dependencies(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the local Java repository to analyze."
    ),
) -> None:
    """Analyze local Java source dependencies and write a structured graph."""
    agent = DependencyAnalysisAgent()
    task = Task(
        project_id=uuid4(),
        task_type=TaskType.DEPENDENCY_ANALYSIS,
        title="Analyze Java dependencies",
        description="Create a deterministic Java source dependency graph.",
        status=TaskStatus.READY,
        assigned_agent=agent.name,
    )
    context = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(path),
    )
    try:
        result = agent.execute(task, context)
    except DependencyAnalysisError as error:
        console.print(f"[red]Dependency analysis failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    graph = result.metadata["dependency_graph"]
    roles = ", ".join(f"{role}: {count}" for role, count in graph["role_counts"].items()) or "None"
    class_roles = {item["fully_qualified_name"]: item["role"] for item in graph["classes"]}
    role_flows = Counter(
        f"{class_roles.get(edge['source'], 'OTHER')} -> "
        f"{class_roles.get(edge['target'], 'OTHER')}"
        for edge in graph["dependencies"]
    )
    top_flows = (
        ", ".join(f"{flow}: {count}" for flow, count in sorted(role_flows.items())) or "None"
    )
    table = Table(title="Java Dependency Analysis")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Repository", escape(str(graph["repository_root"])))
    table.add_row("Java classes", str(graph["total_java_classes"]))
    table.add_row("Dependencies", str(graph["total_dependency_edges"]))
    table.add_row("Roles", roles)
    table.add_row("Top relationships", top_flows)
    table.add_row("Warnings", str(len(graph["warnings"])))
    console.print(table)
    console.print(f"Dependency graph artifact: [cyan]{JAVA_DEPENDENCY_ARTIFACT}[/cyan]")


@app.command("analyze-architecture")
def analyze_architecture(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the local Java repository to analyze."
    ),
) -> None:
    """Analyze Java dependency structure and write architecture artifacts."""
    agent = ArchitectureAnalysisAgent()
    task = Task(
        project_id=uuid4(),
        task_type=TaskType.ARCHITECTURE_ANALYSIS,
        title="Analyze architecture",
        description="Calculate deterministic architecture metrics.",
        status=TaskStatus.READY,
        assigned_agent=agent.name,
    )
    context = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(path),
    )
    try:
        result = agent.execute(task, context)
    except ArchitectureAnalysisError as error:
        console.print(f"[red]Architecture analysis failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    report = result.metadata["architecture_report"]
    coupled_classes = [
        metric["name"]
        for metric in sorted(
            report["class_metrics"],
            key=lambda item: (
                -(item["fan_in"] + item["fan_out"]),
                item["fully_qualified_name"],
            ),
        )[:5]
    ] or ["None"]
    table = Table(title="Architecture Analysis")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Classes", str(report["total_classes"]))
    table.add_row("Dependencies", str(report["total_dependencies"]))
    table.add_row("Connected components", str(report["connected_component_count"]))
    table.add_row("Cycles", str(report["cycle_count"]))
    table.add_row("Candidate components", str(len(report["components"])))
    table.add_row("Risks", str(len(report["risks"])))
    table.add_row("Most coupled classes", "\n".join(coupled_classes[:5]))
    console.print(table)
    console.print(f"Architecture report: [cyan]{ARCHITECTURE_REPORT_ARTIFACT}[/cyan]")
    console.print(f"Graphviz DOT: [cyan]{JAVA_DEPENDENCY_DOT_ARTIFACT}[/cyan]")


@app.command("propose-boundaries")
def propose_boundaries(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the local Java repository to analyze."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Prepare evidence without making an external model call.",
    ),
) -> None:
    """Propose candidate service boundaries from architecture evidence."""
    agent = ServiceBoundaryAgent()
    if dry_run:
        try:
            evidence = agent.prepare_evidence(path)
        except ServiceBoundaryAnalysisError as error:
            console.print(f"[red]Boundary preparation failed:[/red] {escape(_safe_error(error))}")
            raise typer.Exit(code=1) from error
        console.print("Service Boundary Proposal (dry run)")
        console.print("Capability: architecture")
        console.print(f"Structured input bytes: {agent.input_size(evidence)}")
        console.print("External model call: no")
        return

    task = Task(
        project_id=uuid4(),
        task_type=TaskType.SERVICE_BOUNDARY_ANALYSIS,
        title="Propose service boundaries",
        description="Propose grounded candidate service boundaries.",
        status=TaskStatus.READY,
        assigned_agent=agent.name,
    )
    context = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(path),
    )
    try:
        result = agent.execute(task, context)
    except ServiceBoundaryAnalysisError as error:
        console.print(f"[red]Boundary proposal failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    report = result.metadata["service_boundary_report"]
    console.print("Service Boundary Proposal")
    table = Table(title="Service Boundary Proposal")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Candidates", str(len(report["candidate_services"])))
    table.add_row("Shared", str(len(report["shared_components"])))
    table.add_row("Unresolved", str(len(report["unresolved_classes"])))
    console.print(table)
    for candidate in report["candidate_services"]:
        console.print(f"Candidate: {escape(candidate['name'])}")
        console.print(f"Confidence: {candidate['confidence']:.2f}")
        console.print(f"Classes: {len(candidate['classes'])}")
    console.print(f"JSON artifact: [cyan]{SERVICE_BOUNDARIES_JSON_ARTIFACT}[/cyan]")


@app.command("plan-migration")
def plan_migration(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the local Java repository to plan."
    ),
    service: str = typer.Option(
        ..., "--service", help="Exact candidate service name from service-boundaries.json."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Prepare and validate evidence without making an external model call.",
    ),
    show_dag: bool = typer.Option(
        False,
        "--show-dag",
        help="Render the validated migration task DAG as text.",
    ),
) -> None:
    """Create a grounded migration plan for one candidate service."""
    agent = MigrationPlanningAgent()
    if dry_run:
        try:
            evidence = agent.prepare_evidence(path, service, allow_model=False)
        except MigrationPlanningError as error:
            console.print(
                f"[red]Migration planning preparation failed:[/red] "
                f"{escape(_safe_error(error))}"
            )
            raise typer.Exit(code=1) from error
        console.print("Migration Plan (dry run)")
        console.print(f"Candidate service: {escape(service)}")
        console.print("Capability: reasoning")
        console.print(f"Structured input bytes: {agent.input_size(evidence)}")
        console.print("Planned model call: yes")
        console.print("External model call: no")
        if show_dag:
            console.print("Task DAG: unavailable during dry run; planning model call skipped")
        return

    task = Task(
        project_id=uuid4(),
        task_type=TaskType.MIGRATION_PLANNING,
        title=f"Plan migration for {service}",
        description="Create a grounded dependency-aware migration plan.",
        status=TaskStatus.READY,
        assigned_agent=agent.name,
    )
    context = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(path),
        metadata={"candidate_service": service},
    )
    try:
        result = agent.execute(task, context)
    except MigrationPlanningError as error:
        console.print(f"[red]Migration planning failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    plan = MigrationPlan.model_validate(result.metadata["migration_plan"])
    console.print("Migration Plan")
    table = Table(title="Migration Plan")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", overflow="fold")
    table.add_row("Candidate service", escape(plan.candidate_service))
    table.add_row("Phases", str(len(plan.phases)))
    table.add_row("Steps", str(len(plan.steps)))
    table.add_row(
        "High-risk steps",
        str(sum(step.risk_level.value == "high" for step in plan.steps)),
    )
    table.add_row("Human review points", str(len(plan.human_review_points)))
    console.print(table)
    console.print(f"JSON artifact: [cyan]{MIGRATION_PLAN_JSON_ARTIFACT}[/cyan]")
    if show_dag:
        console.print(agent.render_task_dag(plan))


@app.command("git-status")
def git_status(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path inside the Git repository to inspect."
    ),
) -> None:
    """Show safe read-only Git repository status."""
    try:
        repository = GitRepository.from_path(path)
        typer.echo(f"repository root={repository.root}")
        typer.echo(f"branch={repository.current_branch()}")
        typer.echo(f"HEAD={repository.head_commit()}")
        typer.echo(f"status={'dirty' if repository.is_dirty() else 'clean'}")
    except GitRepositoryError as error:
        console.print(f"[red]Git status failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error


@app.command("create-worktree")
def create_worktree(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path inside the Git repository."
    ),
    task_id: UUID = typer.Option(  # noqa: B008
        ..., "--task-id", help="Task UUID owning the worktree."
    ),
) -> None:
    """Create one isolated worktree for a task."""
    try:
        worktree = GitWorktreeManager(path).create_worktree(task_id)
    except GitRepositoryError as error:
        console.print(f"[red]Worktree creation failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    typer.echo(f"task_id={worktree.task_id}")
    typer.echo(f"branch={worktree.branch_name}")
    typer.echo(f"path={worktree.path}")
    typer.echo(f"base_commit={worktree.base_commit}")


@app.command("worktrees")
def list_worktrees(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path inside the Git repository."
    ),
) -> None:
    """List MigrationSwarm-managed task worktrees."""
    try:
        worktrees = GitWorktreeManager(path).list_worktrees()
    except GitRepositoryError as error:
        console.print(f"[red]Worktree listing failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    if not worktrees:
        console.print("No managed worktrees.")
        return
    typer.echo("MigrationSwarm Worktrees")
    for worktree in worktrees:
        typer.echo(
            f"task_id={worktree.task_id} branch={worktree.branch_name} "
            f"status={worktree.status.value} path={worktree.path}"
        )


@app.command("remove-worktree")
def remove_worktree(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path inside the Git repository."
    ),
    task_id: UUID = typer.Option(  # noqa: B008
        ..., "--task-id", help="Task UUID owning the worktree."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Explicitly discard dirty task-worktree content.",
    ),
) -> None:
    """Remove one managed task worktree safely."""
    try:
        GitWorktreeManager(path).remove_worktree(task_id, force=force)
    except GitRepositoryError as error:
        console.print(f"[red]Worktree removal failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error
    typer.echo(f"removed task_id={task_id}")


@app.command("refactor")
def refactor(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the main Git repository."
    ),
    task_id: UUID = typer.Option(  # noqa: B008
        ..., "--task-id", help="Existing task worktree UUID."
    ),
    instruction: str = typer.Option(  # noqa: B008
        ..., "--instruction", help="Narrow refactoring objective."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the proposal without writing source or artifacts.",
    ),
) -> None:
    """Run the bounded RefactorAgent inside an existing task worktree."""
    try:
        manager = GitWorktreeManager(path)
        workspace = manager.task_workspace(task_id)
        agent = RefactorAgent()
        task = Task(
            id=task_id,
            project_id=uuid4(),
            task_type=TaskType.CODE_REFACTOR,
            title="Refactor isolated source",
            description=instruction,
            status=TaskStatus.READY,
            assigned_agent=agent.name,
        )
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.workspace_path),
            metadata={"instruction": instruction},
        )
        result = agent.dry_run(task, context) if dry_run else agent.execute(task, context)
    except (GitRepositoryError, RefactorError, ModelProviderError) as error:
        console.print(f"[red]Refactor failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    if dry_run:
        typer.echo("Refactor dry run")
        typer.echo("source_writes=no")
        typer.echo("intended_files=" + ",".join(result.metadata.get("intended_files", [])))
        return

    changed_files = result.metadata.get("changed_files", [])
    diff = str(result.metadata.get("diff", ""))
    typer.echo("Refactor applied")
    typer.echo("changed_files=" + ",".join(changed_files))
    typer.echo(f"diff_lines={len(diff.splitlines())}")
    for artifact in result.artifacts:
        typer.echo(f"artifact={artifact}")


@app.command("extract-service")
def extract_service(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the main Git repository."
    ),
    task_id: UUID = typer.Option(  # noqa: B008
        ..., "--task-id", help="Existing task worktree UUID."
    ),
    service: str = typer.Option(  # noqa: B008
        ..., "--service", help="Candidate service name from boundary evidence."
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate bounded evidence without calling a model or writing files.",
    ),
) -> None:
    """Copy one selected candidate into a new service in an isolated worktree."""
    try:
        manager = GitWorktreeManager(path)
        workspace = manager.task_workspace(task_id)
        agent = ServiceExtractionAgent()
        task = Task(
            id=task_id,
            project_id=uuid4(),
            task_type=TaskType.SERVICE_EXTRACTION,
            title="Extract selected service",
            description=f"Copy-first extraction of {service}.",
            status=TaskStatus.READY,
            assigned_agent=agent.name,
        )
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.workspace_path),
            metadata={"service_name": service, "evidence_root": str(path.resolve())},
        )
        result = agent.dry_run(task, context) if dry_run else agent.execute(task, context)
    except (GitRepositoryError, ServiceExtractionError, ModelProviderError) as error:
        console.print(f"[red]Service extraction failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    if dry_run:
        typer.echo("Service extraction dry run")
        typer.echo("source_writes=no")
        typer.echo(f"selected_service={result.metadata['selected_service']}")
        typer.echo(f"target_service_directory={result.metadata['target_service_directory']}")
        typer.echo("selected_classes=" + ",".join(result.metadata.get("selected_classes", [])))
        typer.echo(f"context_bytes={result.metadata['context_bytes']}")
        return

    extraction_result = result.metadata["extraction_result"]
    typer.echo("Service extraction applied")
    typer.echo(f"selected_service={extraction_result['selected_candidate']['name']}")
    typer.echo("generated_files=" + ",".join(extraction_result["changed_files"]))
    for artifact in result.artifacts:
        typer.echo(f"artifact={artifact}")


@app.command("migrate-service")
def migrate_service(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the main Git repository."
    ),
    service: str = typer.Option(  # noqa: B008
        ..., "--service", help="Approved candidate service name."
    ),
    timeout: float = typer.Option(  # noqa: B008
        300.0,
        "--timeout",
        min=0.1,
        help="Maximum build verification duration in seconds.",
    ),
    max_debug_attempts: int = typer.Option(  # noqa: B008
        2,
        "--max-debug-attempts",
        min=0,
        max=3,
        help="Maximum bounded repair attempts after eligible verification failures.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Validate the run plan without a worktree, model call, or writes.",
    ),
) -> None:
    """Run one controlled extraction, build, and verification decision."""
    result = MigrationOrchestrator().run(
        path,
        service,
        timeout_seconds=timeout,
        dry_run=dry_run,
        max_debug_attempts=max_debug_attempts,
    )
    run = result.run
    typer.echo("Migration Run")
    typer.echo(f"Service          {run.selected_service}")
    typer.echo("Stages           5")
    typer.echo("Model capability coding")
    typer.echo(f"Worktree         {'planned' if dry_run else run.worktree_path}")
    if dry_run:
        typer.echo("Extraction       planned")
        typer.echo("Build verification planned")
        typer.echo("External call    no")
        if run.status is MigrationRunStatus.FAILED:
            typer.echo(f"Status           failed: {run.failure_reason}")
            raise typer.Exit(code=1)
        return

    for event in run.events:
        if event.event_type.value == "worktree_created":
            typer.echo("Preparing workspace")
        elif event.event_type.value == "extraction_started":
            typer.echo(f"Extracting {run.selected_service}")
        elif event.event_type.value == "extraction_completed":
            typer.echo(f"Generated {event.payload.get('generated_file_count', 0)} files")
        elif event.event_type.value == "build_started":
            typer.echo("Running build tests")
        elif event.event_type.value == "build_passed":
            typer.echo("Build verification passed")
        elif event.event_type.value == "build_failed":
            typer.echo("Build verification failed")
        elif event.event_type.value == "debug_started":
            typer.echo(f"Debug repair attempt {event.payload.get('attempt', '?')}")
        elif event.event_type.value == "debug_applied":
            typer.echo("Debug repair applied; re-verifying")
        elif event.event_type.value == "debug_failed":
            typer.echo("Debug repair failed")
        elif event.event_type.value == "reverify_started":
            typer.echo("Re-running build verification")
        elif event.event_type.value == "reverify_passed":
            typer.echo("Re-verification passed")
        elif event.event_type.value == "reverify_failed":
            typer.echo("Re-verification failed")
        elif event.event_type.value == "debug_attempts_exhausted":
            typer.echo("Debug attempts exhausted")
        elif event.event_type.value == "verification_passed":
            typer.echo("Verification passed")
        elif event.event_type.value == "verification_failed":
            typer.echo("Verification failed")
        elif event.event_type.value == "human_review_required":
            typer.echo("Human review required")
    typer.echo(f"Final status     {run.status.value}")
    if run.failure_reason:
        typer.echo(f"Failure          {run.failure_reason}")
    if run.status is not MigrationRunStatus.COMPLETED:
        raise typer.Exit(code=1)


@app.command("approve-service")
def approve_service(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the analyzed repository."
    ),
    service: str = typer.Option(  # noqa: B008
        ..., "--service", help="Candidate service to explicitly approve."
    ),
) -> None:
    """Record explicit human approval for one candidate service."""
    try:
        artifact = update_approval(path, service, approved=True)
    except MultiServiceMigrationError as error:
        typer.echo(_safe_error(error), err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"approved: {', '.join(artifact.approved) or '(none)'}")


@app.command("revoke-service")
def revoke_service(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the analyzed repository."
    ),
    service: str = typer.Option(  # noqa: B008
        ..., "--service", help="Candidate service whose approval is revoked."
    ),
) -> None:
    """Remove explicit human approval for one candidate service."""
    try:
        artifact = update_approval(path, service, approved=False)
    except MultiServiceMigrationError as error:
        typer.echo(_safe_error(error), err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"approved: {', '.join(artifact.approved) or '(none)'}")


@app.command("approvals")
def approvals(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the analyzed repository."
    ),
) -> None:
    """Show explicitly approved candidate services."""
    try:
        artifact = load_approvals(path)
    except MultiServiceMigrationError as error:
        typer.echo(_safe_error(error), err=True)
        raise typer.Exit(code=1) from error
    typer.echo("approved: " + (", ".join(artifact.approved) or "(none)"))


@app.command("migrate")
def migrate(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the main Git repository."
    ),
    service: list[str] | None = typer.Option(  # noqa: B008
        None, "--service", help="Explicitly approved candidate service; repeatable."
    ),
    all_approved: bool = typer.Option(
        False, "--all-approved", help="Migrate every explicitly approved service."
    ),
    service_workers: int = typer.Option(
        2, "--service-workers", min=1, max=3, help="Maximum concurrent service workflows."
    ),
    timeout: float = typer.Option(
        300.0, "--timeout", min=0.1, help="Maximum verification duration per service."
    ),
    max_debug_attempts: int = typer.Option(
        2, "--max-debug-attempts", min=0, max=3, help="Bounded repair attempts per service."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Plan without worktrees, model calls, or source writes."
    ),
) -> None:
    """Run bounded migration workflows for explicitly approved services."""
    try:
        result = MultiServiceMigrationOrchestrator().run(
            path,
            services=service,
            all_approved=all_approved,
            service_workers=service_workers,
            timeout_seconds=timeout,
            max_debug_attempts=max_debug_attempts,
            dry_run=dry_run,
        )
    except MultiServiceMigrationError as error:
        typer.echo(_safe_error(error), err=True)
        raise typer.Exit(code=1) from error
    run = result.run
    typer.echo(f"Run              {run.run_id}")
    typer.echo(f"Status           {run.status.value}")
    typer.echo(f"Services         {', '.join(run.selected_services)}")
    typer.echo(f"Roots            {', '.join(result.root_services) or '(none)'}")
    typer.echo(f"Service workers  {result.planned_service_workers}")
    typer.echo(f"DAG              {run.repository_root / '.migrationswarm' / 'multi-runs'}")
    typer.echo(f"Worktrees        {'no' if dry_run else 'managed per service'}")
    typer.echo(f"Model calls      {'no' if dry_run else 'through approved workflow'}")
    typer.echo(f"Source writes    {'no' if dry_run else 'task worktrees only'}")
    for state in run.services:
        dependency_text = ", ".join(state.dependencies) or "none"
        typer.echo(
            f"{state.service_name}: {state.status.value}; "
            f"depends_on={dependency_text}; repairs={state.repair_attempts}"
        )
    if result.rejected_services:
        typer.echo("Rejected         " + ", ".join(result.rejected_services))
    if not dry_run and run.status not in {
        MultiServiceMigrationStatus.COMPLETED,
        MultiServiceMigrationStatus.PARTIALLY_COMPLETED,
    }:
        raise typer.Exit(code=1)


@app.command("migration-status")
def migration_status(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the repository containing multi-run artifacts."
    ),
) -> None:
    """Show the latest multi-service migration run without sensitive details."""
    directory = path.expanduser().resolve() / ".migrationswarm" / "multi-runs"
    artifacts = sorted(
        directory.glob("*.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    if not artifacts:
        typer.echo("No multi-service migration runs found.")
        return
    try:
        run = MultiServiceMigrationRun.model_validate(
            json.loads(artifacts[-1].read_text(encoding="utf-8"))
        )
    except (OSError, ValueError) as error:
        typer.echo(f"Could not read migration status: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Run              {run.run_id}")
    typer.echo(f"Status           {run.status.value}")
    for state in run.services:
        typer.echo(
            f"{state.service_name}: {state.status.value}; "
            f"task={state.task_ids[0] if state.task_ids else 'none'}; "
            f"repairs={state.repair_attempts}"
        )


@app.command("demo")
def demo(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to examples/demo-commerce-monolith or another analyzed repository."
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Use deterministic local model responses."
    ),
    live: bool = typer.Option(
        False, "--live", help="Use configured provider credentials and model routing."
    ),
) -> None:
    """Run the bounded commerce migration demonstration."""
    if offline and live:
        typer.echo("Choose exactly one of --offline or --live.", err=True)
        raise typer.Exit(code=2)
    try:
        result = run_demo(path, live=live)
    except (DemoError, ModelProviderError, OSError, ValueError) as error:
        typer.echo(f"Demo failed: {error}", err=True)
        raise typer.Exit(code=1) from error
    _print_demo_result(result)
    if result.main_repository_modified:
        raise typer.Exit(code=1)


@app.command("benchmark")
def benchmark(
    path: Path | None = typer.Argument(  # noqa: B008
        None, help="Repository path for the legacy latest-report view."
    ),
    all_fixtures: bool = typer.Option(
        False, "--all", help="Evaluate every fixture under ./examples."
    ),
    fixture: str | None = typer.Option(
        None, "--fixture", help="Evaluate one fixture directory name under ./examples."
    ),
    baseline: bool = typer.Option(
        False, "--baseline", help="Run only the deterministic package baseline."
    ),
    compare_baseline: bool = typer.Option(
        False,
        "--compare-baseline",
        help="Include baseline results alongside the system evaluation.",
    ),
    live: bool = typer.Option(
        False, "--live", help="Use configured providers; offline mode is the default."
    ),
) -> None:
    """Run a reproducible benchmark or show a legacy demo benchmark."""
    if all_fixtures or fixture is not None or baseline or compare_baseline or live:
        if path is not None:
            typer.echo("Use --all/--fixture without a positional repository path.", err=True)
            raise typer.Exit(code=2)
        examples_root = Path.cwd() / "examples"
        if fixture is not None:
            selected = [examples_root / fixture]
            if not selected[0].is_dir():
                typer.echo(f"Unknown benchmark fixture: {fixture}", err=True)
                raise typer.Exit(code=2)
        elif all_fixtures:
            selected = discover_fixtures(examples_root)
        else:
            typer.echo("Choose --all or --fixture for evaluation mode.", err=True)
            raise typer.Exit(code=2)
        if not selected:
            typer.echo("No benchmark fixtures found.", err=True)
            raise typer.Exit(code=1)
        try:
            run, directory = run_benchmark(
                selected,
                Path.cwd() / "artifacts" / "benchmarks",
                baseline_only=baseline,
                compare_baseline=compare_baseline,
                live=live,
            )
        except (OSError, ValueError, RuntimeError) as error:
            typer.echo(f"Benchmark failed: {error}", err=True)
            raise typer.Exit(code=1) from error
        typer.echo(f"Run              {run.run_id}")
        typer.echo(f"Mode             {run.mode}")
        typer.echo(f"Fixtures         {run.aggregate.fixture_count}")
        typer.echo(f"Completed        {run.aggregate.completed_fixture_count}")
        typer.echo(f"Macro F1         {_format_metric(run.aggregate.macro_boundary_f1)}")
        typer.echo(f"Model calls      {run.aggregate.total_model_calls}")
        typer.echo(f"Output           {directory}")
        return
    if path is None:
        typer.echo("Provide a repository path or use --all/--fixture.", err=True)
        raise typer.Exit(code=2)
    directory = path.expanduser().resolve() / ".migrationswarm" / "reports"
    reports = sorted(
        directory.glob("*-benchmark.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    if not reports:
        typer.echo("No benchmark reports found.", err=True)
        raise typer.Exit(code=1)
    try:
        report = MigrationBenchmark.model_validate(
            json.loads(reports[-1].read_text(encoding="utf-8"))
        )
    except (OSError, ValueError) as error:
        typer.echo(f"Could not read benchmark: {error}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo(f"Run              {report.run_id}")
    typer.echo(
        f"Selected         {', '.join(report.migration.get('selected_services', [])) or '(none)'}"
    )
    typer.echo(
        f"Completed        {', '.join(report.migration.get('completed_services', [])) or '(none)'}"
    )
    typer.echo(f"Tasks            {report.execution.get('total_tasks', 0)}")
    typer.echo(f"Model calls      {report.ai.get('model_calls', 0)}")
    typer.echo(f"Model latency    {report.ai.get('latency_ms', 0.0):.1f} ms")
    typer.echo(
        "Main modified    " f"{'yes' if report.safety.get('main_repository_modified') else 'no'}"
    )


@app.command("perf")
def perf(
    offline: bool = typer.Option(
        False, "--offline", help="Use only deterministic local benchmark responses."
    ),
    fixture: str = typer.Option(
        "commerce", "--fixture", help="Benchmark fixture selector; currently only commerce."
    ),
    iterations: int = typer.Option(
        7, "--iterations", min=3, max=15, help="Bounded microbenchmark repetitions."
    ),
) -> None:
    """Measure bounded offline execution, concurrency, and analysis scalability."""
    del offline
    if fixture.casefold() not in {"commerce", "demo-commerce-monolith"}:
        typer.echo("Unknown performance fixture; use --fixture commerce.", err=True)
        raise typer.Exit(code=2)
    source = Path.cwd() / "examples" / "demo-commerce-monolith"
    try:
        report, json_path, markdown_path = run_performance_benchmark(
            source,
            output_root=Path.cwd() / "artifacts" / "performance",
            iterations=iterations,
        )
    except (OSError, ValueError, RuntimeError) as error:
        typer.echo(f"Performance benchmark failed: {_safe_error(error)}", err=True)
        raise typer.Exit(code=1) from error
    typer.echo("MigrationSwarm Performance")
    for measurement in [report.sequential, *report.parallel]:
        config = measurement.configuration
        typer.echo(f"{config.name:<18} {measurement.total_duration_ms:>10.1f} ms")
        typer.echo(
            f"  workers: services={config.service_workers} tasks={config.task_workers} "
            f"verification={config.verification_workers}"
        )
        speedup = 1.0 if config.name == "sequential" else report.speedups.get(config.name, 0.0)
        typer.echo(f"  speedup: {speedup:.4f}x")
        typer.echo(f"  max concurrent services: {measurement.max_concurrent_services}")
    typer.echo("Analysis scale")
    for item in report.synthetic_repositories:
        typer.echo(
            f"{item.class_count:>4} classes  scan={item.repository_scan_ms.median_ms:.2f} ms  "
            f"parse={item.dependency_parse_ms.median_ms:.2f} ms  "
            f"graph={item.architecture_graph_ms.median_ms:.2f} ms"
        )
    if report.bottlenecks:
        dominant = report.bottlenecks[0]
        typer.echo(f"Dominant bottleneck {dominant['category']} ({dominant['share'] * 100:.1f}%)")
    typer.echo(f"JSON               {json_path}")
    typer.echo(f"Markdown            {markdown_path}")


def _format_metric(value: float | None) -> str:
    """Render an optional evaluation metric without inventing a zero."""
    return "N/A" if value is None else f"{value:.3f}"


@app.command("verify-build")
def verify_build(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the main Git repository."
    ),
    task_id: UUID = typer.Option(  # noqa: B008
        ..., "--task-id", help="Existing task worktree UUID."
    ),
    timeout: float = typer.Option(  # noqa: B008
        300.0,
        "--timeout",
        min=0.1,
        help="Maximum verification command duration in seconds.",
    ),
) -> None:
    """Run the fixed test command in an existing isolated task worktree."""
    try:
        workspace = GitWorktreeManager(path).task_workspace(task_id)
        agent = BuildVerificationAgent(timeout_seconds=timeout)
        task = Task(
            id=task_id,
            project_id=uuid4(),
            task_type=TaskType.VERIFY,
            title="Verify isolated build",
            description="Run the project's allowlisted build test command.",
            status=TaskStatus.READY,
            assigned_agent=agent.name,
        )
        context = AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.workspace_path),
        )
        result = agent.execute(task, context)
    except (GitRepositoryError, BuildVerificationError) as error:
        console.print(f"[red]Build verification failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    verification = result.metadata["verification_result"]
    typer.echo(f"build_system={verification['build_system']}")
    typer.echo(f"status={verification['status']}")
    typer.echo(f"duration_ms={verification['duration_ms']}")
    test_summary = verification["test_summary"]
    for field in ("tests_run", "failures", "errors", "skipped"):
        value = test_summary[field]
        typer.echo(f"{field}={value if value is not None else 'n/a'}")
    for artifact in result.artifacts:
        typer.echo(f"artifact={artifact}")
    if not result.success:
        raise typer.Exit(code=1)


@app.command("decide-verification")
def decide_verification(
    path: Path = typer.Argument(  # noqa: B008
        ..., help="Path to the repository containing verification artifacts."
    ),
    task_id: UUID = typer.Option(  # noqa: B008
        ..., "--task-id", help="Task UUID whose latest verification is evaluated."
    ),
) -> None:
    """Evaluate the latest persisted build evidence without an external model."""
    try:
        repository = GitRepository.from_path(path)
        json_path = repository.root / VERIFICATION_RESULTS_DIR / f"{task_id}.json"
        if not json_path.is_file():
            raise VerificationCoordinatorError(f"No verification result found for task: {task_id}")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        artifact_metadata = payload.pop("artifacts", {})
        task_type = TaskType(payload.pop("task_type", TaskType.VERIFY.value))
        build_result = BuildVerificationResult.model_validate(payload)
        artifact_paths = [str(json_path)]
        if isinstance(artifact_metadata, dict):
            log_artifact = artifact_metadata.get("log")
            if isinstance(log_artifact, str):
                artifact_paths.append(str(repository.root / log_artifact))
        evidence = VerificationEvidence.from_build_result(build_result, artifact_paths)
        task = Task(
            id=task_id,
            project_id=uuid4(),
            task_type=task_type,
            title="Evaluate verification evidence",
            description="Evaluate persisted build verification evidence.",
            status=TaskStatus.VERIFYING,
        )
        decision = VerificationCoordinator(repository.root).decide(task, evidence)
    except (GitRepositoryError, OSError, ValueError, VerificationCoordinatorError) as error:
        console.print(f"[red]Verification decision failed:[/red] {escape(_safe_error(error))}")
        raise typer.Exit(code=1) from error

    typer.echo(f"decision={decision.decision.value}")
    for reason in decision.reasons:
        typer.echo(f"reason={reason}")
    for warning in decision.warnings:
        typer.echo(f"warning={warning}")
    if decision.artifact_path is not None:
        typer.echo(f"artifact={decision.artifact_path}")


@app.command("models")
def list_models() -> None:
    """Show configured logical models and safe routing eligibility."""
    settings = get_settings()
    model_registry = default_model_registry(settings)
    provider_registry = default_provider_registry(settings)
    table = Table(title="MigrationSwarm Models")
    table.add_column("Name", style="cyan")
    table.add_column("Provider", style="magenta")
    table.add_column("Capability", style="green")
    table.add_column("Credentials", style="yellow")
    table.add_column("Eligible", style="yellow")
    table.add_column("Health", style="yellow")
    for model in model_registry.list_models():
        provider_status = provider_registry.status(model.provider)
        capabilities = ", ".join(sorted(capability.value for capability in model.capabilities))
        health = "unknown" if provider_status.healthy is None else (
            "healthy" if provider_status.healthy else "unhealthy"
        )
        table.add_row(
            model.logical_name,
            model.provider,
            capabilities,
            "yes" if provider_status.configured else "no",
            "yes" if provider_status.eligible and model.enabled else "no",
            health,
        )
    console.print(table)
    for model in model_registry.list_models():
        provider_status = provider_registry.status(model.provider)
        console.print(
            f"{model.logical_name}: provider={model.provider} "
            f"configured={'yes' if provider_status.configured else 'no'} "
            f"eligible={'yes' if provider_status.eligible and model.enabled else 'no'}"
        )
    console.print(
        "Cross-provider fallback: "
        f"{'enabled' if settings.allow_cross_provider_fallback else 'disabled'}"
    )


@app.command("model-check")
def model_check(
    provider_name: str = typer.Argument(..., help="Provider name, such as groq or siliconflow."),
    capability: str | None = typer.Option(
        None,
        "--capability",
        help="Capability route to check, such as reasoning or coding.",
    ),
) -> None:
    """Perform one minimal connectivity check for a configured provider route."""
    settings = get_settings()
    model_registry = default_model_registry(settings)
    provider_registry = default_provider_registry(settings)
    try:
        provider = provider_registry.get(provider_name.lower())
    except UnknownProviderError as error:
        console.print(f"provider={provider_name.lower()} model=unknown status=error latency_ms=n/a")
        raise typer.Exit(code=1) from error

    selected_capability: ModelCapability | None = None
    if capability is not None:
        try:
            selected_capability = ModelCapability(capability.lower())
        except ValueError:
            console.print(
                f"provider={provider.name} model=unknown status=invalid_capability latency_ms=n/a"
            )
            raise typer.Exit(code=2) from None
    models = (
        list(model_registry.for_capability(selected_capability))
        if selected_capability is not None
        else list(model_registry.list_models())
    )
    models = [model for model in models if model.provider == provider.name and model.enabled]
    if not models:
        console.print(f"provider={provider.name} model=unknown status=error latency_ms=n/a")
        raise typer.Exit(code=1)
    model = min(models, key=lambda item: (item.priority, item.logical_name))
    if not provider.credentials_available:
        console.print(
            f"provider={provider.name} model={model.provider_model_id} "
            "status=credentials_unavailable latency_ms=n/a"
        )
        return

    try:
        response = provider.check(model.provider_model_id)
    except ModelProviderError as error:
        if error.category.value == "authentication":
            provider_registry.mark_invalid(provider.name)
        console.print(
            f"provider={provider.name} model={model.provider_model_id} "
            f"status={error.category.value} latency_ms=n/a"
        )
        raise typer.Exit(code=1) from error
    provider_registry.mark_healthy(provider.name)
    console.print(
        f"provider={provider.name} model={model.provider_model_id} "
        f"status=ok latency_ms={response.latency_ms:.1f}"
    )


def _print_demo_result(result: DemoResult) -> None:
    typer.echo(f"Run              {result.run_id}")
    typer.echo(f"Status           {result.status}")
    typer.echo(f"Mode             {'offline' if result.offline else 'live'}")
    typer.echo(f"Model calls      {result.model_calls}")
    typer.echo(f"Metrics          {result.metrics_path}")
    typer.echo(f"Benchmark        {result.benchmark_path}")
    if result.live_report_path is not None:
        typer.echo(f"Live report      {result.live_report_path}")
    typer.echo("Main modified    " f"{'yes' if result.main_repository_modified else 'no'}")
    for directory in result.generated_service_directories:
        typer.echo(f"Generated        {directory}")


def main() -> None:
    """Run the MigrationSwarm command-line interface."""
    app()


if __name__ == "__main__":
    main()
