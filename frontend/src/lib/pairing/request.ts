export async function requestPairingLink(userId: string): Promise<string> {
	const response = await fetch(`/users/${userId}/pairing-link`, { method: 'POST' });
	if (!response.ok) throw new Error(`Pairing link request failed: ${response.status}`);
	return (await response.json()).url;
}
