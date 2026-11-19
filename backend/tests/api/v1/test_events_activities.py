from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.repositories.user_connection_repository import UserConnectionRepository
from app.services.providers.garmin.data_247 import Garmin247Data
from app.services.providers.garmin.oauth import GarminOAuth
from tests.factories import ApiKeyFactory, UserFactory
from tests.utils import api_key_headers


def _garmin_247() -> Garmin247Data:
    oauth = GarminOAuth(
        user_repo=MagicMock(),
        connection_repo=UserConnectionRepository(),
        provider_name="garmin",
        api_base_url="https://apis.garmin.com",
    )
    return Garmin247Data(provider_name="garmin", api_base_url="https://apis.garmin.com", oauth=oauth)


class TestListActivities:
    """GET /users/{user_id}/events/activities"""

    def test_lists_a_garmin_move_iq_activity(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        move_iq = {
            "summaryId": "x3f1-miq-2024-01-15-walking",
            "startTimeInSeconds": 1705320000,  # 2024-01-15 12:00 UTC
            "durationInSeconds": 1680,
            "startTimeOffsetInSeconds": 3600,
            "activityType": "Walking",
            "activitySubType": "Hurried",
        }
        _garmin_247().process_items_batch(db, user.id, "moveIQActivities", [move_iq])
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/events/activities",
            headers=api_key_headers(ApiKeyFactory().plain_key),
            params={"start_date": "2024-01-15T00:00:00Z", "end_date": "2024-01-16T00:00:00Z"},
        )

        assert response.status_code == 200
        [activity] = response.json()["data"]
        assert {
            key: activity[key]
            for key in ("type", "subtype", "start_time", "end_time", "zone_offset", "duration_seconds")
        } == {
            "type": "moveiq_walking",
            "subtype": "hurried",
            "start_time": "2024-01-15T12:00:00Z",
            "end_time": "2024-01-15T12:28:00Z",
            "zone_offset": "+01:00",
            "duration_seconds": 1680,
        }
        assert activity["source"]["provider"] == "garmin"

    def test_leaves_out_other_event_categories(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        night = {"summaryId": "sleep_1", "startTimeInSeconds": 1705269600, "durationInSeconds": 28800}
        _garmin_247().process_items_batch(db, user.id, "sleeps", [night])
        db.commit()

        response = client.get(
            f"/api/v1/users/{user.id}/events/activities",
            headers=api_key_headers(ApiKeyFactory().plain_key),
            params={"start_date": "2024-01-14T00:00:00Z", "end_date": "2024-01-16T00:00:00Z"},
        )

        assert response.status_code == 200
        assert response.json()["data"] == []

    def test_pages_forward_and_back_by_cursor(self, client: TestClient, db: Session) -> None:
        user = UserFactory()
        walks = [
            {
                "summaryId": f"miq-{hour}",
                "startTimeInSeconds": 1705320000 + hour * 3600,
                "durationInSeconds": 600,
                "activityType": "Walking",
            }
            for hour in range(3)
        ]
        _garmin_247().process_items_batch(db, user.id, "moveIQActivities", walks)
        db.commit()
        headers = api_key_headers(ApiKeyFactory().plain_key)

        def page(cursor: str | None = None) -> tuple[list[str], str | None, str | None]:
            params = {"start_date": "2024-01-15T00:00:00Z", "end_date": "2024-01-16T00:00:00Z", "limit": 1}
            response = client.get(
                f"/api/v1/users/{user.id}/events/activities",
                headers=headers,
                params={**params, **({"cursor": cursor} if cursor else {})},
            )
            assert response.status_code == 200
            body = response.json()
            pagination = body["pagination"]
            return [a["start_time"] for a in body["data"]], pagination["next_cursor"], pagination["previous_cursor"]

        first, next_cursor, previous = page()
        second, next_cursor, previous = page(next_cursor)
        third, last_next, _ = page(next_cursor)
        back, _, back_previous = page(previous)

        assert len({*first, *second, *third}) == 3
        assert last_next is None
        assert back == first
        assert back_previous is None
