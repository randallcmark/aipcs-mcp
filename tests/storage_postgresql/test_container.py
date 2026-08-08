"""SYNTHETIC_FIXTURE. Synthetic fixture provenance: subprocess-only fixture safety tests."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from .container import (
    ACTIVATION_ENV,
    IMAGE_ENV,
    MAJOR_ENV,
    PINNED_POSTGRES_IMAGES,
    DisposablePostgres,
    PostgresContainerSettings,
    PostgresFixtureError,
)

_IMAGE = PINNED_POSTGRES_IMAGES["16"]
_ID = "b" * 64


class RecordingRunner:
    def __init__(self, *, ready: bool = True, port: str = "127.0.0.1:45678\n") -> None:
        self.commands: list[tuple[str, ...]] = []
        self.ready = ready
        self.port = port

    def __call__(self, command: Sequence[str], check: bool) -> subprocess.CompletedProcess[str]:
        captured = tuple(command)
        self.commands.append(captured)
        if captured[:3] == ("docker", "run", "--detach"):
            return subprocess.CompletedProcess(captured, 0, stdout=f"{_ID}\n", stderr="")
        if captured[:2] == ("docker", "port"):
            return subprocess.CompletedProcess(captured, 0, stdout=self.port, stderr="")
        if captured[:3] == ("docker", "exec", _ID):
            return subprocess.CompletedProcess(captured, int(not self.ready), stdout="", stderr="")
        return subprocess.CompletedProcess(captured, 0, stdout="image-id\n", stderr="")


def _settings() -> PostgresContainerSettings:
    return PostgresContainerSettings(_IMAGE, "16")


def _return_init_role(target, _username, _password):  # type: ignore[no-untyped-def]
    return target


def test_incomplete_or_absent_opt_in_returns_no_settings() -> None:
    assert PostgresContainerSettings.optional_from_environ({}) is None
    assert PostgresContainerSettings.optional_from_environ({ACTIVATION_ENV: "container"}) is None
    assert PostgresContainerSettings.optional_from_environ(
        {ACTIVATION_ENV: "anything-else", IMAGE_ENV: _IMAGE, MAJOR_ENV: "16"}
    ) is None


@pytest.mark.parametrize("major", ["16", "17", "18"])
def test_settings_accept_the_exact_image_for_each_supported_matrix_major(major: str) -> None:
    settings = PostgresContainerSettings(PINNED_POSTGRES_IMAGES[major], major)

    assert settings.image == PINNED_POSTGRES_IMAGES[major]


@pytest.mark.parametrize(
    ("image", "major"),
    [
        ("postgres:16", "16"),
        ("postgres:17@sha256:" + "a" * 64, "17"),
        ("postgres@sha256:" + "a" * 63, "18"),
        (PINNED_POSTGRES_IMAGES["18"], "16"),
    ],
)
def test_settings_reject_unpinned_or_non_matrix_images(image: str, major: str) -> None:
    with pytest.raises(PostgresFixtureError):
        PostgresContainerSettings(image, major)


def test_start_uses_only_a_pinned_labeled_loopback_container_and_exact_id_cleanup() -> None:
    runner = RecordingRunner()
    fixture = DisposablePostgres(
        _settings(), runner=runner, sleeper=lambda _: None, provisioner=_return_init_role
    )

    target = fixture.start()
    fixture.close()

    assert target.host == "127.0.0.1"
    assert target.port == 45678
    assert target.dsn.startswith("postgresql://")
    assert target.password not in repr(target)
    assert target.password not in repr(fixture)
    run = next(command for command in runner.commands if command[:3] == ("docker", "run", "--detach"))
    assert "--pull=never" in run
    published = (run[run.index("--publish")], run[run.index("--publish") + 1])
    assert published == ("--publish", "127.0.0.1::5432")
    assert "--mount" not in run and "--volume" not in run and "-v" not in run
    assert "compose" not in run and "prune" not in run
    assert run[-1] == _IMAGE
    assert runner.commands[0] == ("docker", "image", "inspect", "--format", "{{.Id}}", _IMAGE)
    assert runner.commands[-1] == ("docker", "container", "rm", "--force", _ID)
    assert target.password not in " ".join(" ".join(command) for command in runner.commands if command[:3] != ("docker", "run", "--detach"))


@pytest.mark.parametrize("port", ["0.0.0.0:45678\n", "[::1]:45678\n", "127.0.0.1:70000\n"])
def test_start_rejects_non_loopback_or_invalid_published_ports_and_cleans_exact_id(port: str) -> None:
    runner = RecordingRunner(port=port)
    fixture = DisposablePostgres(
        _settings(), runner=runner, sleeper=lambda _: None, provisioner=_return_init_role
    )

    with pytest.raises(PostgresFixtureError):
        fixture.start()

    assert runner.commands[-1] == ("docker", "container", "rm", "--force", _ID)


def test_readiness_failure_cleans_exact_id_without_printing_credential() -> None:
    runner = RecordingRunner(ready=False)
    fixture = DisposablePostgres(
        _settings(), runner=runner, sleeper=lambda _: None, provisioner=_return_init_role
    )

    with pytest.raises(PostgresFixtureError) as captured:
        fixture.start()

    assert "postgresql://" not in str(captured.value)
    assert runner.commands[-1] == ("docker", "container", "rm", "--force", _ID)


def test_runtime_role_setup_exception_cleans_exact_id() -> None:
    runner = RecordingRunner()

    def fail_setup(_target, _username, _password):  # type: ignore[no-untyped-def]
        raise PostgresFixtureError("synthetic setup failure")

    fixture = DisposablePostgres(
        _settings(), runner=runner, sleeper=lambda _: None, provisioner=fail_setup
    )

    with pytest.raises(PostgresFixtureError, match="synthetic setup failure"):
        fixture.start()

    assert runner.commands[-1] == ("docker", "container", "rm", "--force", _ID)


def test_opt_in_fixture_skips_without_invoking_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ACTIVATION_ENV, raising=False)
    monkeypatch.delenv(IMAGE_ENV, raising=False)
    monkeypatch.delenv(MAJOR_ENV, raising=False)
    settings = PostgresContainerSettings.optional_from_environ({})

    assert settings is None
