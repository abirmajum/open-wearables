"""Domain errors raised by the shared provider API client.

A missing or dead connection is the user's state at the provider, never a 401 (which reads
as a bad API key), and a failed call keeps the provider's status and body out of ``detail``.
"""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any
from unittest.mock import MagicMock, create_autospec
from uuid import uuid4

import httpx
import pytest
import respx
from pytest_mock import MockerFixture
from sqlalchemy.orm import Session

from app.models import UserConnection
from app.repositories import UserConnectionRepository
from app.services.providers import api_client
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.exceptions import (
    InvalidInputError,
    ProviderAuthRejectedError,
    ProviderError,
    ProviderNotConnectedError,
    ProviderRateLimitError,
    ProviderTokenUnavailableError,
    ReauthRequiredError,
)

URL = "https://api.provider.test/v1/data"


def _token_args(connection: Any) -> dict[str, Any]:
    connection_repo = create_autospec(UserConnectionRepository, instance=True)
    connection_repo.get_by_user_and_provider.return_value = connection
    return {
        "db": MagicMock(spec=Session, info={}),
        "user_id": uuid4(),
        "provider_name": "garmin",
        "connection_repo": connection_repo,
        "oauth": create_autospec(BaseOAuthTemplate, instance=True),
    }


def _request_args() -> dict[str, Any]:
    return {
        "db": MagicMock(spec=Session, info={}),
        "user_id": uuid4(),
        "connection_repo": create_autospec(UserConnectionRepository, instance=True),
        "oauth": create_autospec(BaseOAuthTemplate, instance=True),
        "api_base_url": "https://api.provider.test",
        "provider_name": "garmin",
        "endpoint": "/v1/data",
    }


@pytest.fixture
def valid_token(mocker: MockerFixture) -> None:
    mocker.patch("app.services.providers.api_client.get_valid_token", autospec=True, return_value="token")


class TestGetValidToken:
    def test_no_connection(self) -> None:
        with pytest.raises(ProviderNotConnectedError) as exc_info:
            api_client.get_valid_token(**_token_args(None))

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "User is not connected to Garmin."

    def test_connection_without_a_token(self) -> None:
        with pytest.raises(ProviderTokenUnavailableError) as exc_info:
            api_client.get_valid_token(**_token_args(UserConnection(access_token=None)))

        assert exc_info.value.status_code == 400

    def test_expired_token_without_a_refresh_token(self) -> None:
        connection = UserConnection(
            access_token="at",
            refresh_token=None,
            token_expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )

        with pytest.raises(ReauthRequiredError) as exc_info:
            api_client.get_valid_token(**_token_args(connection))

        assert exc_info.value.status_code == 409


