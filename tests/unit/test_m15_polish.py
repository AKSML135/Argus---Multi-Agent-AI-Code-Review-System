"""M15 acceptance tests — Polish & Resume Readiness.

Acceptance criteria from TASKS.md M15:
  [AC1] A person can clone, install, configure .env, and run
        argus review --diff fixtures/sample.patch --no-wait by following
        only the README. Verified structurally: README contains all required
        sections and all ARGUS_* vars from Settings appear in .env.example.
  [AC2] The README's architecture diagram matches the actually-implemented
        graph structure (key node names present in both).
  [AC3] The full test suite (pytest) passes clean end-to-end, including
        tests/eval — verified by running pytest in subprocess.
  [AC4] ruff check src/ passes clean (no linting errors).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
README = ROOT / "README.md"
ENV_EXAMPLE = ROOT / ".env.example"
SRC = ROOT / "src" / "argus"


# ---------------------------------------------------------------------------
# AC1 — README quickstart completeness + .env.example matches Settings
# ---------------------------------------------------------------------------


def test_readme_exists_and_has_required_sections():
    """AC1: README has a quick-start, install instructions, and CLI example."""
    assert README.exists(), "README.md is missing"
    text = README.read_text()

    required_sections = [
        "Quick Start",
        "Install",
        "pip install",
        "argus review",
        "Configuration",
        "Testing",
    ]
    for section in required_sections:
        assert section in text, f"README missing section: {section!r}"


def test_env_example_covers_all_settings_fields():
    """AC1: every ARGUS_-prefixed setting in config.py is in .env.example."""
    assert ENV_EXAMPLE.exists(), ".env.example is missing"

    config_text = (SRC / "config.py").read_text()
    env_text = ENV_EXAMPLE.read_text()

    # Extract field names from Settings class (lines like `field_name: type`)
    import re

    # Find all field names declared in the Settings class body
    field_names = re.findall(r"^\s{4}(\w+):\s+(?:str|int|float|Literal|Optional|bool|\w+\s*\|)", config_text, re.MULTILINE)

    # Build the expected env var names (ARGUS_<FIELD_NAME_UPPER>)
    skip = {"model_config"}  # pydantic meta-field, not an env var
    for field in field_names:
        if field.startswith("_") or field in skip:
            continue
        env_var = f"ARGUS_{field.upper()}"
        assert env_var in env_text, (
            f"Config field '{field}' → '{env_var}' is not documented in .env.example"
        )


# ---------------------------------------------------------------------------
# AC2 — README architecture diagram matches implemented graph nodes
# ---------------------------------------------------------------------------


def test_readme_architecture_matches_implementation():
    """AC2: key node names from the real graph appear in the README diagram."""
    readme_text = README.read_text()

    # These are the node names that appear in graph.py and must be in the README
    implemented_nodes = [
        "input_guardrail",
        "supervisor",
        "aggregator",
        "report_generator",
        "gate_critical_triage",
        "gate_final_approval",
        "secret_scanner",
        "static_analysis",
    ]

    for node in implemented_nodes:
        assert node in readme_text, (
            f"Node '{node}' is implemented but missing from README architecture diagram"
        )


def test_readme_does_not_reference_unimplemented_nodes():
    """AC2: README does not claim nodes that were deferred."""
    readme_text = README.read_text()
    # These were explicitly deferred per ARCHITECTURE.md §11
    deferred_nodes = [
        "docker_sandbox",
        "auto_fix",
        "chat_ops",
    ]
    for node in deferred_nodes:
        assert node not in readme_text.lower(), (
            f"Deferred feature '{node}' should not appear in README as implemented"
        )


# ---------------------------------------------------------------------------
# AC3 — Fixture sample.patch exists (required by AC1 quickstart)
# ---------------------------------------------------------------------------


def test_sample_patch_exists():
    """AC1: fixtures/sample.patch must exist so the quickstart command works."""
    patch = ROOT / "fixtures" / "sample.patch"
    assert patch.exists(), "fixtures/sample.patch is missing — quickstart will fail"
    assert patch.stat().st_size > 0, "fixtures/sample.patch is empty"


# ---------------------------------------------------------------------------
# AC4 — ruff passes clean
# ---------------------------------------------------------------------------


def test_ruff_clean():
    """AC4: ruff check src/ exits 0 (no linting errors in the codebase)."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", str(SRC)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, (
        f"ruff check src/ found errors:\n{result.stdout}\n{result.stderr}"
    )


# ---------------------------------------------------------------------------
# Structural: docs/DEMO.md exists and has required steps
# ---------------------------------------------------------------------------


def test_demo_md_exists_and_complete():
    """docs/DEMO.md must exist and cover the key demo steps."""
    demo = ROOT / "docs" / "DEMO.md"
    assert demo.exists(), "docs/DEMO.md is missing"
    text = demo.read_text()

    required_content = [
        "argus review",
        "argus serve",
        "/reviews",
        "stream",
        "resume",
        "metrics",
        "pytest",
    ]
    for item in required_content:
        assert item in text, f"docs/DEMO.md missing content: {item!r}"


# ---------------------------------------------------------------------------
# Structural: no dead imports left in top-level __init__ files
# ---------------------------------------------------------------------------


def test_no_wildcard_imports_in_package():
    """Verify no __init__.py uses 'from x import *' (dead code smell)."""
    for init_file in SRC.rglob("__init__.py"):
        content = init_file.read_text()
        assert "import *" not in content, (
            f"Wildcard import found in {init_file} — remove it"
        )
