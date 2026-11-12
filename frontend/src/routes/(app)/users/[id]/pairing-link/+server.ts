import { json } from '@sveltejs/kit';
import { requireToken } from '$lib/server/guard';
import { createPairingToken } from '$lib/server/users';
import { pairingLink } from '$lib/pairing/links';
import type { RequestHandler } from './$types';

/** A fresh link per copy: its token is what lets the public pairing page start OAuth. */
export const POST: RequestHandler = async ({ params, locals, url }) => {
	const accessToken = await requireToken(locals);
	const { token } = await createPairingToken(params.id, accessToken);

	return json({ url: pairingLink(url.origin, params.id, token) });
};
