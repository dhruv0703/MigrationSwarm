"""Unit tests for grounded service-boundary proposals."""

import json
from pathlib import Path
from typing import Any, ClassVar, cast
from uuid import UUID

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.architecture_analysis import (
    ARCHITECTURE_REPORT_ARTIFACT,
    ArchitectureComponent,
    ArchitectureReport,
    ClassArchitectureMetric,
    PackageArchitectureMetric,
)
from migrationswarm.agents.dependency_analysis import (
    JAVA_DEPENDENCY_ARTIFACT,
    JavaClass,
    JavaClassRole,
    JavaDependency,
    JavaDependencyGraph,
    JavaDependencyRelationship,
)
from migrationswarm.agents.service_boundary import (
    SERVICE_BOUNDARIES_JSON_ARTIFACT,
    SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT,
    BoundaryResponseError,
    ServiceBoundaryAgent,
    _allowed_candidate_dependencies,
    _deterministic_candidates,
    _required_classes_by_candidate,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentResult
from migrationswarm.core.models import (
    ModelCapability,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ProviderRegistry,
)
from migrationswarm.core.models.models import ModelDefinition
from migrationswarm.core.models.registry import ModelRegistry
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

PROJECT_ID = UUID(int=5000)
runner = CliRunner()


def write_file(root: Path, relative_path: str, content: str) -> Path:
    """Write a UTF-8 test file."""
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def architecture_report() -> ArchitectureReport:
    """Create compact structured evidence for a small Spring-style graph."""
    classes = [
        ClassArchitectureMetric(
            fully_qualified_name="example.GreetingController",
            name="GreetingController",
            package="example",
            role=JavaClassRole.CONTROLLER,
            in_degree=0,
            out_degree=1,
            fan_in=0,
            fan_out=1,
        ),
        ClassArchitectureMetric(
            fully_qualified_name="example.GreetingService",
            name="GreetingService",
            package="example",
            role=JavaClassRole.SERVICE,
            in_degree=1,
            out_degree=1,
            fan_in=1,
            fan_out=1,
        ),
        ClassArchitectureMetric(
            fully_qualified_name="example.GreetingRepository",
            name="GreetingRepository",
            package="example",
            role=JavaClassRole.REPOSITORY,
            in_degree=1,
            out_degree=0,
            fan_in=1,
            fan_out=0,
        ),
        ClassArchitectureMetric(
            fully_qualified_name="example.SharedMapper",
            name="SharedMapper",
            package="example",
            role=JavaClassRole.COMPONENT,
            in_degree=0,
            out_degree=0,
            fan_in=0,
            fan_out=0,
        ),
    ]
    return ArchitectureReport(
        repository_root="C:/fixture",
        total_classes=len(classes),
        total_dependencies=2,
        connected_component_count=2,
        strongly_connected_component_count=4,
        cycle_count=0,
        class_metrics=classes,
        package_metrics=[
            PackageArchitectureMetric(
                package="example",
                class_count=4,
                in_degree=0,
                out_degree=0,
                fan_in=0,
                fan_out=0,
                internal_edge_count=2,
                external_edge_count=0,
            )
        ],
        components=[
            ArchitectureComponent(
                component_id="component-001",
                classes=[
                    "example.GreetingController",
                    "example.GreetingRepository",
                    "example.GreetingService",
                ],
                packages=["example"],
                internal_edge_count=2,
                external_edge_count=0,
                controller_count=1,
                service_count=1,
                repository_count=1,
            )
        ],
    )


def dependency_graph() -> JavaDependencyGraph:
    """Create the matching local dependency graph fixture."""
    classes = [
        JavaClass(
            name=name.rsplit(".", 1)[-1],
            fully_qualified_name=name,
            package="example",
            file_path=f"src/main/java/{name.replace('.', '/')}.java",
            role=role,
        )
        for name, role in (
            ("example.GreetingController", JavaClassRole.CONTROLLER),
            ("example.GreetingService", JavaClassRole.SERVICE),
            ("example.GreetingRepository", JavaClassRole.REPOSITORY),
            ("example.SharedMapper", JavaClassRole.COMPONENT),
        )
    ]
    dependencies = [
        JavaDependency(
            source="example.GreetingController",
            target="example.GreetingService",
            relationship=JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY,
            evidence="constructor",
        ),
        JavaDependency(
            source="example.GreetingService",
            target="example.GreetingRepository",
            relationship=JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY,
            evidence="constructor",
        ),
    ]
    return JavaDependencyGraph(
        repository_root="C:/fixture",
        total_java_classes=len(classes),
        total_dependency_edges=len(dependencies),
        classes=classes,
        dependencies=dependencies,
    )


def valid_output(**overrides: object) -> dict[str, object]:
    """Return valid model output with optional test-specific overrides."""
    data: dict[str, object] = {
        "candidate_services": [
            {
                "name": "Greeting",
                "description": "Greeting web and persistence flow.",
                "classes": [
                    "example.GreetingController",
                    "example.GreetingService",
                    "example.GreetingRepository",
                ],
                "packages": ["example"],
                "controllers": ["example.GreetingController"],
                "services": ["example.GreetingService"],
                "repositories": ["example.GreetingRepository"],
                "confidence": 0.86,
                "reasoning": "The controller, service, and repository form a cohesive chain.",
                "dependencies_on_other_candidates": [],
                "risks": [],
            }
        ],
        "shared_components": [],
        "unresolved_classes": ["example.SharedMapper"],
        "evidence_accounting": [
            {
                "candidate": "Greeting",
                "disposition": "INCLUDED",
                "classes": [
                    "example.GreetingController",
                    "example.GreetingService",
                    "example.GreetingRepository",
                ],
            }
        ],
        "overall_reasoning": (
            "The evidence supports one cohesive candidate and one unresolved component."
        ),
        "warnings": [],
    }
    data.update(overrides)
    return data


def candidate_template() -> dict[str, object]:
    """Return a typed copy of the valid candidate fixture."""
    candidates = cast(list[dict[str, object]], valid_output()["candidate_services"])
    return candidates[0]


def candidate_list() -> list[dict[str, object]]:
    """Return the typed candidate list from the valid fixture."""
    return cast(list[dict[str, object]], valid_output()["candidate_services"])


class FakeRouter:
    """Test router that records requests and returns scripted responses."""

    def __init__(self, contents: list[str]) -> None:
        self.contents = list(contents)
        self.requests: list[ModelRequest] = []

    def generate(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            provider="fake",
            model="fake-architecture-model",
            content=self.contents.pop(0),
            latency_ms=2.0,
        )


class NamedFakeProvider(ModelProvider):
    """Fake provider for proving SiliconFlow availability is not required."""

    name: ClassVar[str] = "groq"

    def __init__(self, *, credentials: bool) -> None:
        self.credentials = credentials
        self.calls = 0

    @property
    def credentials_available(self) -> bool:
        return self.credentials

    def generate(self, request: ModelRequest, model: str) -> ModelResponse:
        self.calls += 1
        return ModelResponse(
            provider=self.name,
            model=model,
            content=json.dumps(valid_output()),
            latency_ms=1.0,
        )


class SiliconFlowFakeProvider(NamedFakeProvider):
    """Unavailable SiliconFlow fake for routing tests."""

    name = "siliconflow"


def make_task() -> Task:
    """Create a ready service-boundary task."""
    return Task(
        project_id=PROJECT_ID,
        task_type=TaskType.SERVICE_BOUNDARY_ANALYSIS,
        title="Boundary proposal",
        description="Propose boundaries",
        status=TaskStatus.READY,
        assigned_agent=ServiceBoundaryAgent.name,
    )


def write_evidence(root: Path, *, include_dependency: bool = True) -> None:
    """Write architecture and optional dependency artifacts."""
    write_file(
        root,
        ARCHITECTURE_REPORT_ARTIFACT,
        json.dumps(architecture_report().model_dump(mode="json")),
    )
    if include_dependency:
        write_file(
            root,
            JAVA_DEPENDENCY_ARTIFACT,
            json.dumps(dependency_graph().model_dump(mode="json")),
        )


def run_agent(root: Path, router: FakeRouter) -> AgentResult:
    """Execute the production agent with a fake router."""
    task = make_task()
    context = AgentContext(project_id=PROJECT_ID, task=task, workspace_path=str(root))
    return ServiceBoundaryAgent(router).execute(task, context)


def test_architecture_evidence_loading_and_compact_input(tmp_path: Path) -> None:
    """Existing artifacts become compact structured model input without source code."""
    write_evidence(tmp_path)
    router = FakeRouter([json.dumps(valid_output())])
    result = run_agent(tmp_path, router)

    request = router.requests[0]
    assert result.success
    assert request.capability is ModelCapability.ARCHITECTURE
    assert request.max_tokens == 4000
    assert len(request.messages) == 2
    assert "example.GreetingService" in request.messages[1].content
    assert "CONSTRUCTOR_DEPENDENCY" in request.messages[1].content
    assert "public class" not in request.messages[1].content
    assert len(request.messages[1].content.encode("utf-8")) < 5000


def test_missing_architecture_artifact_runs_deterministic_fallback(tmp_path: Path) -> None:
    """Missing artifacts are prepared by existing dependency and architecture agents."""
    for class_name in ("GreetingController", "GreetingService", "GreetingRepository"):
        write_file(
            tmp_path,
            f"src/main/java/example/{class_name}.java",
            f"package example; public class {class_name} {{}}",
        )
    router = FakeRouter([json.dumps(valid_output(unresolved_classes=[]))])

    result = run_agent(tmp_path, router)

    assert result.success
    assert (tmp_path / ARCHITECTURE_REPORT_ARTIFACT).is_file()
    assert (tmp_path / JAVA_DEPENDENCY_ARTIFACT).is_file()


def test_valid_response_grounding_and_agent_result(tmp_path: Path) -> None:
    """Valid structured output is grounded and returned through AgentResult."""
    write_evidence(tmp_path)
    result = run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))
    report = result.metadata["service_boundary_report"]

    assert result.agent_name == ServiceBoundaryAgent.name
    assert result.artifacts == [
        SERVICE_BOUNDARIES_JSON_ARTIFACT,
        SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT,
    ]
    assert report["model_provider"] == "fake"
    assert report["model_name"] == "fake-architecture-model"
    assert report["candidate_services"][0]["confidence"] == 0.86


