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
            key: activity[key] for key in ("type", "start_time", "end_time", "zone_offset", "duration_seconds")
        } == {
            "type": "moveiq_walking",
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
