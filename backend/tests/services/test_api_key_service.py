from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.services.api_key_service import api_key_service
from tests.factories import ApiKeyFactory, DeveloperFactory
from tests.utils import api_key_secret


class TestApiKeyServiceCreateApiKey:
    """Test API key creation with proper format."""

    def test_create_api_key_separates_id_and_secret(self, db: Session) -> None:
        # Arrange
        developer = DeveloperFactory()

        # Act
        api_key = api_key_service.create_api_key(db, developer.id, "Test Key")

        # Assert
        assert isinstance(api_key.id, UUID)
        assert len(api_key.secret) == 67
        assert "secret=" not in repr(api_key)
        assert api_key.name == "Test Key"
        assert api_key.created_by == developer.id

    def test_create_api_key_persists_only_hash(self, db: Session) -> None:
        # Arrange
        developer = DeveloperFactory()

        # Act
        api_key = api_key_service.create_api_key(db, developer.id)

        persisted = api_key_service.get(db, api_key.id)
        assert persisted is not None
        assert persisted.key_hash == api_key_service.hash_secret(api_key.secret)
        assert not hasattr(persisted, "secret")
        assert "key_hash" not in repr(persisted)

    def test_create_api_key_default_name(self, db: Session) -> None:
        """Should use default name when not provided."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        api_key = api_key_service.create_api_key(db, developer.id)

        # Assert
        assert api_key.name == "Default"

    def test_create_api_key_without_developer(self, db: Session) -> None:
        """Should create API key with None as created_by."""
        # Act
        api_key = api_key_service.create_api_key(db, None, "Anonymous Key")

        # Assert
        assert isinstance(api_key.id, UUID)
        assert api_key.created_by is None
        assert api_key.name == "Anonymous Key"

    def test_create_api_key_sets_created_at(self, db: Session) -> None:
        """Should set created_at timestamp."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        api_key = api_key_service.create_api_key(db, developer.id, "Timestamped Key")

        # Assert
        assert api_key.created_at is not None
        # Verify timestamp is recent (within last minute)
        assert (datetime.now(timezone.utc) - api_key.created_at).total_seconds() < 60

    def test_create_api_key_generates_unique_keys(self, db: Session) -> None:
        """Should generate unique keys for each creation."""
        # Arrange
        developer = DeveloperFactory()

        # Act
        key1 = api_key_service.create_api_key(db, developer.id, "Key 1")
        key2 = api_key_service.create_api_key(db, developer.id, "Key 2")

        # Assert
        assert key1.id != key2.id


class TestApiKeyServiceGenerateSecret:
    """Test internal key generation method."""

    def test_generate_secret_format(self) -> None:
        # Act
        key = api_key_service._generate_secret()

        # Assert
        assert key.startswith("sk-")
        assert len(key) == 67
        hex_portion = key[3:]
        assert all(c in "0123456789abcdef" for c in hex_portion)

    def test_generate_secret_uniqueness(self) -> None:
        """Should generate unique keys on each call."""
        # Act
        keys = [api_key_service._generate_secret() for _ in range(100)]

        # Assert
        assert len(keys) == len(set(keys))  # All unique


class TestApiKeyServiceListApiKeys:
    """Test listing API keys."""

    def test_list_api_keys_ordered_by_created_at(self, db: Session) -> None:
        """Should list API keys ordered by creation date."""
        # Arrange
        developer = DeveloperFactory()
        now = datetime.now(timezone.utc)

        # Create keys at different times
        key1 = ApiKeyFactory(developer=developer, name="First", created_at=now - timedelta(days=2))
        key2 = ApiKeyFactory(developer=developer, name="Second", created_at=now - timedelta(days=1))
        key3 = ApiKeyFactory(developer=developer, name="Third", created_at=now)

        # Act
        keys = api_key_service.list_api_keys(db)

        # Assert
        assert len(keys) >= 3
        # Find our test keys in the result
        test_keys = [k for k in keys if k.id in [key1.id, key2.id, key3.id]]
        assert len(test_keys) == 3
        # Verify ordering - newest first based on actual implementation
        assert test_keys[0].id == key3.id
        assert test_keys[1].id == key2.id
        assert test_keys[2].id == key1.id

    def test_list_api_keys_empty(self, db: Session) -> None:
        """Should return empty list when no API keys exist."""
        # Act
        keys = api_key_service.list_api_keys(db)

        # Assert
        assert keys == []

    def test_list_api_keys_multiple_developers(self, db: Session) -> None:
        """Should list keys from all developers."""
        # Arrange
        dev1 = DeveloperFactory(email="dev1@example.com")
        dev2 = DeveloperFactory(email="dev2@example.com")

        key1 = ApiKeyFactory(developer=dev1)
        key2 = ApiKeyFactory(developer=dev2)

        # Act
        keys = api_key_service.list_api_keys(db)

        # Assert
        key_ids = [k.id for k in keys]
        assert key1.id in key_ids
        assert key2.id in key_ids