def test_malformed_json_gets_one_repair_attempt(tmp_path: Path) -> None:
    """Malformed first output is repaired exactly once."""
    write_evidence(tmp_path)
    router = FakeRouter(["not json", json.dumps(valid_output())])

    result = run_agent(tmp_path, router)

    assert result.success
    assert len(router.requests) == 2
    assert "Repair the following invalid response" in router.requests[1].messages[-1].content


@pytest.mark.parametrize(
    "bad_output",
    [
        {
            "candidate_services": [
                {**candidate_template(), "classes": ["example.Missing"]}
            ],
            "shared_components": [],
            "unresolved_classes": [],
            "overall_reasoning": "x",
            "warnings": [],
        },
        {
            "candidate_services": [
                {**candidate_template(), "classes": ["example.GreetingController"]},
                {
                    **candidate_template(),
                    "name": "Other",
                    "classes": ["example.GreetingController"],
                },
            ],
            "shared_components": [],
            "unresolved_classes": [],
            "overall_reasoning": "x",
            "warnings": [],
        },
        {
            "candidate_services": [{**candidate_template(), "confidence": 1.2}],
            "shared_components": [],
            "unresolved_classes": [],
            "overall_reasoning": "x",
            "warnings": [],
        },
        {
            "candidate_services": [
                {**candidate_template(), "dependencies_on_other_candidates": ["Missing"]}
            ],
            "shared_components": [],
            "unresolved_classes": [],
            "overall_reasoning": "x",
            "warnings": [],
        },
        {
            "candidate_services": [{**candidate_template(), "classes": []}],
            "shared_components": [],
            "unresolved_classes": [],
            "overall_reasoning": "x",
            "warnings": [],
        },
        {
            "candidate_services": candidate_list(),
            "shared_components": [],
            "unresolved_classes": ["example.Missing"],
            "overall_reasoning": "x",
            "warnings": [],
        },
    ],
)
def test_invalid_grounding_fails_after_one_repair(
    tmp_path: Path, bad_output: dict[str, object]
) -> None:
    """Hallucinations, duplicates, invalid confidence, and empty references fail clearly."""
    write_evidence(tmp_path)
    content = json.dumps(bad_output)
    router = FakeRouter([content, content])

    with pytest.raises(BoundaryResponseError, match="one repair attempt"):
        run_agent(tmp_path, router)
    assert len(router.requests) == 2


