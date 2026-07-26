from aipcs_mcp.configuration import reporting
from aipcs_mcp.configuration.models import ConfigOverrides
from aipcs_mcp.configuration.reporting import safe_config_report, safe_validation_report
from aipcs_mcp.configuration.resolver import resolve_configuration


def test_reports_are_allowlisted(monkeypatch) -> None:
    monkeypatch.setattr(reporting, "is_supported_sqlite_runtime", lambda: True)
    config = resolve_configuration(
        overrides=ConfigOverrides(
            profile="sqlite", principal_id="secret-principal", sqlite_data_root="/private/root"
        ),
        environ={},
        config_path=None,
    )
    report = safe_config_report(config)
    assert "secret" not in repr(report) and "/private" not in repr(report)
    assert safe_validation_report(config)["runnable"] is True
