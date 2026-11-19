import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import {
  authorizeConnection,
  type ConnectionOut,
  createIbkrConnection,
  createMonzoConnection,
  disableConnection,
  listConnections,
  syncConnectionNow,
  updateIbkrConnection,
  uploadIbkrStatement,
} from "../api";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { errorMessage } from "../lib/errors";

const POLL_MS = 5000;
// The statuses offered Authorise. The server also accepts `authorising`, but this page can't
// tell the wait for approval from the backfill after it, and authorising again during the
// backfill throws a completed approval away; an expired wait ends as needs_reauth.
const AUTHORISABLE = ["created", "needs_reauth", "disabled"];

const CALLBACK_ERRORS: Record<string, string> = {
  state: "The authorisation link expired or was already used. Click Authorise again.",
  exchange: "Monzo didn't accept the authorisation. Click Authorise to try again.",
  credentials:
    "This connection's client secret can't be read: it was stored under another master key. Add the connection again.",
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

const WARN_DAYS = 14;

// The server stores the start of the day after the token's last day (London); show that last day.
function tokenExpiry(credentialExpiresAt: string): { date: string; soon: boolean } {
  const end = new Date(credentialExpiresAt).getTime();
  const date = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/London" }).format(
    new Date(end - 1),
  );
  return { date, soon: end - Date.now() <= WARN_DAYS * 86400000 };
}

type IbkrCredentials = { token: string; token_expires_on: string; query_id?: string };