def test_shared_class_assignment_is_allowed_when_marked(tmp_path: Path) -> None:
    """A duplicated class is valid only when listed as shared."""
    write_evidence(tmp_path)
    candidate = candidate_template()
    output = valid_output(
        candidate_services=[
            candidate,
            {
                **candidate,
                "name": "Shared",
                "classes": ["example.SharedMapper"],
                "controllers": [],
                "services": [],
                "repositories": [],
            },
        ],
        shared_components=[
            {"class_name": "example.SharedMapper", "reason": "Used across candidates."}
        ],
        unresolved_classes=[],
    )
    result = run_agent(tmp_path, FakeRouter([json.dumps(output)]))

    assert result.success


def test_unsupported_candidate_dependency_is_rejected(tmp_path: Path) -> None:
    """Declared cross-candidate edges must be present in Java evidence."""
    write_evidence(tmp_path)
    candidate = candidate_template()
    output = valid_output(
        candidate_services=[
            {
                **candidate,
                "dependencies_on_other_candidates": ["Shared"],
            },
            {
                **candidate,
                "name": "Shared",
                "classes": ["example.SharedMapper"],
                "controllers": [],
                "services": [],
                "repositories": [],
            },
        ],
        shared_components=[],
        unresolved_classes=[],
    )
    content = json.dumps(output)
    router = FakeRouter([content, content])

    with pytest.raises(BoundaryResponseError, match="one repair attempt"):
        run_agent(tmp_path, router)
    repair_content = router.requests[1].messages[-1].content
    assert "deterministic allowlist" in repair_content
    assert "[]" in repair_content
    assert "Candidate dependency is not supported by Java dependency evidence" in repair_content