@pytest.mark.usefixtures("valid_token")
class TestMakeAuthenticatedRequest:
    def test_a_rejected_token_is_an_upstream_error_not_a_reconnect(
        self, mocker: MockerFixture, respx_mock: respx.MockRouter
    ) -> None:
        """Not proof the grant is dead: only a day of rejections revokes it (ReauthRequiredError, 409)."""
        mocker.patch("app.services.providers.api_client._reload_token_after_401", autospec=True, return_value=None)
        respx_mock.get(URL).respond(401, text="invalid_token")

        with pytest.raises(ProviderAuthRejectedError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert exc_info.value.status_code == 502

    def test_error_response_keeps_the_body_out_of_the_detail(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(404, text='{"message": "user 42 not found"}')

        with pytest.raises(ProviderError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        error = exc_info.value
        assert (error.status_code, error.upstream_status) == (502, 404)
        assert error.detail == "Garmin API error (status 404)"
        assert error.upstream_body == '{"message": "user 42 not found"}'

    def test_throttle_that_persists_is_a_503(self, mocker: MockerFixture, respx_mock: respx.MockRouter) -> None:
        mocker.patch("app.services.providers.api_client.time.sleep", autospec=True)
        route = respx_mock.get(URL).respond(429, text="slow down")

        with pytest.raises(ProviderRateLimitError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert (exc_info.value.status_code, exc_info.value.upstream_status) == (503, 429)
        assert route.call_count == 4

    @pytest.mark.parametrize(
        ("retry_after", "delays"),
        [
            ("7", [7.0, 7.5, 15.0]),  # honoured, never under a quarter of the backoff
            ("0", [3.75, 7.5, 15.0]),  # floored: a "0" must not spend every retry in seconds
            ("-5", [3.75, 7.5, 15.0]),
            ("3600", [120.0, 60.0]),  # capped per wait, then by the request's total of 180s
            ("inf", [120.0, 60.0]),
            ("soon", [15.0, 30.0, 60.0]),  # unreadable: the exponential backoff
            (None, [15.0, 30.0, 60.0]),
        ],
    )
    def test_throttle_waits_for_retry_after(
        self, mocker: MockerFixture, respx_mock: respx.MockRouter, retry_after: str | None, delays: list[float]
    ) -> None:
        sleep = mocker.patch("app.services.providers.api_client.time.sleep", autospec=True)
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        respx_mock.get(URL).respond(429, headers=headers)

        with pytest.raises(ProviderRateLimitError):
            api_client.make_authenticated_request(**_request_args())

        assert [c.args[0] for c in sleep.call_args_list] == delays

    def test_a_retry_after_date_in_the_past_waits_the_floor(
        self, mocker: MockerFixture, respx_mock: respx.MockRouter
    ) -> None:
        sleep = mocker.patch("app.services.providers.api_client.time.sleep", autospec=True)
        retry_at = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=5), usegmt=True)
        respx_mock.get(URL).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": retry_at}), httpx.Response(200, json={})]
        )

        assert api_client.make_authenticated_request(**_request_args()) == {}

        assert [c.args[0] for c in sleep.call_args_list] == [3.75]

    def test_throttle_waits_until_a_retry_after_date(self, mocker: MockerFixture, respx_mock: respx.MockRouter) -> None:
        sleep = mocker.patch("app.services.providers.api_client.time.sleep", autospec=True)
        retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
        respx_mock.get(URL).mock(
            side_effect=[httpx.Response(429, headers={"Retry-After": retry_at}), httpx.Response(200, json={})]
        )

        assert api_client.make_authenticated_request(**_request_args()) == {}

        [wait] = [c.args[0] for c in sleep.call_args_list]
        assert 25 <= wait <= 30

    def test_no_response(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

        with pytest.raises(ProviderError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert exc_info.value.upstream_status is None
        assert exc_info.value.detail == "Garmin API request failed"

    def test_empty_body_where_json_is_expected(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(204)

        with pytest.raises(ProviderError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert exc_info.value.upstream_status == 204

    def test_error_in_a_200_body(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(200, text='{"error": "not found", "code": 404, "request_id": "r1"}')

        with pytest.raises(ProviderError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert exc_info.value.upstream_status == 404
        assert exc_info.value.upstream_body == '{"error": "not found", "code": 404, "request_id": "r1"}'

    def test_error_in_a_200_body_without_a_code(self, respx_mock: respx.MockRouter) -> None:
        """The provider still rejected the request: dropped by a task like an upstream 400, not retried."""
        respx_mock.get(URL).respond(200, json={"error": "Workout not found"})

        with pytest.raises(ProviderError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert exc_info.value.upstream_status == 400
        assert exc_info.value.detail == "Garmin API error (status 400)"

    def test_error_in_a_200_body_with_a_string_code(self, respx_mock: respx.MockRouter) -> None:
        """A provider outage reported as "503" is still retried by a task, not dropped as a 400."""
        respx_mock.get(URL).respond(200, json={"error": "maintenance", "code": "503"})

        with pytest.raises(ProviderError) as exc_info:
            api_client.make_authenticated_request(**_request_args())

        assert exc_info.value.upstream_status == 503


@pytest.mark.usefixtures("valid_token")
class TestDownloadBinaryContent:
    def _download(self, url: str = URL) -> bytes:
        args = _request_args()
        return api_client.download_binary_content(
            db=args["db"],
            user_id=args["user_id"],
            connection_repo=args["connection_repo"],
            oauth=args["oauth"],
            provider_name="garmin",
            url=url,
            allowed_hosts={"api.provider.test"},
        )

    def test_downloads_from_an_allowed_host_without_following_redirects(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(200, content=b"fit")

        assert self._download() == b"fit"

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example/x",
            "http://api.provider.test/v1/data",
            "https://api.provider.test.evil.example/x",
            "https://user@api.provider.test/x",
            "https://api.provider.test:8443/x",
            "https://169.254.169.254/latest/meta-data",
            "file:///etc/passwd",
        ],
    )
    def test_a_url_off_the_allowed_hosts_is_never_fetched(self, url: str) -> None:
        with pytest.raises(InvalidInputError):
            self._download(url)

        api_client.get_valid_token.assert_not_called()

    def test_a_redirect_off_the_allowed_hosts_is_not_followed(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(302, headers={"Location": "https://evil.example/steal"})

        with pytest.raises(InvalidInputError):
            self._download()

        assert [str(call.request.url) for call in respx_mock.calls] == [URL]

    def test_a_redirect_on_an_allowed_host_is_followed(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(302, headers={"Location": "/v1/file"})
        respx_mock.get("https://api.provider.test/v1/file").respond(200, content=b"fit")

        assert self._download() == b"fit"
        assert [str(call.request.url) for call in respx_mock.calls] == [URL, "https://api.provider.test/v1/file"]

    def test_a_redirect_loop_stops(self, respx_mock: respx.MockRouter) -> None:
        route = respx_mock.get(URL).respond(302, headers={"Location": URL})

        with pytest.raises(ProviderError):
            self._download()

        assert route.call_count == api_client.MAX_DOWNLOAD_REDIRECTS + 1

    def test_error_response(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).respond(403, text="forbidden")

        with pytest.raises(ProviderError) as exc_info:
            self._download()

        assert exc_info.value.upstream_status == 403

    def test_no_response(self, respx_mock: respx.MockRouter) -> None:
        respx_mock.get(URL).mock(side_effect=httpx.ReadTimeout("timed out"))

        with pytest.raises(ProviderError) as exc_info:
            self._download()

        assert exc_info.value.upstream_status is None
