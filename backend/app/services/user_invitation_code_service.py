import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from app.config import settings
from app.database import DbSession
from app.integrations import rate_limiter
from app.models.user_invitation_code import UserInvitationCode
from app.repositories.user_invitation_code_repository import UserInvitationCodeRepository
from app.schemas.auth import SDKTokenOrigin
from app.schemas.model_crud.credentials import (
    InvitationCodeRedeemResponse,
    UserInvitationCodeCreate,
    UserInvitationCodeRead,
)
from app.services.refresh_token_service import refresh_token_service
from app.services.user_service import user_service
from app.utils.exceptions import NotFoundError, TooManyRequestsError

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
_REDEEM_RATE_KEY = "rate_limit:invitation_code_redeem:"


class UserInvitationCodeService:
    def __init__(self) -> None:
        self.crud = UserInvitationCodeRepository(UserInvitationCode)

    def _generate_code(self) -> str:
        """Generate an 8-character alphanumeric code from unambiguous charset."""
        return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))

    def generate(
        self,
        db_session: DbSession,
        user_id: UUID,
        developer_id: UUID,
    ) -> UserInvitationCodeRead:
        """Generate a new invitation code for a user."""
        user_service.get(db_session, user_id, raise_404=True)
        self.crud.revoke_active_for_user(db_session, user_id)

        now = datetime.now(timezone.utc)
        code_data = UserInvitationCodeCreate(
            id=uuid4(),
            code=self._generate_code(),
            user_id=user_id,
            created_by_id=developer_id,
            expires_at=now + timedelta(days=settings.user_invitation_code_expire_days),
            created_at=now,
        )
        row = self.crud.create(db_session, code_data)
        return UserInvitationCodeRead.model_validate(row)

    def enforce_redeem_rate_limit(self, client_ip: str | None) -> None:
        """Cap redeem attempts per client IP, then across all clients, so codes cannot be brute-forced.

        The IP is checked first, so a client that is already blocked does not use up the global budget.
        """
        window = settings.user_invitation_code_redeem_window_seconds
        ip_count = rate_limiter.hit(f"{_REDEEM_RATE_KEY}ip:{client_ip or 'unknown'}", window)
        if ip_count > settings.user_invitation_code_redeem_ip_limit:
            raise TooManyRequestsError("Too many invitation code attempts, try again later")
        if rate_limiter.hit(f"{_REDEEM_RATE_KEY}global", window) > settings.user_invitation_code_redeem_global_limit:
            raise TooManyRequestsError("Too many invitation code attempts, try again later")

    def redeem(self, db_session: DbSession, code: str) -> InvitationCodeRedeemResponse:
        """Redeem an invitation code and return SDK tokens."""
        invitation_code = self.crud.claim_valid_code(db_session, code.upper())

        if not invitation_code:
            raise NotFoundError("Invalid or expired invitation code")

        # Storing the new refresh token commits the claim with it, so a failure before that
        # leaves the code usable
        access_token, refresh_token = refresh_token_service.issue_sdk_tokens(
            db_session,
            user_id=invitation_code.user_id,
            origin=SDKTokenOrigin.INVITE,
            issued_by_developer_id=invitation_code.created_by_id,
        )

        return InvitationCodeRedeemResponse(
            access_token=access_token,
            token_type="bearer",
            refresh_token=refresh_token,
            expires_in=settings.access_token_expire_minutes * 60,
            user_id=invitation_code.user_id,
        )


user_invitation_code_service = UserInvitationCodeService()