def test_artifacts_include_json_and_concise_markdown(tmp_path: Path) -> None:
    """JSON and human-readable Markdown artifacts are generated."""
    write_evidence(tmp_path)
    run_agent(tmp_path, FakeRouter([json.dumps(valid_output())]))

    json_path = tmp_path / SERVICE_BOUNDARIES_JSON_ARTIFACT
    markdown_path = tmp_path / SERVICE_BOUNDARIES_MARKDOWN_ARTIFACT
    assert json.loads(json_path.read_text(encoding="utf-8"))["candidate_services"]
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "# Service Boundary Proposals" in markdown
    assert "Greeting" in markdown
    assert "example.GreetingService" in markdown
    assert "example.SharedMapper" in markdown


def test_dry_run_prepares_evidence_without_model_call(tmp_path: Path) -> None:
    """Dry-run evidence preparation does not invoke the router."""
    write_evidence(tmp_path)
    router = FakeRouter([])
    agent = ServiceBoundaryAgent(router)

    evidence = agent.prepare_evidence(tmp_path)

    assert evidence["classes"]
    assert agent.input_size(evidence) > 0
    assert router.requests == []


def test_cli_dry_run(tmp_path: Path) -> None:
    """The CLI dry-run reports capability and estimated input size."""
    write_evidence(tmp_path)

    result = runner.invoke(app, ["propose-boundaries", str(tmp_path), "--dry-run"])

    assert result.exit_code == 0
    assert "Service Boundary Proposal (dry run)" in result.stdout
    assert "Capability: architecture" in result.stdout
    assert "Structured input bytes:" in result.stdout
    assert "External model call: no" in result.stdout