class TestApiKeyServiceRotateApiKey:
    """Test API key rotation."""

    def test_rotate_api_key_replaces_secret_in_place(self, db: Session) -> None:
        # Arrange
        developer = DeveloperFactory()
        old_key = ApiKeyFactory(developer=developer, name="Old Key")
        old_key_id = old_key.id

        # Act
        old_secret = api_key_secret(old_key)
        new_key = api_key_service.rotate_api_key(db, old_key_id)

        # Assert
        assert new_key.id == old_key_id
        assert len(new_key.secret) == 67
        assert new_key.created_by == developer.id

        with pytest.raises(HTTPException):
            api_key_service.validate_api_key(db, old_secret)
        assert api_key_service.validate_api_key(db, new_key.secret).id == old_key_id

    def test_rotate_api_key_with_none_creator(self, db: Session) -> None:
        """Should rotate key with None as creator."""
        # Arrange
        old_key = ApiKeyFactory(developer=None)
        old_key_id = old_key.id

        # Act
        new_key = api_key_service.rotate_api_key(db, old_key_id)

        # Assert
        assert new_key.id == old_key_id

    def test_rotate_nonexistent_key_raises_404(self, db: Session) -> None:
        """Should raise HTTPException(404) when rotating non-existent key."""
        # Arrange
        from fastapi import HTTPException

        fake_key = uuid4()

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            api_key_service.rotate_api_key(db, fake_key)

        assert exc_info.value.status_code == 404


class TestApiKeyServiceValidateApiKey:
    """Test API key validation."""

    def test_validate_api_key_existing_key(self, db: Session) -> None:
        """Should validate and return existing API key."""
        # Arrange
        developer = DeveloperFactory()
        api_key = ApiKeyFactory(developer=developer)

        # Act
        validated = api_key_service.validate_api_key(db, api_key_secret(api_key))

        # Assert
        assert validated.id == api_key.id
        assert validated.created_by == developer.id

    def test_validate_api_key_nonexistent_raises_401(self, db: Session) -> None:
        """Should raise 401 for non-existent key."""
        # Arrange
        fake_key = "definitely-invalid"

        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            api_key_service.validate_api_key(db, fake_key)

        assert exc_info.value.status_code == 401
        assert "Invalid or missing API key" in exc_info.value.detail

    def test_validate_api_key_empty_string_raises_401(self, db: Session) -> None:
        """Should raise 401 for empty key."""
        # Act & Assert
        with pytest.raises(HTTPException) as exc_info:
            api_key_service.validate_api_key(db, "")

        assert exc_info.value.status_code == 401


class TestApiKeyServiceGet:
    """Test getting API key by ID."""

    def test_get_existing_api_key(self, db: Session) -> None:
        """Should retrieve existing API key."""
        # Arrange
        developer = DeveloperFactory()
        api_key = ApiKeyFactory(developer=developer, name="Test Key")

        # Act
        retrieved = api_key_service.get(db, api_key.id)

        # Assert
        assert retrieved is not None
        assert retrieved.id == api_key.id
        assert retrieved.name == "Test Key"

    def test_get_nonexistent_api_key_returns_none(self, db: Session) -> None:
        """Should return None for non-existent key."""
        # Arrange
        fake_key = uuid4()

        # Act
        result = api_key_service.get(db, fake_key)

        # Assert
        assert result is None


class TestApiKeyServiceDelete:
    """Test API key deletion."""

    def test_delete_existing_api_key(self, db: Session) -> None:
        """Should delete existing API key."""
        # Arrange
        developer = DeveloperFactory()
        api_key = ApiKeyFactory(developer=developer)
        key_id = api_key.id

        # Act
        api_key_service.delete(db, key_id)

        # Assert
        result = api_key_service.get(db, key_id)
        assert result is None

    def test_delete_nonexistent_api_key(self, db: Session) -> None:
        """Should handle deleting non-existent key gracefully."""
        # Arrange
        fake_key = uuid4()

        # Act & Assert - should not raise error
        api_key_service.delete(db, fake_key, raise_404=False)
