// @vitest-environment jsdom

import type { ButtonHTMLAttributes, ReactNode } from 'react';
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiKeysSection } from './-api-keys-section';

const mocks = vi.hoisted(() => ({
  copy: vi.fn(),
  create: vi.fn(),
  rotate: vi.fn(),
  update: vi.fn(),
  remove: vi.fn(),
}));

vi.mock('@/lib/utils/clipboard', () => ({ copyToClipboard: mocks.copy }));

vi.mock('@/hooks/api/use-api-keys', () => ({
  useApiKeys: () => ({
    data: [
      {
        id: '00000000-0000-4000-8000-000000000001',
        display_prefix: 'sk-listonly',
        name: 'Production',
        created_by: null,
        created_at: '2026-09-04T00:00:00Z',
      },
    ],
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  }),
  useCreateApiKey: () => ({ mutateAsync: mocks.create, isPending: false }),
  useRotateApiKey: () => ({ mutateAsync: mocks.rotate, isPending: false }),
  useUpdateApiKey: () => ({ mutateAsync: mocks.update, isPending: false }),
  useDeleteApiKey: () => ({ mutateAsync: mocks.remove, isPending: false }),
}));

vi.mock('@/components/ui/dialog', () => ({
  Dialog: ({
    children,
    onOpenChange,
    open,
  }: {
    children: ReactNode;
    onOpenChange: (open: boolean) => void;
    open: boolean;
  }) =>
    open ? (
      <section role="dialog">
        {children}
        <button aria-label="Close" onClick={() => onOpenChange(false)}>
          Close
        </button>
      </section>
    ) : null,
  DialogContent: ({ children }: { children: ReactNode }) => children,
  DialogDescription: ({ children }: { children: ReactNode }) => children,
  DialogFooter: ({ children }: { children: ReactNode }) => children,
  DialogHeader: ({ children }: { children: ReactNode }) => children,
  DialogTitle: ({ children }: { children: ReactNode }) => children,
}));

vi.mock('@/components/ui/alert-dialog', () => ({
  AlertDialog: ({
    children,
    open,
  }: {
    children: ReactNode;
    onOpenChange: (open: boolean) => void;
    open: boolean;
  }) => (open ? <section role="alertdialog">{children}</section> : null),
  AlertDialogAction: (props: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props} />
  ),
  AlertDialogCancel: (props: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button {...props} />
  ),
  AlertDialogContent: ({ children }: { children: ReactNode }) => children,
  AlertDialogDescription: ({ children }: { children: ReactNode }) => children,
  AlertDialogFooter: ({ children }: { children: ReactNode }) => children,
  AlertDialogHeader: ({ children }: { children: ReactNode }) => children,
  AlertDialogTitle: ({ children }: { children: ReactNode }) => children,
}));

afterEach(cleanup);

describe('ApiKeysSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.create.mockResolvedValue({ secret: 'generated-create-secret' });
    mocks.rotate.mockResolvedValue({ secret: 'generated-rotate-secret' });
  });

  it('shows only the non-secret prefix in list rows', () => {
    render(<ApiKeysSection />);

    expect(screen.getByText('sk-listonly…')).toBeTruthy();
    expect(
      screen.queryByText('00000000-0000-4000-8000-000000000001')
    ).toBeNull();
    expect(screen.queryByLabelText('Copy key ID')).toBeNull();
  });

  it('reveals and copies a newly created secret once', async () => {
    render(<ApiKeysSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Create API Key' }));
    fireEvent.change(screen.getByLabelText('Key Name'), {
      target: { value: 'CLI' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create key' }));

    expect(await screen.findByText('generated-create-secret')).toBeTruthy();
    expect(screen.getByText(/cannot be retrieved again/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull();
    fireEvent.click(
      screen.getByRole('button', { name: 'Copy API key secret' })
    );
    expect(mocks.copy).toHaveBeenCalledWith(
      'generated-create-secret',
      'API key copied'
    );
  });

  it('requires confirmation before rotating and reveals the new secret', async () => {
    render(<ApiKeysSection />);

    fireEvent.click(screen.getByRole('button', { name: 'Rotate API key' }));
    expect(mocks.rotate).not.toHaveBeenCalled();
    expect(screen.getByText(/Rotate "Production"/)).toBeTruthy();
    expect(screen.getByText(/stop working immediately/i)).toBeTruthy();

    fireEvent.click(
      screen.getByRole('button', { name: 'Confirm rotate API key' })
    );

    expect(await screen.findByText('generated-rotate-secret')).toBeTruthy();
    expect(mocks.rotate).toHaveBeenCalledTimes(1);
    expect(mocks.rotate).toHaveBeenCalledWith(
      '00000000-0000-4000-8000-000000000001'
    );
  });

  it('clears secret state for every dialog close action', async () => {
    render(<ApiKeysSection />);

    for (const closeButton of ['Close', 'I saved it']) {
      fireEvent.click(screen.getByRole('button', { name: 'Rotate API key' }));
      fireEvent.click(
        screen.getByRole('button', { name: 'Confirm rotate API key' })
      );
      await screen.findByText('generated-rotate-secret');
      fireEvent.click(screen.getByRole('button', { name: closeButton }));
      await waitFor(() => {
        expect(screen.queryByText('generated-rotate-secret')).toBeNull();
      });
    }
  });
});