def test_cli_successful_mocked_proposal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI renders a successful fake proposal without contacting Groq."""
    write_evidence(tmp_path)
    router = FakeRouter([json.dumps(valid_output())])
    from migrationswarm.cli import main as cli_main

    monkeypatch.setattr(cli_main, "ServiceBoundaryAgent", lambda: ServiceBoundaryAgent(router))
    result = runner.invoke(app, ["propose-boundaries", str(tmp_path)])

    assert result.exit_code == 0
    assert "Service Boundary Proposal" in result.stdout
    assert "Candidates" in result.stdout
    assert "Candidate: Greeting" in result.stdout
    assert len(router.requests) == 1


def test_siliconflow_unavailable_does_not_block_groq_routing() -> None:
    """The architecture-capable Groq model works with no SiliconFlow credentials."""
    groq = NamedFakeProvider(credentials=True)
    siliconflow = SiliconFlowFakeProvider(credentials=False)
    router = ModelRouter(
        ModelRegistry(
            [
                ModelDefinition(
                    logical_name="groq-reasoning",
                    provider="groq",
                    provider_model_id="openai/gpt-oss-120b",
                    capabilities=frozenset({ModelCapability.ARCHITECTURE}),
                    priority=1,
                ),
                ModelDefinition(
                    logical_name="siliconflow-architecture",
                    provider="siliconflow",
                    provider_model_id="unused",
                    capabilities=frozenset({ModelCapability.ARCHITECTURE}),
                    priority=2,
                ),
            ]
        ),
        ProviderRegistry([groq, siliconflow]),
        max_attempts_per_model=1,
    )
    agent = ServiceBoundaryAgent(router)
    request = agent._request({"classes": [], "dependencies": []})

    result = router.generate(request)

    assert result.provider == "groq"
    assert groq.calls == 1
    assert siliconflow.calls == 0


def _commerce_domains() -> dict[str, list[str]]:
    """Return sanitized domain class names used by coverage regression tests."""
    singular = {
        "Inventory": "Inventory",
        "Orders": "Order",
        "Notifications": "Notification",
        "Customers": "Customer",
    }
    return {
        domain: [
            f"com.example.commerce.{domain.casefold()}.{singular[domain]}Controller",
            f"com.example.commerce.{domain.casefold()}.{singular[domain]}Service",
            f"com.example.commerce.{domain.casefold()}.{singular[domain]}Repository",
            f"com.example.commerce.{domain.casefold()}.{singular[domain]}Dto",
        ]
        for domain in ("Inventory", "Orders", "Notifications", "Customers")
    }


def _commerce_evidence() -> tuple[ArchitectureReport, JavaDependencyGraph]:
    """Build deterministic, source-free evidence for four strong domains."""
    domains = _commerce_domains()
    classes: list[ClassArchitectureMetric] = []
    java_classes: list[JavaClass] = []
    components: list[ArchitectureComponent] = []
    dependencies: list[JavaDependency] = []
    for domain, names in domains.items():
        package = names[0].rsplit(".", 1)[0]
        for name in names:
            simple = name.rsplit(".", 1)[-1]
            role = (
                JavaClassRole.CONTROLLER
                if simple.endswith("Controller")
                else JavaClassRole.SERVICE
                if simple.endswith("Service")
                else JavaClassRole.REPOSITORY
                if simple.endswith("Repository")
                else JavaClassRole.OTHER
            )
            classes.append(
                ClassArchitectureMetric(
                    fully_qualified_name=name,
                    name=simple,
                    package=package,
                    role=role,
                    in_degree=1,
                    out_degree=1,
                    fan_in=1,
                    fan_out=1,
                )
            )
            java_classes.append(
                JavaClass(
                    name=simple,
                    fully_qualified_name=name,
                    package=package,
                    file_path=f"src/main/java/{name.replace('.', '/')}.java",
                    role=role,
                )
            )
        components.append(
            ArchitectureComponent(
                component_id=f"component-{domain.casefold()}",
                classes=names,
                packages=[package],
                internal_edge_count=3,
                external_edge_count=1 if domain == "Orders" else 0,
                controller_count=1,
                service_count=1,
                repository_count=1,
            )
        )
        dependencies.extend(
            [
                JavaDependency(
                    source=names[0],
                    target=names[1],
                    relationship=JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY,
                    evidence="constructor",
                ),
                JavaDependency(
                    source=names[1],
                    target=names[2],
                    relationship=JavaDependencyRelationship.CONSTRUCTOR_DEPENDENCY,
                    evidence="constructor",
                ),
            ]
        )
    dependencies.append(
        JavaDependency(
            source=domains["Orders"][1],
            target=domains["Inventory"][1],
            relationship=JavaDependencyRelationship.FIELD_DEPENDENCY,
            evidence="gateway",
        )
    )
    return (
        ArchitectureReport(
            repository_root="C:/sanitized-commerce",
            total_classes=len(classes),
            total_dependencies=len(dependencies),
            connected_component_count=4,
            strongly_connected_component_count=len(classes),
            cycle_count=0,
            class_metrics=classes,
            components=components,
        ),
        JavaDependencyGraph(
            repository_root="C:/sanitized-commerce",
            total_java_classes=len(java_classes),
            total_dependency_edges=len(dependencies),
            classes=java_classes,
            dependencies=dependencies,
        ),
    )


def write_commerce_evidence(root: Path) -> None:
    """Write only structured sanitized evidence for coverage tests."""
    architecture, dependencies = _commerce_evidence()
    write_file(root, ARCHITECTURE_REPORT_ARTIFACT, architecture.model_dump_json())
    write_file(root, JAVA_DEPENDENCY_ARTIFACT, dependencies.model_dump_json())


def commerce_candidate(name: str, classes: list[str]) -> dict[str, Any]:
    """Create one structured candidate from sanitized class names."""
    return {
        "name": name,
        "description": f"The {name} domain.",
        "classes": classes,
        "packages": sorted({item.rsplit(".", 1)[0] for item in classes}),
        "controllers": [item for item in classes if item.endswith("Controller")],
        "services": [item for item in classes if item.endswith("Service")],
        "repositories": [item for item in classes if item.endswith("Repository")],
        "confidence": 0.9,
        "reasoning": f"The {name} classes form a cohesive domain.",
        "dependencies_on_other_candidates": ["Inventory"] if name == "Orders" else [],
        "risks": [],
    }


def commerce_output(
    *,
    accounting: list[dict[str, Any]] | None = None,
    candidate_services: list[dict[str, Any]] | None = None,
    shared_components: list[dict[str, str]] | None = None,
    unresolved_classes: list[str] | None = None,
) -> dict[str, Any]:
    """Create a complete or intentionally incomplete sanitized boundary response."""
    domains = _commerce_domains()
    candidates = candidate_services or [
        commerce_candidate(name, classes) for name, classes in domains.items()
    ]
    return {
        "candidate_services": candidates,
        "shared_components": shared_components or [],
        "unresolved_classes": unresolved_classes or [],
        "evidence_accounting": accounting
        if accounting is not None
        else [
            {"candidate": name, "disposition": "INCLUDED", "classes": classes}
            for name, classes in domains.items()
        ],
        "overall_reasoning": "All deterministic domains are explicitly accounted for.",
        "warnings": [],
    }


def run_commerce_output(root: Path, outputs: list[dict[str, Any]]) -> AgentResult:
    """Execute coverage validation with scripted, non-network responses."""
    write_commerce_evidence(root)
    return run_agent(root, FakeRouter([json.dumps(item) for item in outputs]))


def test_sanitized_live_response_rejects_omitted_domains(tmp_path: Path) -> None:
    """The sanitized former live response cannot silently omit three domains."""
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures/service-boundary/commerce-live-incomplete.json")
        .read_text(encoding="utf-8")
    )
    write_commerce_evidence(tmp_path)
    with pytest.raises(BoundaryResponseError, match="INCOMPLETE_BOUNDARY_COVERAGE"):
        run_agent(tmp_path, FakeRouter([json.dumps(fixture), json.dumps(fixture)]))


def _implementation_component(package: str, stem: str) -> dict[str, Any]:
    """Build one strong component for deterministic package-accounting tests."""
    return {
        "packages": [package],
        "classes": [
            f"{package}.{stem}Controller",
            f"{package}.{stem}Service",
            f"{package}.{stem}Repository",
        ],
        "controller_count": 1,
        "service_count": 1,
        "repository_count": 1,
    }


def _implementation_evidence(*components: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal evidence shape consumed by deterministic candidates."""
    classes = [
        {
            "fully_qualified_name": class_name,
            "role": role,
        }
        for component in components
        for class_name, role in (
            (component["classes"][0], "CONTROLLER"),
            (component["classes"][1], "SERVICE"),
            (component["classes"][2], "REPOSITORY"),
        )
    ]
    return {"classes": classes, "candidate_components": list(components), "dependencies": []}


