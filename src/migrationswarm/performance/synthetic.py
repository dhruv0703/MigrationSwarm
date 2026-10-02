"""Bounded deterministic Java repository fixtures for local measurements."""

from __future__ import annotations

from pathlib import Path


def generate_synthetic_repository(root: Path, class_count: int) -> Path:
    """Create a predictable Java source tree without a build file."""
    if class_count not in {25, 100, 500}:
        raise ValueError("synthetic class_count must be one of 25, 100, or 500")
    source_root = root / "src" / "main" / "java"
    for index in range(class_count):
        group = index % 5
        package = f"com.example.synthetic.domain{group}"
        directory = source_root / Path(*package.split("."))
        directory.mkdir(parents=True, exist_ok=True)
        name = f"Class{index:03d}"
        previous = index - 1
        import_line = ""
        field_line = ""
        if previous >= 0:
            previous_group = previous % 5
            import_line = (
                f"import com.example.synthetic.domain{previous_group}.Class{previous:03d};\n"
            )
            field_line = f"    private Class{previous:03d} previous;\n"
        content = (
            f"package {package};\n\n"
            f"{import_line}\n"
            f"public class {name} {{\n"
            f"{field_line}"
            "}\n"
        )
        (directory / f"{name}.java").write_text(content, encoding="utf-8")
    return root


__all__ = ["generate_synthetic_repository"]
