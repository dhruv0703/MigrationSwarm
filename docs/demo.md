# Local demonstration

The fixture at `examples/demo-commerce-monolith` is a small Spring Boot 3
commerce monolith using Java 17 source compatibility, Maven, REST controllers,
Spring Data JPA, H2, and JUnit. Its Inventory and Orders domains are coupled;
Notifications is an independent branch and shared utilities remain visible to
the analysis.

Run the baseline first:

```powershell
Push-Location examples/demo-commerce-monolith
mvn test
Pop-Location
```

Run the fully local deterministic demonstration:

```powershell
migrationswarm demo ./examples/demo-commerce-monolith --offline
migrationswarm migration-status ./examples/demo-commerce-monolith
migrationswarm benchmark ./examples/demo-commerce-monolith
```

Offline mode makes no provider calls. It performs the real local analyses,
creates explicit approvals, plans the selected services, creates isolated Git
worktrees, extracts generated service projects, runs Maven verification, and
writes metrics and benchmark artifacts under `.migrationswarm/`. Generated
service directories are preserved under `.migrationswarm/demo-services/`.

Live mode is optional and uses the configured model registry:

```powershell
migrationswarm demo ./examples/demo-commerce-monolith --live
```

The demo does not modify monolith application source, split databases, deploy
containers, commit or push changes, or provide production migration guarantees.
Missing provider token counts remain `null`; credentials are never written to
the metrics or benchmark artifacts.
