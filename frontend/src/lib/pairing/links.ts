export const PAIRING_LINK_INVALID =
	'This pairing link is invalid or has expired. Ask whoever sent it for a new one.';

/**
 * The page an end user opens to connect a provider; lives on this frontend. Its
 * token is what lets that page start an OAuth flow, and only for this user.
 */
export function pairingPath(userId: string, token?: string | null): string {
	const path = `/users/${userId}/pair`;
	return token ? `${path}?${new URLSearchParams({ token })}` : path;
}

export function pairingLink(origin: string, userId: string, token: string): string {
	return `${origin.replace(/\/+$/, '')}${pairingPath(userId, token)}`;
}

/** The pairing link is public, so its query string is attacker-controlled. */
export function safeReturnUrl(raw: string | null): string | null {
	if (!raw) return null;

	try {
		const url = new URL(raw);
		return url.protocol === 'http:' || url.protocol === 'https:' ? url.toString() : null;
	} catch {
		return null;
	}
}

export function pairingSuccessUrl(
	origin: string,
	userId: string,
	provider: string,
	returnUrl: string | null,
	token: string
): string {
	const target = new URL(`${pairingPath(userId)}/success`, origin);
	target.searchParams.set('provider', provider);
	target.searchParams.set('token', token);

	const back = safeReturnUrl(returnUrl);
	if (back) target.searchParams.set('redirect_url', back);

	return target.toString();
}
