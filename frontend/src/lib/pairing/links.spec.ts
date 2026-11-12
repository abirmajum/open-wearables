import { describe, expect, it } from 'vitest';
import { pairingLink, pairingSuccessUrl, safeReturnUrl } from './links';

describe('pairingLink', () => {
	it('builds an absolute link an admin can paste into a message', () => {
		expect(pairingLink('https://app.example.com', 'abc-123', 'tok.en')).toBe(
			'https://app.example.com/users/abc-123/pair?token=tok.en'
		);
	});

	// origin from a proxy or config sometimes arrives with a trailing slash.
	it('does not double the slash', () => {
		expect(pairingLink('https://app.example.com/', 'abc-123', 'tok.en')).toBe(
			'https://app.example.com/users/abc-123/pair?token=tok.en'
		);
	});
});

describe('pairingSuccessUrl', () => {
	// "Connect another device" on the success page needs the token again.
	it('keeps the token for the next provider', () => {
		expect(pairingSuccessUrl('https://app.example.com', 'abc-123', 'oura', null, 'tok.en')).toBe(
			'https://app.example.com/users/abc-123/pair/success?provider=oura&token=tok.en'
		);
	});
});

describe('safeReturnUrl', () => {
	it('passes an ordinary web address through', () => {
		expect(safeReturnUrl('https://client.example.com/done')).toBe(
			'https://client.example.com/done'
		);
	});

	// The pairing link is public, so its query string is attacker-controlled.
	it.each(['javascript:alert(1)', 'data:text/html,<script>', 'not a url', ''])(
		'refuses %s',
		(raw) => {
			expect(safeReturnUrl(raw)).toBeNull();
		}
	);

	it('is null when the link carries none', () => {
		expect(safeReturnUrl(null)).toBeNull();
	});
});
