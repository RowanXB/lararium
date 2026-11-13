import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import {
  authorizeConnection,
  type ConnectionOut,
  createMonzoConnection,
  disableConnection,
  listConnections,
  syncConnectionNow,
} from "../api";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { errorMessage } from "../lib/errors";

const POLL_MS = 5000;
// The statuses the server accepts for an authorise request; anything else gets a 409.
const AUTHORISABLE = ["created", "needs_reauth", "authorising", "disabled"];

const CALLBACK_ERRORS: Record<string, string> = {
  state: "The authorisation link expired or was already used. Click Authorise again.",
  exchange: "Monzo didn't accept the authorisation. Click Authorise to try again.",
  status:
    "This connection can't be authorised right now. Disable it first, or wait until it needs re-authorisation.",
};

function relativeTime(iso: string): string {
  const seconds = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ["day", 86400],
    ["hour", 3600],
    ["minute", 60],
  ];
  const formatter = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) return formatter.format(Math.round(seconds / size), unit);
  }
  return formatter.format(seconds, "second");
}

function ConnectionCard({ connection }: { connection: ConnectionOut }) {
  const queryClient = useQueryClient();
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["connections"] });
  const path = { connection_id: connection.id };

  const sync = useMutation({
    mutationFn: async () => await syncConnectionNow({ path, throwOnError: true }),
    onSuccess: refresh,
  });
  const disable = useMutation({
    mutationFn: async () => await disableConnection({ path, throwOnError: true }),
    onSuccess: refresh,
  });
  const authorise = useMutation({
    mutationFn: async () => (await authorizeConnection({ path, throwOnError: true })).data,
    onSuccess: (data) => {
      window.location.href = data.url;
    },
  });
  const failed = [sync, disable, authorise].find((mutation) => mutation.isError);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-3">
          {connection.display_name}
          <span className="rounded-full border px-2 py-0.5 text-xs font-normal">
            {connection.status}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {connection.status === "authorising" && (
          <p role="status" className="rounded-md border border-primary p-3 font-medium">
            Open the Monzo app and approve the request now. Lararium has 4 minutes to fetch your
            full history.
          </p>
        )}
        <p className="text-sm text-muted-foreground">
          Last successful sync:{" "}
          {connection.last_success_at ? (
            <time
              dateTime={connection.last_success_at}
              title={new Date(connection.last_success_at).toLocaleString()}
            >
              {relativeTime(connection.last_success_at)}
            </time>
          ) : (
            "never"
          )}
        </p>
        {connection.last_error && (
          <p className="text-sm text-destructive">Last error: {connection.last_error}</p>
        )}
        {connection.warnings.map((warning) => (
          <p key={warning} className="text-sm text-amber-600">
            {warning}
          </p>
        ))}
        {!connection.webhook_enabled && (
          <p className="text-sm text-muted-foreground">
            Webhooks are off, so new Monzo transactions arrive within 30 minutes.
          </p>
        )}
        {failed && <p className="text-sm text-destructive">{errorMessage(failed.error)}</p>}
        <div className="flex gap-2">
          <Button variant="outline" disabled={sync.isPending} onClick={() => sync.mutate()}>
            Sync now
          </Button>
          {AUTHORISABLE.includes(connection.status) && (
            <Button disabled={authorise.isPending} onClick={() => authorise.mutate()}>
              Authorise
            </Button>
          )}
          <Button variant="outline" disabled={disable.isPending} onClick={() => disable.mutate()}>
            Disable
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function AddMonzoForm({ redirectUri }: { redirectUri: string | undefined }) {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [error, setError] = useState<string>();
  const callbackUrl =
    redirectUri ?? `${window.location.origin}/api/v1/connections/oauth/monzo/callback`;

  const mutation = useMutation({
    // gcTime 0 plus the reset below drops the request variables (the secret) from the cache.
    gcTime: 0,
    mutationFn: async (body: { display_name: string; client_id: string; client_secret: string }) =>
      (await createMonzoConnection({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setDisplayName("");
      setClientId("");
      await queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (failure) => setError(errorMessage(failure)),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    mutation.mutate(
      {
        display_name: displayName.trim(),
        client_id: clientId.trim(),
        client_secret: clientSecret,
      },
      { onSettled: () => mutation.reset() },
    );
    // The secret only lives in the request; never keep it in state afterwards.
    setClientSecret("");
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Monzo</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Add Monzo" onSubmit={submit} className="grid gap-4 sm:grid-cols-3">
          <TextField label="Display name" value={displayName} onChange={setDisplayName} required />
          <TextField label="Client ID" value={clientId} onChange={setClientId} required />
          <TextField
            label="Client secret"
            type="password"
            value={clientSecret}
            onChange={setClientSecret}
            required
          />
          <p className="text-sm text-muted-foreground sm:col-span-3">
            Create an OAuth client at developers.monzo.com and set its redirect URL to{" "}
            <code>{callbackUrl}</code>.
          </p>
          <div className="flex items-center gap-3 sm:col-span-3">
            <Button type="submit" disabled={mutation.isPending}>
              Add Monzo
            </Button>
            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

export function ConnectionsPage() {
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: async () => (await listConnections({ throwOnError: true })).data,
    refetchInterval: (query) =>
      query.state.data?.some((connection) => connection.status === "authorising")
        ? POLL_MS
        : false,
  });
  const callbackError = new URLSearchParams(window.location.search).get("error");
  const banner = callbackError ? CALLBACK_ERRORS[callbackError] : undefined;
  const list = connections.data ?? [];

  return (
    <div className="space-y-6">
      {banner && (
        <p role="alert" className="rounded-md border border-destructive p-3 text-sm">
          {banner}
        </p>
      )}
      {connections.error && (
        <p className="text-sm text-destructive">{errorMessage(connections.error)}</p>
      )}
      {list.map((connection) => (
        <ConnectionCard key={connection.id} connection={connection} />
      ))}
      <AddMonzoForm
        redirectUri={list.find((connection) => connection.redirect_uri)?.redirect_uri ?? undefined}
      />
    </div>
  );
}
