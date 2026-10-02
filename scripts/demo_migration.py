"""Run the MigrationSwarm commerce demo without duplicating orchestration logic."""

from migrationswarm.demo import run_demo

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the MigrationSwarm commerce demo.")
    parser.add_argument("path", help="Path to examples/demo-commerce-monolith")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline", action="store_true", help="Use deterministic fake model responses."
    )
    mode.add_argument("--live", action="store_true", help="Use configured provider credentials.")
    args = parser.parse_args()
    result = run_demo(args.path, live=args.live)
    print(f"status={result.status} run_id={result.run_id} offline={result.offline}")
    print(f"metrics={result.metrics_path}")
    print(f"benchmark={result.benchmark_path}")
    print(f"main_repository_modified={'yes' if result.main_repository_modified else 'no'}")
    for directory in result.generated_service_directories:
        print(f"generated_service={directory}")