def test_nested_internal_package_merges_into_parent_module() -> None:
    evidence = _implementation_evidence(
        {
            "packages": ["com.example.orders"],
            "classes": [
                "com.example.orders.OrderEvent",
                "com.example.orders.OrderState",
                "com.example.orders.OrderPolicy",
            ],
            "controller_count": 0,
            "service_count": 0,
            "repository_count": 0,
        },
        _implementation_component("com.example.orders.internal", "Order")
    )

    candidates = _deterministic_candidates(evidence)

    assert [item["name"] for item in candidates] == ["Orders"]
    assert candidates[0]["merged_packages"] == ["com.example.orders.internal"]
    assert set(evidence["candidate_components"][1]["classes"]) <= set(candidates[0]["classes"])
    required = _required_classes_by_candidate(evidence)
    assert required[0]["merged_classes"] == sorted(
        evidence["candidate_components"][1]["classes"]
    )
    assert set(required[0]["merged_classes"]) <= set(required[0]["required_classes"])


def test_multiple_modules_merge_their_internal_packages_independently() -> None:
    evidence = _implementation_evidence(
        _implementation_component("com.example.orders.internal", "Order"),
        _implementation_component("com.example.billing.internal", "Billing"),
    )

    candidates = _deterministic_candidates(evidence)

    assert [item["name"] for item in candidates] == ["Billing", "Orders"]
    assert all(item["merged_packages"] for item in candidates)


def test_merged_internal_classes_contribute_to_allowed_candidate_edges() -> None:
    orders = _implementation_component("com.example.orders.internal", "Order")
    billing = _implementation_component("com.example.billing.internal", "Billing")
    evidence = _implementation_evidence(orders, billing)
    evidence["dependencies"] = [
        {
            "source": orders["classes"][1],
            "target": billing["classes"][1],
            "relationship": "FIELD_DEPENDENCY",
        }
    ]

    assert _allowed_candidate_dependencies(evidence) == [
        {"source": "Orders", "target": "Billing"}
    ]


def test_supported_candidate_dependency_is_accepted_and_explicitly_allowed(
    tmp_path: Path,
) -> None:
    write_commerce_evidence(tmp_path)
    output = commerce_output()
    router = FakeRouter([json.dumps(output)])
    result = run_agent(tmp_path, router)

    assert result.success
    request = router.requests[0].messages[1].content
    assert '"allowed_candidate_dependencies"' in request
    assert '"source":"Orders"' in request
    assert '"target":"Inventory"' in request


def test_empty_allowed_dependency_set_is_explicit_in_prompt(tmp_path: Path) -> None:
    write_evidence(tmp_path)
    router = FakeRouter([json.dumps(valid_output())])
    run_agent(tmp_path, router)

    request = router.requests[0].messages[1].content
    assert '"allowed_candidate_dependencies":[]' in request


def test_top_level_internal_package_remains_a_domain_candidate() -> None:
    evidence = _implementation_evidence(
        _implementation_component("com.example.internal", "Internal")
    )

    candidates = _deterministic_candidates(evidence)

    assert [item["name"] for item in candidates] == ["Internal"]
    assert candidates[0]["merged_packages"] == []


def test_all_deterministic_domains_included_are_accepted(tmp_path: Path) -> None:
    """Every strong source domain can be included with complete class coverage."""
    result = run_commerce_output(tmp_path, [commerce_output()])
    report = result.metadata["service_boundary_report"]
    assert {item["candidate"] for item in report["evidence_accounting"]} == {
        "Customers", "Inventory", "Notifications", "Orders"
    }


