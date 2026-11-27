import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useEffect, useId, useState } from "react";

import {
  type AccountTypeOut,
  authorizeConnection,
  type BinanceConnectionCreateWritable,
  type BitcoinPreviewInWritable,
  type BitcoinPreviewOut,
  type BitcoinStateOut,
  type BitcoinWalletCreateWritable,
  type ConnectionOut,
  createBinanceConnection,
  createBitcoinConnection,
  createEtoroConnection,
  createIbkrConnection,
  createMonzoConnection,
  createTrading212Connection,
  disableConnection,
  listAccounts,
  listConnections,
  previewBitcoinWallet,
  syncConnectionNow,
  updateBinanceConnection,
  updateEtoroConnection,
  updateIbkrConnection,
  updateTrading212Connection,
  uploadEtoroStatement,
  uploadIbkrStatement,
  uploadTrading212Export,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
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

// A connector's two keys: the body field each is sent as, and its label.
type KeyFields<K extends string> = readonly [[K, string], [K, string]];

const TRADING212_KEYS = [
  ["api_key", "API key"],
  ["api_secret", "API secret"],
] as const satisfies KeyFields<string>;
const ETORO_KEYS = [
  ["public_api_key", "Public API Key"],
  ["user_key", "User Key"],
] as const satisfies KeyFields<string>;

// Two keys as password fields, both cleared as soon as they are sent; the request variables
// are dropped from the mutation cache (gcTime 0 and reset) so they are never kept.
function useKeysForm<K extends string>(
  keys: KeyFields<K>,
  send: (body: Record<K, string>) => Promise<unknown>,
  onDone: () => void,
  doneMessage: string,
) {
  const [[firstName, firstLabel], [secondName, secondLabel]] = keys;
  const [first, setFirst] = useState("");
  const [second, setSecond] = useState("");
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  const mutation = useMutation({
    gcTime: 0,
    mutationFn: send,
    onSuccess: () => {
      setDone(doneMessage);
      onDone();
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    setDone(undefined);
    const body = { [firstName]: first.trim(), [secondName]: second.trim() } as Record<K, string>;
    mutation.mutate(body, { onSettled: () => mutation.reset() });
    setFirst("");
    setSecond("");
  }
  const fields = (
    <>
      <TextField label={firstLabel} type="password" value={first} onChange={setFirst} required />
      <TextField
        label={secondLabel}
        type="password"
        value={second}
        onChange={setSecond}
        required
      />
    </>
  );
  return { fields, submit, error, done, pending: mutation.isPending };
}

function UpdateTrading212KeysForm({ connection }: { connection: ConnectionOut }) {
  const queryClient = useQueryClient();
  const form = useKeysForm(
    TRADING212_KEYS,
    async (body) =>
      await updateTrading212Connection({
        path: { connection_id: connection.id },
        body,
        throwOnError: true,
      }),
    () => void queryClient.invalidateQueries({ queryKey: ["connections"] }),
    "Keys updated",
  );
  return <UpdateKeysForm form={form} />;
}

function UpdateEtoroKeysForm({ connection }: { connection: ConnectionOut }) {
  const queryClient = useQueryClient();
  const form = useKeysForm(
    ETORO_KEYS,
    async (body) =>
      await updateEtoroConnection({
        path: { connection_id: connection.id },
        body,
        throwOnError: true,
      }),
    () => void queryClient.invalidateQueries({ queryKey: ["connections"] }),
    "Keys updated",
  );
  return <UpdateKeysForm form={form} />;
}

function UpdateKeysForm({ form }: { form: ReturnType<typeof useKeysForm> }) {
  return (
    <form aria-label="Update keys" onSubmit={form.submit} className="grid gap-4 sm:grid-cols-2">
      {form.fields}
      <div className="flex items-center gap-3 sm:col-span-2">
        <Button type="submit" variant="outline" disabled={form.pending}>
          Update keys
        </Button>
        {form.error && <p className="text-sm text-destructive">{form.error}</p>}
        {form.done && <p role="status" className="text-sm">{form.done}</p>}
      </div>
    </form>
  );
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
  return (
    <UploadStatement
      label="Upload a Flex statement"
      accept=".xml,text/xml"
      help="To add a closed account (such as a closed ISA), run your Flex query in Client Portal with closed accounts selected, then upload the XML here."
      send={async (file) =>
        (
          await uploadIbkrStatement({
            path: { connection_id: connection.id },
            body: { file },
            throwOnError: true,
          })
        ).data
      }
    />
  );
}

function UploadEtoroStatement({ connection }: { connection: ConnectionOut }) {
  return (
    <UploadStatement
      label="Upload eToro statement (xlsx)"
      accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
      help="Download it from eToro's web app: Portfolio → History → Account Statement, as Excel. The first one must start on or before the day you opened the account."
      send={async (file) =>
        (
          await uploadEtoroStatement({
            path: { connection_id: connection.id },
            body: { file },
            throwOnError: true,
          })
        ).data
      }
    />
  );
}

function UploadTrading212Export({ connection }: { connection: ConnectionOut }) {
  return (
    <UploadStatement
      label="Upload Trading 212 export (CSV, optional)"
      accept=".csv,text/csv"
      help="Optional: adds details the API doesn't give. Export it in the Trading 212 app under History."
      send={async (file) =>
        (
          await uploadTrading212Export({
            path: { connection_id: connection.id },
            body: { file },
            throwOnError: true,
          })
        ).data
      }
    />
  );
}

function UploadStatement({
  label,
  accept,
  help,
  send,
}: {
  label: string;
  accept: string;
  help: string;
  send: (file: File) => Promise<{ raw_records: number }>;
}) {
  const queryClient = useQueryClient();
  const id = useId();
  const [result, setResult] = useState<string>();
  const [error, setError] = useState<string>();
  const upload = useMutation({
    mutationFn: send,
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
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="file"
        accept={accept}
        disabled={upload.isPending}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) upload.mutate(file);
          event.target.value = "";
        }}
      />
      <p className="text-sm text-muted-foreground">{help}</p>
      {result && <p role="status" className="text-sm">{result}</p>}
      {error && <p className="text-sm text-destructive">{error}</p>}
    </div>
  );
}

type BinanceKeyType = BinanceConnectionCreateWritable["key_type"];
type BinanceKey = Omit<BinanceConnectionCreateWritable, "display_name">;

const BINANCE_KEY_TYPES: { value: BinanceKeyType; label: string }[] = [
  { value: "hmac", label: "HMAC" },
  { value: "ed25519", label: "Ed25519" },
];

// A PEM key has line breaks that a password input would strip, so it is a textarea that
// masks its text instead.
function PrivateKeyField({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const id = useId();
  return (
    <div className="grid gap-1.5">
      <Label htmlFor={id}>Private key</Label>
      <textarea
        id={id}
        value={value}
        required
        rows={3}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
        style={{ WebkitTextSecurity: "disc" } as React.CSSProperties}
        className="w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base outline-none md:text-sm"
      />
    </div>
  );
}

// Every secret is cleared as soon as it is sent, whether the request then succeeds or fails;
// the request variables are dropped from the mutation cache (gcTime 0 and reset).
function useBinanceKeyForm(
  send: (key: BinanceKey) => Promise<unknown>,
  onDone: () => void,
  doneMessage: string,
) {
  const [keyType, setKeyType] = useState<BinanceKeyType>("hmac");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [privateKey, setPrivateKey] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();
  const mutation = useMutation({
    gcTime: 0,
    mutationFn: send,
    onSuccess: () => {
      setDone(doneMessage);
      onDone();
    },
    onError: (failure) => setError(errorMessage(failure)),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    setDone(undefined);
    mutation.mutate(
      keyType === "hmac"
        ? { key_type: keyType, api_key: apiKey.trim(), api_secret: apiSecret.trim() }
        : {
            key_type: keyType,
            api_key: apiKey.trim(),
            private_key: privateKey,
            ...(passphrase ? { private_key_passphrase: passphrase } : {}),
          },
      { onSettled: () => mutation.reset() },
    );
    setApiKey("");
    setApiSecret("");
    setPrivateKey("");
    setPassphrase("");
  }
  const fields = (
    <>
      <LabelledSelect
        label="Key type"
        value={keyType}
        options={BINANCE_KEY_TYPES}
        onChange={setKeyType}
      />
      <TextField
        label="API key"
        type="password"
        autoComplete="off"
        value={apiKey}
        onChange={setApiKey}
        required
      />
      {keyType === "hmac" ? (
        <TextField
          label="Secret key"
          type="password"
          autoComplete="off"
          value={apiSecret}
          onChange={setApiSecret}
          required
        />
      ) : (
        <>
          <PrivateKeyField value={privateKey} onChange={setPrivateKey} />
          <TextField
            label="Passphrase (optional)"
            type="password"
            autoComplete="new-password"
            value={passphrase}
            onChange={setPassphrase}
          />
        </>
      )}
    </>
  );
  return { fields, submit, error, done, pending: mutation.isPending };
}

function formatUtc(iso: string): string {
  return new Date(iso).toISOString().slice(0, 16).replace("T", " ");
}

function BinanceState({
  state,
  accountNames,
}: {
  state: NonNullable<ConnectionOut["binance"]>;
  accountNames: Map<string, string>;
}) {
  return (
    <div className="space-y-2">
      {state.wallets.length > 0 && (
        <p className="text-sm">Wallets: {state.wallets.join(", ")}</p>
      )}
      {!state.first_sync_done && (
        <p role="status" className="text-sm font-medium">
          First sync in progress
        </p>
      )}
      {state.differences.length > 0 && (
        <ul className="space-y-1 text-sm text-amber-600">
          {state.differences.map((difference) => (
            <li key={`${difference.account_id}/${difference.symbol}/${difference.from_at}`}>
              {`${accountNames.get(difference.account_id) ?? "Binance account"}: ${difference.symbol}, ${formatUtc(difference.from_at)} to ${formatUtc(difference.to_at)} (UTC)`}
            </li>
          ))}
        </ul>
      )}
      {state.flags.map((flag) => (
        <p key={flag} className="text-sm text-amber-600">
          {flag}
        </p>
      ))}
    </div>
  );
}

function UpdateBinanceKeyForm({ connection }: { connection: ConnectionOut }) {
  const queryClient = useQueryClient();
  const form = useBinanceKeyForm(
    async (body) =>
      await updateBinanceConnection({
        path: { connection_id: connection.id },
        body,
        throwOnError: true,
      }),
    () => void queryClient.invalidateQueries({ queryKey: ["connections"] }),
    "Key updated",
  );
  return (
    <form aria-label="Update key" onSubmit={form.submit} className="grid gap-4 sm:grid-cols-3">
      {form.fields}
      <div className="flex items-center gap-3 sm:col-span-3">
        <Button type="submit" variant="outline" disabled={form.pending}>
          Update key
        </Button>
        {form.error && <p className="text-sm text-destructive">{form.error}</p>}
        {form.done && <p role="status" className="text-sm">{form.done}</p>}
      </div>
    </form>
  );
}

function ConnectionCard({
  connection,
  accountNames,
}: {
  connection: ConnectionOut;
  accountNames: Map<string, string>;
}) {
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
  const isTrading212 = connection.connector === "trading212";
  const isEtoro = connection.connector === "etoro";
  const isMonzo = connection.connector === "monzo";
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
        {isMonzo && connection.status === "authorising" && (
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
          <p key={warning} className="text-sm text-warning">
            {warning}
          </p>
        ))}
        {expiry && (
          <p className={expiry.soon ? "text-sm font-medium text-warning" : "text-sm"}>
            Token expires on {expiry.date}
          </p>
        )}
        {isIbkr && connection.status === "needs_reauth" && (
          <p role="alert" className="text-sm text-destructive">
            IBKR rejected the token or query. Update them to resume syncing.
          </p>
        )}
        {isMonzo && !connection.webhook_enabled && (
          <p className="text-sm text-muted-foreground">
            Webhooks are off, so new Monzo transactions arrive within 30 minutes.
          </p>
        )}
        {failed && <p className="text-sm text-destructive">{errorMessage(failed.error)}</p>}
        {isTrading212 && connection.status === "needs_reauth" && (
          <p role="alert" className="text-sm text-destructive">
            Trading 212 rejected the key. Update it to resume syncing.
          </p>
        )}
        {isEtoro && connection.status === "needs_reauth" && (
          <p role="alert" className="text-sm text-destructive">
            eToro rejected the keys. Update them to resume syncing.
          </p>
        )}
        {connection.binance && (
          <BinanceState state={connection.binance} accountNames={accountNames} />
        )}
        {connection.connector === "binance" && <UpdateBinanceKeyForm connection={connection} />}
        {connection.bitcoin && <BitcoinState state={connection.bitcoin} />}
        {isIbkr && <UpdateIbkrTokenForm connection={connection} />}
        {isTrading212 && <UpdateTrading212KeysForm connection={connection} />}
        {isEtoro && <UpdateEtoroKeysForm connection={connection} />}
        {isIbkr && <UploadFlexStatement connection={connection} />}
        {isEtoro && <UploadEtoroStatement connection={connection} />}
        {isTrading212 && <UploadTrading212Export connection={connection} />}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={sync.isPending} onClick={() => sync.mutate()}>
            Sync now
          </Button>
          {isMonzo && AUTHORISABLE.includes(connection.status) && (
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

function AddMonzoForm({
  redirectUri,
  onAdded,
}: {
  redirectUri: string | undefined;
  onAdded?: () => void;
}) {
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
      onAdded?.();
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

function AddIbkrForm({ onAdded }: { onAdded?: () => void }) {
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
      onAdded?.();
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

function AddTrading212Form({
  kind,
  onAdded,
}: {
  kind: "invest" | "isa";
  onAdded?: () => void;
}) {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const form = useKeysForm(
    TRADING212_KEYS,
    async (keys) =>
      await createTrading212Connection({
        body: { display_name: displayName.trim(), account_kind: kind, ...keys },
        throwOnError: true,
      }),
    () => {
      setDisplayName("");
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
      onAdded?.();
    },
    "Trading 212 connected",
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Trading 212</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          aria-label="Add Trading 212"
          onSubmit={form.submit}
          className="grid gap-4 sm:grid-cols-2"
        >
          <TextField label="Display name" value={displayName} onChange={setDisplayName} required />
          {form.fields}
          <p className="text-sm text-muted-foreground sm:col-span-2">
            Create an API key in the Trading 212 app under Settings → API (Beta), for this account
            only, with read permissions.
          </p>
          <div className="flex items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={form.pending}>
              Add Trading 212
            </Button>
            {form.error && <p className="text-sm text-destructive">{form.error}</p>}
            {form.done && <p role="status" className="text-sm">{form.done}</p>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function AddEtoroForm({ onAdded }: { onAdded?: () => void }) {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const form = useKeysForm(
    ETORO_KEYS,
    async (keys) =>
      await createEtoroConnection({
        body: { display_name: displayName.trim(), ...keys },
        throwOnError: true,
      }),
    () => {
      setDisplayName("");
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
      onAdded?.();
    },
    "eToro connected",
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add eToro</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Add eToro" onSubmit={form.submit} className="grid gap-4 sm:grid-cols-2">
          <TextField label="Display name" value={displayName} onChange={setDisplayName} required />
          {form.fields}
          <p className="text-sm text-muted-foreground sm:col-span-2">
            Create a key in eToro under Settings → Trading → API Key Management, Environment Real,
            Permission Read.
          </p>
          <div className="flex items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={form.pending}>
              Add eToro
            </Button>
            {form.error && <p className="text-sm text-destructive">{form.error}</p>}
            {form.done && <p role="status" className="text-sm">{form.done}</p>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function AddBinanceForm({ onAdded }: { onAdded?: () => void }) {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const form = useBinanceKeyForm(
    async (key) =>
      await createBinanceConnection({
        body: { display_name: displayName.trim(), ...key },
        throwOnError: true,
      }),
    () => {
      setDisplayName("");
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
      onAdded?.();
    },
    "Binance connected",
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Binance</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="Add Binance" onSubmit={form.submit} className="grid gap-4 sm:grid-cols-2">
          <TextField label="Display name" value={displayName} onChange={setDisplayName} required />
          {form.fields}
          <p className="text-sm text-muted-foreground sm:col-span-2">
            Create an API key on Binance with only <strong>Enable Reading</strong> ticked. Any
            other permission is refused.
          </p>
          <div className="flex items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={form.pending}>
              Add Binance
            </Button>
            {form.error && <p className="text-sm text-destructive">{form.error}</p>}
            {form.done && <p role="status" className="text-sm">{form.done}</p>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

type WalletForm = BitcoinPreviewInWritable["form"];
type ElectrumType = NonNullable<BitcoinPreviewInWritable["electrum_type"]>;

const WALLET_FORMS: { value: WalletForm; label: string }[] = [
  { value: "electrum", label: "Electrum master public key" },
  { value: "descriptor", label: "Output descriptor" },
  { value: "addresses", label: "Address list" },
];

const ELECTRUM_TYPES: { value: ElectrumType; label: string }[] = [
  { value: "single", label: "Standard" },
  { value: "multisig", label: "Multi-signature" },
  { value: "2fa", label: "Two-factor authentication" },
  { value: "old", label: "Old seed (Electrum 1.x)" },
];

// How long typing pauses before the first address is derived again.
const PREVIEW_DELAY_MS = 300;

// One entry per line: a seed phrase pasted on one line stays one entry, so the server
// recognises it (and refuses it) instead of seeing twelve short words.
function lines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function MultilineField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const id = useId();
  return (
    <div className="grid gap-1.5 sm:col-span-2">
      <Label htmlFor={id}>{label}</Label>
      <textarea
        id={id}
        value={value}
        required
        rows={2}
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => onChange(event.target.value)}
        className="w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 font-mono text-base outline-none md:text-sm"
      />
    </div>
  );
}

function gapLimit(text: string): number | undefined {
  return text.trim() ? Number(text.trim()) : undefined;
}

// The keys, descriptors and addresses are cleared as soon as they are sent, whether the request
// then succeeds or fails; the request variables are dropped from the mutation cache (gcTime 0
// and reset). The preview's request is never cached: it is sent directly.
function AddBitcoinForm({ onAdded }: { onAdded?: () => void }) {
  const queryClient = useQueryClient();
  const [displayName, setDisplayName] = useState("");
  const [walletForm, setWalletForm] = useState<WalletForm>("electrum");
  const [electrumType, setElectrumType] = useState<ElectrumType>("single");
  const [material, setMaterial] = useState("");
  const [threshold, setThreshold] = useState("");
  const [receivingGap, setReceivingGap] = useState("");
  const [changeGap, setChangeGap] = useState("");
  const [preview, setPreview] = useState<BitcoinPreviewOut>();
  const [previewError, setPreviewError] = useState<string>();
  const [error, setError] = useState<string>();
  const [done, setDone] = useState<string>();

  const entries = lines(material);
  const isMultisig = walletForm === "electrum" && electrumType === "multisig";
  const wallet: BitcoinPreviewInWritable =
    walletForm === "electrum"
      ? {
          form: walletForm,
          electrum_type: electrumType,
          keys: entries,
          ...(isMultisig && threshold ? { threshold: Number(threshold) } : {}),
        }
      : walletForm === "descriptor"
        ? { form: walletForm, descriptors: entries }
        : { form: walletForm, addresses: entries };
  const walletKey = JSON.stringify(wallet);

  // Keyed by the wallet's JSON: the object itself is new on every render.
  useEffect(() => {
    setPreview(undefined);
    setPreviewError(undefined);
    const body = JSON.parse(walletKey) as BitcoinPreviewInWritable;
    if (!(body.keys ?? body.descriptors ?? body.addresses)?.length) return;
    let current = true;
    const timer = setTimeout(() => {
      previewBitcoinWallet({ body, throwOnError: true }).then(
        (response) => current && setPreview(response.data),
        (failure: unknown) => current && setPreviewError(errorMessage(failure)),
      );
    }, PREVIEW_DELAY_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [walletKey]);

  const mutation = useMutation({
    gcTime: 0,
    mutationFn: async (body: BitcoinWalletCreateWritable) =>
      await createBitcoinConnection({ body, throwOnError: true }),
    onSuccess: () => {
      setDone("Bitcoin wallet connected");
      setDisplayName("");
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
      onAdded?.();
    },
    onError: (failure) => setError(errorMessage(failure)),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    setError(undefined);
    setDone(undefined);
    const branches: Record<string, number> = {};
    const receiving = gapLimit(receivingGap);
    const change = gapLimit(changeGap);
    if (receiving !== undefined) branches["0"] = receiving;
    if (change !== undefined) branches["1"] = change;
    mutation.mutate(
      {
        display_name: displayName.trim(),
        ...wallet,
        // The gap limits apply to the first descriptor; an address list has none.
        ...(walletForm !== "addresses" && Object.keys(branches).length > 0
          ? { gaps: { d0: branches } }
          : {}),
      },
      { onSettled: () => mutation.reset() },
    );
    setMaterial("");
  }

  const materialLabel =
    walletForm === "descriptor"
      ? "Output descriptors (one per line)"
      : walletForm === "addresses"
        ? "Addresses (one per line)"
        : electrumType === "single" || electrumType === "old"
          ? "Master public key"
          : "Master public keys (one per line)";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Add Bitcoin wallet</CardTitle>
      </CardHeader>
      <CardContent>
        <form
          aria-label="Add Bitcoin wallet"
          onSubmit={submit}
          className="grid gap-4 sm:grid-cols-2"
        >
          <TextField label="Display name" value={displayName} onChange={setDisplayName} required />
          <LabelledSelect
            label="Wallet form"
            value={walletForm}
            options={WALLET_FORMS}
            onChange={setWalletForm}
          />
          {walletForm === "electrum" && (
            <LabelledSelect
              label="Wallet type"
              value={electrumType}
              options={ELECTRUM_TYPES}
              onChange={setElectrumType}
            />
          )}
          {isMultisig && (
            <TextField
              label="Signatures required"
              type="number"
              value={threshold}
              onChange={setThreshold}
              required
            />
          )}
          <MultilineField label={materialLabel} value={material} onChange={setMaterial} />
          {walletForm === "electrum" && (
            <p className="text-sm text-muted-foreground sm:col-span-2">
              In Electrum: Wallet → Information → Master Public Key. Never enter your seed
              phrase.
            </p>
          )}
          <p className="text-sm text-muted-foreground sm:col-span-2">
            Lightning channel balances can't be seen from a public key; record them as a manual
            account.
          </p>
          {preview && (
            <p role="status" className="text-sm sm:col-span-2">
              First receiving address ({preview.script_type}):{" "}
              <code className="break-all">{preview.first_receiving_address}</code>. Check that
              it is the first address your wallet shows.
            </p>
          )}
          {previewError && (
            <p className="text-sm text-destructive sm:col-span-2">{previewError}</p>
          )}
          {walletForm !== "addresses" && (
            <details className="sm:col-span-2">
              <summary className="cursor-pointer text-sm">Advanced</summary>
              <div className="mt-3 grid gap-4 sm:grid-cols-2">
                <TextField
                  label="Receiving gap limit"
                  type="number"
                  value={receivingGap}
                  onChange={setReceivingGap}
                />
                <TextField
                  label="Change gap limit"
                  type="number"
                  value={changeGap}
                  onChange={setChangeGap}
                />
                <p className="text-sm text-muted-foreground sm:col-span-2">
                  Compare the balance with Electrum's. If it is lower, raise the gap limit.
                </p>
              </div>
            </details>
          )}
          <div className="flex items-center gap-3 sm:col-span-2">
            <Button type="submit" disabled={mutation.isPending}>
              Add Bitcoin wallet
            </Button>
            {error && <p className="text-sm text-destructive">{error}</p>}
            {done && <p role="status" className="text-sm">{done}</p>}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function BitcoinState({ state }: { state: BitcoinStateOut }) {
  const form = WALLET_FORMS.find((option) => option.value === state.form)?.label ?? state.form;
  const kind = ELECTRUM_TYPES.find((option) => option.value === state.electrum_type)?.label;
  return (
    <div className="space-y-1 text-sm">
      <p>{kind ? `${form}, ${kind.toLowerCase()}` : form}</p>
      <p>
        {`Addresses scanned: ${state.scanned.receiving} receiving, ${state.scanned.change} change;` +
          ` used: ${state.used.receiving} receiving, ${state.used.change} change`}
      </p>
      <p className={state.last_check && state.last_check !== "agrees" ? "text-warning" : undefined}>
        {state.last_check ? `Last balance check: ${state.last_check}` : "Not checked yet"}
      </p>
      <p>{`Unconfirmed transactions waiting: ${state.unconfirmed}`}</p>
      <p className="text-muted-foreground">{`Server: ${state.server}`}</p>
    </div>
  );
}

// The form that connects an account type's API; onAdded runs once the connection exists.
export function ConnectForm({ type, onAdded }: { type: AccountTypeOut; onAdded?: () => void }) {
  const connector = type.api?.connector;
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: async () => (await listConnections({ throwOnError: true })).data,
    enabled: connector === "monzo",
  });
  switch (connector) {
    case "monzo":
      return (
        <AddMonzoForm
          redirectUri={
            connections.data?.find((connection) => connection.redirect_uri)?.redirect_uri ??
            undefined
          }
          onAdded={onAdded}
        />
      );
    case "ibkr":
      return <AddIbkrForm onAdded={onAdded} />;
    case "trading212":
      return (
        <AddTrading212Form
          kind={type.api?.config.account_kind === "isa" ? "isa" : "invest"}
          onAdded={onAdded}
        />
      );
    case "etoro":
      return <AddEtoroForm onAdded={onAdded} />;
    case "binance":
      return <AddBinanceForm onAdded={onAdded} />;
    case "bitcoin":
      return <AddBitcoinForm onAdded={onAdded} />;
    default:
      return null;
  }
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
  const hasDifferences = list.some((connection) => connection.binance?.differences.length);
  // Only fetched when a difference needs an account's name.
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
    enabled: hasDifferences,
  });
  const accountNames = new Map((accounts.data ?? []).map((account) => [account.id, account.name]));

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
        <ConnectionCard key={connection.id} connection={connection} accountNames={accountNames} />
      ))}
      {connections.isSuccess && list.length === 0 && (
        <p className="text-sm text-muted-foreground">
          No connections yet. Connect a bank or broker from Accounts → Add account.
        </p>
      )}
    </div>
  );
}
