"""Shared test policy for supported and unsupported SQLite runtimes."""

from __future__ import annotations

import pytest

from aipcs_mcp.configuration.resolver import (
    is_supported_sqlite_platform,
    is_supported_sqlite_runtime,
)

_SQLITE_NODEIDS = frozenset(
    {
        "tests/test_admin_cli_lifecycle.py::test_sqlite_cli_lifecycle_replay_stale_and_exact_transitions",
        "tests/test_admin_cli_purge.py::test_sqlite_cli_purge_override_replay_and_active_refusal",
        "tests/test_admin_cli_transfer.py::test_export_dry_run_import_replay_collision_and_tamper",
        "tests/test_admin_cli_transfer.py::test_import_rejects_symlink_without_echoing_path",
        "tests/test_composition.py::test_read_only_admin_composition_does_not_create_or_migrate_sqlite",
        "tests/test_composition.py::test_private_portable_composition_selects_the_configured_sqlite_backend",
        "tests/test_composition.py::test_ready_sqlite_migrates_once_before_mcp_construction",
        "tests/test_composition.py::test_ready_sqlite_server_info_does_not_allocate_or_migrate_a_service_store",
        "tests/test_composition.py::test_non_ready_sqlite_fails_before_mcp_construction[uninitialised]",
        "tests/test_composition.py::test_non_ready_sqlite_fails_before_mcp_construction[dirty]",
        "tests/test_composition.py::test_non_ready_sqlite_fails_before_mcp_construction[incompatible]",
        "tests/test_composition.py::test_startup_failures_are_one_bounded_stderr_envelope[dirty]",
        "tests/test_composition.py::test_startup_failures_are_one_bounded_stderr_envelope[incompatible]",
        "tests/test_private_relational_boundary.py::test_private_relational_schema_isolated_from_registry_and_public_restart",
        "tests/test_release_verifier.py::test_embedded_catalog_client_exercises_the_source_contract",
        "tests/test_release_verifier.py::test_embedded_domain_schema_client_exercises_the_source_contract",
        "tests/test_release_verifier.py::test_embedded_lifecycle_coordinator_client_exercises_the_source_contract",
        "tests/test_release_verifier.py::test_embedded_portable_lifecycle_client_uses_private_streams_and_restarts",
        "tests/test_release_verifier.py::test_embedded_registry_r2_client_exercises_the_source_contract",
        "tests/test_stdio_lifecycle.py::test_public_pending_and_recovery_required_projection",
        "tests/test_stdio_lifecycle.py::test_two_same_key_stdio_processes_resume_one_prepared_materialise_once",
        "tests/test_stdio_lifecycle.py::test_different_key_stdio_request_is_blocked_before_service_store_work",
        "tests/test_streamable_http_smoke.py::test_streamable_http_server_info_smoke",
    }
)
_SQLITE_REASON = (
    "requires SQLite 3.51.3 or newer because older runtimes contain the WAL-reset "
    "data-integrity defect; use the managed Python 3.14 test path"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep non-SQLite tests useful when a local runtime is intentionally unsupported."""

    if is_supported_sqlite_platform() and is_supported_sqlite_runtime():
        return
    skip = pytest.mark.skip(reason=_SQLITE_REASON)
    for item in items:
        if item.nodeid.startswith("tests/storage_sqlite/") or item.nodeid in _SQLITE_NODEIDS:
            item.add_marker(skip)
