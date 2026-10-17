import * as React from "react";
import { Auth, errorText, ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { parseHash } from "@/lib/route";

export function Login({ message, onDone }: { message: string; onDone: () => void }) {
  const [account, setAccount] = React.useState("");
  const [credential, setCredential] = React.useState("");
  const [error, setError] = React.useState(message);
  const [pending, setPending] = React.useState(false);
  const hasLink = parseHash(location.hash).ws !== null;

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (pending) return;
    setPending(true);
    setError("");
    try {
      await Auth.login(account, credential);
      setCredential("");
      onDone();
    } catch (e) {
      setCredential("");
      setError(e instanceof ApiError && e.status === 401 ? "Sign-in failed: the account or credential was not accepted." : errorText(e));
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="min-h-full grid place-items-center p-4">
      <form onSubmit={submit} aria-labelledby="login-h" className="w-full max-w-sm bg-surface border border-border rounded-lg p-6 grid gap-4" data-testid="login-form">
        <h1 id="login-h" className="text-2xl font-semibold">Sign in to Ripple</h1>
        {hasLink && (
          <p className="text-sm text-muted" data-testid="login-context">
            Sign in with the account that should act on this link. You will come back to the same place afterwards.
          </p>
        )}
        <label className="grid gap-1 text-sm font-medium">
          Account ID
          <input className="border border-border rounded-sm bg-bg px-3 py-2 text-base font-normal" name="account" autoFocus autoComplete="username" required value={account} onChange={(e) => setAccount(e.target.value)} data-testid="login-account" />
        </label>
        <label className="grid gap-1 text-sm font-medium">
          Credential
          <input className="border border-border rounded-sm bg-bg px-3 py-2 text-base font-normal" name="credential" type="password" autoComplete="current-password" required value={credential} onChange={(e) => setCredential(e.target.value)} data-testid="login-credential" />
        </label>
        <Button type="submit" variant="primary" disabled={pending} data-testid="login-submit">{pending ? "Signing in..." : "Sign in"}</Button>
        {error && <p role="alert" className="text-sm text-danger" data-testid="login-error">{error}</p>}
      </form>
    </main>
  );
}