def test_grounded_merge_requires_and_accepts_dependency_evidence(tmp_path: Path) -> None:
    """A merge is accepted only when the target owns every source class and evidence connects it."""
    domains = _commerce_domains()
    merged = commerce_candidate("Orders", domains["Orders"] + domains["Inventory"])
    merged["dependencies_on_other_candidates"] = []
    accounting: list[dict[str, Any]] = [
        {
            "candidate": "Inventory",
            "disposition": "MERGED",
            "merged_into": "Orders",
            "reason": "Inventory is coupled to Orders by gateway evidence.",
        },
        {"candidate": "Orders", "disposition": "INCLUDED", "classes": domains["Orders"]},
        *[
            {"candidate": name, "disposition": "INCLUDED", "classes": domains[name]}
            for name in ("Notifications", "Customers")
        ],
    ]
    result = run_commerce_output(
        tmp_path,
        [
            commerce_output(
                accounting=accounting,
                candidate_services=[
                    merged,
                    commerce_candidate("Notifications", domains["Notifications"]),
                    commerce_candidate("Customers", domains["Customers"]),
                ],
            )
        ],
    )
    assert result.success


def test_merge_without_class_coverage_is_rejected(tmp_path: Path) -> None:
    """A merge cannot discard source classes."""
    domains = _commerce_domains()
    accounting: list[dict[str, Any]] = [
        {
            "candidate": "Inventory",
            "disposition": "MERGED",
            "merged_into": "Orders",
            "reason": "Inventory is coupled to Orders.",
        },
        *[
            {"candidate": name, "disposition": "INCLUDED", "classes": classes}
            for name, classes in domains.items()
            if name != "Inventory"
        ],
    ]
    output = commerce_output(accounting=accounting)
    output["candidate_services"] = [
        commerce_candidate("Orders", domains["Orders"]),
        commerce_candidate("Notifications", domains["Notifications"]),
        commerce_candidate("Customers", domains["Customers"]),
    ]
    with pytest.raises(BoundaryResponseError, match="one repair attempt"):
        run_commerce_output(tmp_path, [output, output])


def test_explicit_shared_domain_is_accepted(tmp_path: Path) -> None:
    """A strong candidate may be shared when every class and reason are explicit."""
    domains = _commerce_domains()
    shared = [
        {"class_name": item, "reason": "Inventory is shared by multiple workflows."}
        for item in domains["Inventory"]
    ]
    accounting = [
        {
            "candidate": "Inventory",
            "disposition": "SHARED",
            "classes": domains["Inventory"],
            "reason": "Inventory is shared by multiple workflows.",
        },
        *[
            {"candidate": name, "disposition": "INCLUDED", "classes": classes}
            for name, classes in domains.items()
            if name != "Inventory"
        ],
    ]
    output = commerce_output(accounting=accounting, shared_components=shared)
    output["candidate_services"] = [
        commerce_candidate(name, classes)
        for name, classes in domains.items()
        if name != "Inventory"
    ]
    output["candidate_services"][0]["dependencies_on_other_candidates"] = []
    assert run_commerce_output(tmp_path, [output]).success


def test_grounded_exclusion_and_silent_exclusion_behavior(tmp_path: Path) -> None:
    """Grounded exclusions pass; omitting the same domain fails closed."""
    domains = _commerce_domains()
    accounting = [
        {
            "candidate": "Inventory",
            "disposition": "EXCLUDED_WITH_GROUNDED_REASON",
            "classes": domains["Inventory"],
            "reason": "Inventory is excluded because its evidence is incomplete.",
        },
        *[
            {"candidate": name, "disposition": "INCLUDED", "classes": classes}
            for name, classes in domains.items()
            if name != "Inventory"
        ],
    ]
    output = commerce_output(accounting=accounting, unresolved_classes=domains["Inventory"])
    output["candidate_services"] = [
        commerce_candidate(name, classes)
        for name, classes in domains.items()
        if name != "Inventory"
    ]
    output["candidate_services"][0]["dependencies_on_other_candidates"] = []
    assert run_commerce_output(tmp_path, [output]).success
    silent = commerce_output(
        accounting=[item for item in accounting if item["candidate"] != "Inventory"]
    )
    with pytest.raises(BoundaryResponseError, match="INCOMPLETE_BOUNDARY_COVERAGE"):
        run_commerce_output(tmp_path, [silent, silent])


def test_missing_class_coverage_is_rejected(tmp_path: Path) -> None:
    """A candidate that omits an architecture-relevant DTO is incomplete."""
    domains = _commerce_domains()
    missing_class = domains["Inventory"][-1]
    candidates = [
        commerce_candidate(name, classes[:-1] if name == "Inventory" else classes)
        for name, classes in domains.items()
    ]
    output = commerce_output(candidate_services=candidates)
    write_commerce_evidence(tmp_path)
    router = FakeRouter([json.dumps(output), json.dumps(output)])
    with pytest.raises(BoundaryResponseError, match="INCOMPLETE_BOUNDARY_COVERAGE"):
        run_agent(tmp_path, router)
    assert missing_class in str(router.requests[1].messages[-1].content)
    assert "required_classes_by_candidate" in router.requests[1].messages[-1].content


