from pathlib import Path

import pytest

from tests.utils.api_key_auth_scan import count_resource_id_auth_violations


@pytest.fixture(autouse=True)
def set_factory_session() -> None:
    return


@pytest.fixture(autouse=True)
def flush_redis() -> None:
    return


@pytest.fixture(scope="session", autouse=True)
def _configure_redis() -> None:
    return


@pytest.mark.parametrize(
    "source",
    [
        "api_key_headers(str(api_key.id))",
        "{'X-Open-Wearables-API-Key': str(api_key.id)}",
    ],
)
def test_wrapped_resource_ids_are_rejected(source: str) -> None:
    assert count_resource_id_auth_violations(source) == 1


@pytest.mark.parametrize(
    "source",
    [
        "api_key_headers(api_key)",
        "{'X-Open-Wearables-API-Key': api_key_secret(api_key)}",
    ],
)
def test_secret_bearing_auth_calls_are_accepted(source: str) -> None:
    assert count_resource_id_auth_violations(source) == 0


def test_backend_auth_call_sites_do_not_use_resource_ids() -> None:
    tests_root = Path(__file__).parents[1]
    violating_paths = [
        path.relative_to(tests_root)
        for path in sorted(tests_root.rglob("*.py"))
        if count_resource_id_auth_violations(path.read_text())
    ]

    assert violating_paths == []