// Token, expiry and query id with the token cleared as soon as it is sent; the request
// variables are dropped from the mutation cache (gcTime 0 and reset) so it is never kept.
function useIbkrCredentialsForm(
  send: (body: IbkrCredentials) => Promise<unknown>,
  onDone: () => void,
  options: { doneMessage: string; queryIdOptional?: boolean },
) {
  const [token, setToken] = useState("");
  const [expiresOn, setExpiresOn] = useState("");
  const [queryId, setQueryId] = useState("");
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  const mutation = useMutation({
    gcTime: 0,
    mutationFn: send,
    onSuccess: () => {
      setDone(options.doneMessage);
      setExpiresOn("");
      setQueryId("");
      onDone();
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    setDone(undefined);
    const trimmedQueryId = queryId.trim();
    mutation.mutate(
      {
        token: token.trim(),
        token_expires_on: expiresOn,
        ...(trimmedQueryId ? { query_id: trimmedQueryId } : {}),
      },
      { onSettled: () => mutation.reset() },
    );
    setToken("");
  }
  const fields = (
    <>
      <TextField label="Token" type="password" value={token} onChange={setToken} required />
      <TextField
        label="Token expiry date"
        type="date"
        value={expiresOn}
        onChange={setExpiresOn}
        required
      />
      <TextField
        label="Query ID"
        value={queryId}
        onChange={setQueryId}
        required={!options.queryIdOptional}
      />
      {options.queryIdOptional && (
        <p className="text-sm text-muted-foreground sm:col-span-3">
          Leave empty to keep the current query
        </p>
      )}
    </>
  );
  return { fields, submit, error, done, pending: mutation.isPending };
}

function UpdateIbkrTokenForm({ connection }: { connection: ConnectionOut }) {
  const queryClient = useQueryClient();
  const form = useIbkrCredentialsForm(
    async (body) =>
      await updateIbkrConnection({
        path: { connection_id: connection.id },
        body,
        throwOnError: true,
      }),
    () => void queryClient.invalidateQueries({ queryKey: ["connections"] }),
    { doneMessage: "Token updated", queryIdOptional: true },
  );
  return (
    <form aria-label="Update token" onSubmit={form.submit} className="grid gap-4 sm:grid-cols-3">
      {form.fields}
      <div className="flex items-center gap-3 sm:col-span-3">
        <Button type="submit" variant="outline" disabled={form.pending}>
          Update token
        </Button>
        {form.error && <p className="text-sm text-destructive">{form.error}</p>}
        {form.done && <p role="status" className="text-sm">{form.done}</p>}
      </div>
    </form>
  );
}

function UploadFlexStatement({ connection }: { connection: ConnectionOut }) {
  const queryClient = useQueryClient();
  const id = useId();
  const [result, setResult] = useState<string>();
  const [error, setError] = useState<string>();
  const upload = useMutation({
    mutationFn: async (file: File) =>
      (
        await uploadIbkrStatement({
          path: { connection_id: connection.id },
          body: { file },
          throwOnError: true,
        })
      ).data,
    onSuccess: async (data, file) => {
      setError(undefined);
      setResult(`Uploaded ${file.name}: ${data.raw_records} records ingested.`);
      await queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (failure) => {
      setResult(undefined);
      setError(errorMessage(failure));
    },
  });
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>Upload a Flex statement</Label>
      <Input
        id={id}
        type="file"
        accept=".xml,text/xml"
        disabled={upload.isPending}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) upload.mutate(file);
          event.target.value = "";
        }}
      />
      <p className="text-sm text-muted-foreground">
        To add a closed account (such as a closed ISA), run your Flex query in Client Portal with
        closed accounts selected, then upload the XML here.
      </p>
      {result && <p role="status" className="text-sm">{result}</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
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
  const isIbkr = connection.connector === "ibkr";
  const expiry =
    isIbkr && connection.credential_expires_at
      ? tokenExpiry(connection.credential_expires_at)
      : undefined;
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
        {!isIbkr && connection.status === "authorising" && (
          <p role="status" className="rounded-md border border-primary p-3 font-medium">
            Approve the request in the Monzo app within 4 minutes of authorising: only then can
            Lararium fetch your full history. Once you approve, it fetches it, which can take a
            few minutes, and this page shows the result. If no request appeared, disable the
            connection and authorise it again.
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
        {expiry && (
          <p className={expiry.soon ? "text-sm font-medium text-amber-600" : "text-sm"}>
            Token expires on {expiry.date}
          </p>
        )}
        {isIbkr && connection.status === "needs_reauth" && (
          <p role="alert" className="text-sm text-destructive">
            IBKR rejected the token or query. Update them to resume syncing.
          </p>
        )}
        {!isIbkr && !connection.webhook_enabled && (
          <p className="text-sm text-muted-foreground">
            Webhooks are off, so new Monzo transactions arrive within 30 minutes.
          </p>
        )}
        {failed && <p className="text-sm text-destructive">{errorMessage(failed.error)}</p>}
        {isIbkr && <UpdateIbkrTokenForm connection={connection} />}
        {isIbkr && <UploadFlexStatement connection={connection} />}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={sync.isPending} onClick={() => sync.mutate()}>
            Sync now
          </Button>
          {!isIbkr && AUTHORISABLE.includes(connection.status) && (
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
  // Only the server's value is exact: it is built from LARARIUM_PUBLIC_BASE_URL, which need
  // not match the address this page was opened at. Until a connection carries it, say how
  // it is built instead of guessing.
  const callbackPath = "/api/v1/connections/oauth/monzo/callback";

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
            {redirectUri ? (
              <code>{redirectUri}</code>
            ) : (
              <>
                your <code>LARARIUM_PUBLIC_BASE_URL</code> followed by <code>{callbackPath}</code>
              </>
            )}
            .
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

function AddIbkrForm() {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const form = useIbkrCredentialsForm(
    async (credentials) =>
      await createIbkrConnection({
        body: {
          display_name: displayName.trim(),
          ...credentials,
          query_id: credentials.query_id ?? "",
        },
        throwOnError: true,
      }),
    () => {
      setDisplayName("");
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
    { doneMessage: "Interactive Brokers connected" },
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Interactive Brokers</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          aria-label="Add Interactive Brokers"
          onSubmit={form.submit}
          className="grid gap-4 sm:grid-cols-2"
        >
          <TextField label="Display name" value={displayName} onChange={setDisplayName} required />
          {form.fields}
          <p className="text-sm text-muted-foreground sm:col-span-2">
            Create an Activity Flex Query and a Flex Web Service token in IBKR's Client Portal; see
            the README for the exact fields.
          </p>
          <div className="flex items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={form.pending}>
              Add Interactive Brokers
            </Button>
            {form.error && <p className="text-sm text-destructive">{form.error}</p>}
            {form.done && <p role="status" className="text-sm">{form.done}</p>}
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
      <AddIbkrForm />
    </div>
  );
}
