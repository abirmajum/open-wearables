import { apiGet } from './api';

type AuthorizationUrl = { authorization_url: string; state: string };

/** `pairingToken` comes from the pairing link; the API accepts it for that user only. */
export function authorizeProvider(
	provider: string,
	userId: string,
	redirectUri: string,
	pairingToken: string
) {
	const params = new URLSearchParams({ user_id: userId, redirect_uri: redirectUri });
	return apiGet<AuthorizationUrl>(`/api/v1/oauth/${provider}/authorize?${params}`, pairingToken);
}