def test_candidate_cannot_claim_classes_owned_by_another_candidate(tmp_path: Path) -> None:
    """Deterministic ownership cannot be bypassed by swapping candidate classes."""
    domains = _commerce_domains()
    candidates = [
        commerce_candidate("Inventory", domains["Orders"]),
        commerce_candidate("Orders", domains["Inventory"]),
        commerce_candidate("Notifications", domains["Notifications"]),
        commerce_candidate("Customers", domains["Customers"]),
    ]
    output = commerce_output(candidate_services=candidates)
    write_commerce_evidence(tmp_path)
    content = json.dumps(output)
    with pytest.raises(BoundaryResponseError, match="missing class assignments"):
        run_agent(tmp_path, FakeRouter([content, content]))


def test_infrastructure_and_test_classes_need_not_be_services(tmp_path: Path) -> None:
    """Application/configuration/test classes are outside strong domain coverage."""
    architecture, graph = _commerce_evidence()
    extra = [
        ClassArchitectureMetric(
            fully_qualified_name="com.example.commerce.DemoApplication",
            name="DemoApplication",
            package="com.example.commerce",
            role=JavaClassRole.APPLICATION,
            in_degree=0,
            out_degree=0,
            fan_in=0,
            fan_out=0,
        ),
        ClassArchitectureMetric(
            fully_qualified_name="com.example.commerce.config.ClockConfiguration",
            name="ClockConfiguration",
            package="com.example.commerce.config",
            role=JavaClassRole.CONFIGURATION,
            in_degree=0,
            out_degree=0,
            fan_in=0,
            fan_out=0,
        ),
        ClassArchitectureMetric(
            fully_qualified_name="com.example.commerce.inventory.InventoryTest",
            name="InventoryTest",
            package="com.example.commerce.inventory",
            role=JavaClassRole.OTHER,
            in_degree=0,
            out_degree=0,
            fan_in=0,
            fan_out=0,
        ),
    ]
    architecture = architecture.model_copy(
        update={"class_metrics": [*architecture.class_metrics, *extra]}
    )
    architecture.components[0].classes.append("com.example.commerce.inventory.InventoryTest")
    write_file(tmp_path, ARCHITECTURE_REPORT_ARTIFACT, architecture.model_dump_json())
    write_file(tmp_path, JAVA_DEPENDENCY_ARTIFACT, graph.model_dump_json())
    assert run_agent(tmp_path, FakeRouter([json.dumps(commerce_output())])).success


def test_bounded_repair_fixes_coverage_omission(tmp_path: Path) -> None:
    """One structured retry can repair missing evidence accounting."""
    domains = _commerce_domains()
    incomplete = commerce_output(
        accounting=[
            {"candidate": name, "disposition": "INCLUDED", "classes": classes}
            for name, classes in domains.items()
            if name != "Inventory"
        ]
    )
    result = run_commerce_output(tmp_path, [incomplete, commerce_output()])
    assert result.success


def test_bounded_repair_still_incomplete_fails(tmp_path: Path) -> None:
    """The existing single-retry bound remains fail-closed."""
    domains = _commerce_domains()
    incomplete = commerce_output(
        accounting=[
            {"candidate": name, "disposition": "INCLUDED", "classes": classes}
            for name, classes in domains.items()
            if name != "Inventory"
        ]
    )
    with pytest.raises(BoundaryResponseError, match="one repair attempt"):
        run_commerce_output(tmp_path, [incomplete, incomplete])


def test_coverage_request_is_compact_and_secret_free(tmp_path: Path) -> None:
    """Coverage evidence contains structured candidates, not prompts, source, or secrets."""
    write_commerce_evidence(tmp_path)
    router = FakeRouter([json.dumps(commerce_output())])
    run_agent(tmp_path, router)
    request = router.requests[0].messages[1].content
    assert "candidate_domains" in request
    assert '"required_classes_by_candidate"' in request
    assert all(class_name in request for class_name in _commerce_domains()["Inventory"])
    assert "prompt" not in request.casefold()
    assert "api_key" not in request.casefold()
    assert "public class" not in request


def test_sanitized_coverage_output_is_deterministic(tmp_path: Path) -> None:
    """Repeated offline validation yields the same candidate accounting."""
    first = run_commerce_output(tmp_path, [commerce_output()])
    second = run_commerce_output(tmp_path, [commerce_output()])
    assert first.metadata["service_boundary_report"]["evidence_accounting"] == second.metadata[
        "service_boundary_report"
    ]["evidence_accounting"]
