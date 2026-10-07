import { useState } from 'react';
import { Copy, Key, Pencil, Plus, RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';
import {
  useApiKeys,
  useCreateApiKey,
  useDeleteApiKey,
  useRotateApiKey,
  useUpdateApiKey,
} from '@/hooks/api/use-api-keys';
import type { ApiKey } from '@/lib/api/types';
import { copyToClipboard } from '@/lib/utils/clipboard';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

export function ApiKeysSection() {
  const [isCreateDialogOpen, setIsCreateDialogOpen] = useState(false);
  const [oneTimeSecret, setOneTimeSecret] = useState<string | null>(null);
  const [keyName, setKeyName] = useState('');
  const [editingKey, setEditingKey] = useState<ApiKey | null>(null);
  const [renameKeyName, setRenameKeyName] = useState('');
  const [keyToDelete, setKeyToDelete] = useState<ApiKey | null>(null);
  const [keyToRotate, setKeyToRotate] = useState<ApiKey | null>(null);

  const { data: apiKeys, isLoading, error, refetch } = useApiKeys();
  const createMutation = useCreateApiKey();
  const deleteMutation = useDeleteApiKey();
  const updateMutation = useUpdateApiKey();
  const rotateMutation = useRotateApiKey();

  const handleCreate = async () => {
    if (!keyName.trim()) {
      toast.error('Please enter a key name');
      return;
    }

    const newKey = await createMutation.mutateAsync({ name: keyName });
    setIsCreateDialogOpen(false);
    setKeyName('');
    setOneTimeSecret(newKey.secret);
  };

  const handleRotateConfirm = async () => {
    if (!keyToRotate) return;
    const rotatedKey = await rotateMutation.mutateAsync(keyToRotate.id);
    setKeyToRotate(null);
    setOneTimeSecret(rotatedKey.secret);
  };

  const handleDeleteConfirm = async () => {
    if (!keyToDelete) return;
    await deleteMutation.mutateAsync(keyToDelete.id);
    setKeyToDelete(null);
  };

  const openRenameDialog = (key: ApiKey) => {
    setEditingKey(key);
    setRenameKeyName(key.name);
  };

  const handleRenameSubmit = async () => {
    if (!editingKey) return;
    if (!renameKeyName.trim()) {
      toast.error('Please enter a key name');
      return;
    }

    await updateMutation.mutateAsync({
      id: editingKey.id,
      data: { name: renameKeyName.trim() },
    });
    setEditingKey(null);
    setRenameKeyName('');
  };

  const formatDate = (dateString: string) => {
    return new Date(dateString).toLocaleDateString();
  };

  if (isLoading) {
    return (
      <div className="rounded-2xl border border-border/60 bg-gradient-to-br from-card/80 to-card/40 backdrop-blur-xl p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-10 bg-muted rounded-md w-full" />
          <div className="space-y-3">
            {[1, 2, 3].map((i) => (
              <div key={i} className="h-16 bg-muted/50 rounded-md" />
            ))}
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="rounded-2xl border border-border/60 bg-gradient-to-br from-card/80 to-card/40 backdrop-blur-xl p-12 text-center">
        <p className="text-muted-foreground mb-4">Failed to load API keys</p>
        <Button onClick={() => refetch()}>Retry</Button>
      </div>
    );
  }

  return (
    <>
      <div className="rounded-2xl border border-border/60 bg-gradient-to-br from-card/80 to-card/40 backdrop-blur-xl overflow-hidden">
        <div className="px-6 py-4 border-b border-border/60 flex items-center justify-between gap-4">
          <div>
            <h3 className="text-sm font-medium text-foreground">API Keys</h3>
            <p className="text-xs text-muted-foreground mt-1">
              Use these keys to authenticate API requests and embed widgets
            </p>
          </div>
          <Button
            size="sm"
            onClick={() => setIsCreateDialogOpen(true)}
            className="shrink-0"
          >
            <Plus className="h-4 w-4" />
            Create API Key
          </Button>
        </div>

        {apiKeys && apiKeys.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-border/60 text-left">
                  <th className="px-6 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">
                    Name
                  </th>
                  <th className="px-6 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">
                    Prefix
                  </th>
                  <th className="px-6 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider">
                    Created
                  </th>
                  <th className="px-6 py-3 text-xs font-medium text-muted-foreground uppercase tracking-wider text-right">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border/40">
                {apiKeys.map((key) => (
                  <tr
                    key={key.id}
                    className="hover:bg-muted/40 transition-colors"
                  >
                    <td className="px-6 py-4 text-sm font-medium text-foreground/90">
                      {key.name}
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex items-center gap-2">
                        <code className="font-mono text-xs bg-muted text-foreground/90 px-2 py-1 rounded">
                          {key.display_prefix}…
                        </code>
                      </div>
                    </td>
                    <td className="px-6 py-4 text-xs text-muted-foreground">
                      {formatDate(key.created_at)}
                    </td>
                    <td className="px-6 py-4">
                      <div className="flex justify-end items-center gap-2">
                        <Button
                          variant="outline"
                          size="icon"
                          aria-label="Rotate API key"
                          onClick={() => setKeyToRotate(key)}
                          disabled={rotateMutation.isPending}
                        >
                          <RefreshCw className="h-4 w-4" aria-hidden />
                        </Button>
                        <Button
                          variant="outline"
                          size="icon"
                          aria-label="Rename API key"
                          onClick={() => openRenameDialog(key)}
                        >
                          <Pencil className="h-4 w-4" aria-hidden />
                        </Button>
                        <Button
                          variant="destructive-outline"
                          size="icon"
                          onClick={() => setKeyToDelete(key)}
                          disabled={deleteMutation.isPending}
                          aria-label="Delete API key"
                        >
                          <Trash2 className="h-4 w-4" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="p-12 text-center">
            <Key className="h-12 w-12 text-muted-foreground/60 mx-auto mb-4" />
            <p className="text-muted-foreground mb-2">No API keys yet</p>
            <p className="text-sm text-muted-foreground mb-4">
              Create your first key to get started
            </p>
            <Button
              variant="outline"
              onClick={() => setIsCreateDialogOpen(true)}
              aria-label="Create API key"
            >
              <Plus className="h-4 w-4" />
              Create API Key
            </Button>
          </div>
        )}
      </div>

      <Dialog
        open={editingKey !== null}
        onOpenChange={(open) => {
          if (!open) {
            setEditingKey(null);
            setRenameKeyName('');
          }
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Rename API Key</DialogTitle>
            <DialogDescription>
              Update the display name for this API key
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="rename_key_name" className="text-foreground/90">
              Key Name
            </Label>
            <Input
              id="rename_key_name"
              type="text"
              placeholder="e.g., Production API Key"
              value={renameKeyName}
              onChange={(e) => setRenameKeyName(e.target.value)}
              className="bg-muted border-border"
            />
            <p className="text-[10px] text-muted-foreground/70">
              A descriptive name to identify this key
            </p>
          </div>
          <DialogFooter className="gap-3">
            <Button
              variant="outline"
              onClick={() => {
                setEditingKey(null);
                setRenameKeyName('');
              }}
              disabled={updateMutation.isPending}
              aria-label="Cancel rename"
            >
              Cancel
            </Button>
            <Button
              onClick={handleRenameSubmit}
              disabled={updateMutation.isPending}
              aria-label="Save changes"
            >
              {updateMutation.isPending ? 'Saving...' : 'Save Changes'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={isCreateDialogOpen} onOpenChange={setIsCreateDialogOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Create New API Key</DialogTitle>
            <DialogDescription>
              Generate a new API key for your application
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="key_name" className="text-foreground/90">
              Key Name
            </Label>
            <Input
              id="key_name"
              type="text"
              placeholder="e.g., Production API Key"
              value={keyName}
              onChange={(e) => setKeyName(e.target.value)}
              className="bg-muted border-border"
            />
            <p className="text-[10px] text-muted-foreground/70">
              A descriptive name to identify this key
            </p>
          </div>
          <DialogFooter className="gap-3">
            <Button
              variant="outline"
              onClick={() => {
                setIsCreateDialogOpen(false);
                setKeyName('');
              }}
              disabled={createMutation.isPending}
              aria-label="Cancel create"
            >
              Cancel
            </Button>
            <Button
              onClick={handleCreate}
              disabled={createMutation.isPending}
              aria-label="Create key"
            >
              {createMutation.isPending ? 'Creating...' : 'Create Key'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={oneTimeSecret !== null}
        onOpenChange={(open) => {
          if (!open) setOneTimeSecret(null);
        }}
      >
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Save your API key</DialogTitle>
            <DialogDescription>
              This secret cannot be retrieved again after you close this dialog.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <code className="block break-all rounded bg-muted p-3 font-mono text-xs">
              {oneTimeSecret}
            </code>
            <Button
              variant="outline"
              onClick={() => {
                if (oneTimeSecret) {
                  copyToClipboard(oneTimeSecret, 'API key copied');
                }
              }}
              aria-label="Copy API key secret"
            >
              <Copy className="h-4 w-4" />
              Copy secret
            </Button>
          </div>
          <DialogFooter className="gap-3">
            <Button onClick={() => setOneTimeSecret(null)}>I saved it</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={keyToRotate !== null}
        onOpenChange={(open) => {
          if (!open) setKeyToRotate(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Rotate API Key</AlertDialogTitle>
            <AlertDialogDescription>
              {keyToRotate
                ? `Rotate "${keyToRotate.name}"? Its current secret will stop working immediately.`
                : 'The current secret will stop working immediately.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={rotateMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleRotateConfirm}
              disabled={rotateMutation.isPending}
              aria-label="Confirm rotate API key"
            >
              {rotateMutation.isPending ? 'Rotating...' : 'Rotate key'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        open={keyToDelete !== null}
        onOpenChange={(open) => {
          if (!open) setKeyToDelete(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete API Key</AlertDialogTitle>
            <AlertDialogDescription>
              {keyToDelete
                ? `Are you sure you want to delete "${keyToDelete.name}"? This action cannot be undone.`
                : 'Are you sure you want to delete this API key? This action cannot be undone.'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDeleteConfirm}
              disabled={deleteMutation.isPending}
              aria-label="Delete key"
            >
              {deleteMutation.isPending ? 'Deleting...' : 'Delete'}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
