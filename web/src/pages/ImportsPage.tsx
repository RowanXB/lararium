import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useId, useState } from "react";

import {
  type BodyStageImport,
  getFinanceSettings,
  type ImportProfileOut,
  listAccounts,
  listImportBatches,
  listImportProfiles,
  listInstruments,
  stageImport,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { errorMessage } from "../lib/errors";
import {
  BATCH_STATUS_LABELS,
  countsSummary,
  formatTimestamp,
  isImportable,
  isoCode,
} from "../lib/imports";

// The currency select's value for "use the currency the file states".
const FROM_FILE = "file";

// Rows without a currency of their own take the one chosen at upload. OFX files and PDF
// statements state one, and so do CSVs that map a currency column, so the choice can be
// left to the file (a row still without one becomes an error the review page shows).
function fileStatesCurrency(profile: ImportProfileOut | undefined): boolean {
  if (profile === undefined) return false;
  if (profile.config.format === "ofx" || profile.config.format === "pdf") return true;
  return profile.config.format === "csv" && Boolean(profile.config.columns.currency);
}

function UploadForm() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const fileId = useId();
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
  });
  const profiles = useQuery({
    queryKey: ["import-profiles"],
    queryFn: async () => (await listImportProfiles({ throwOnError: true })).data,
  });
  const instruments = useQuery({
    queryKey: ["instruments", { includeHidden: false }],
    queryFn: async () => (await listInstruments({ throwOnError: true })).data,
  });
  const settings = useQuery({
    queryKey: ["finance-settings"],
    queryFn: async () => (await getFinanceSettings({ throwOnError: true })).data,
  });

  const [accountId, setAccountId] = useState<string>();
  const [profileId, setProfileId] = useState<string>();
  const [currency, setCurrency] = useState<string>();
  const [file, setFile] = useState<File>();
  const [incomplete, setIncomplete] = useState(false);

  const importable = (accounts.data ?? []).filter(isImportable);
  const fiat = (instruments.data ?? []).filter((instrument) => instrument.kind === "fiat");
  const profile = profiles.data?.find((p) => p.id === profileId);
  const fromFileAllowed = fileStatesCurrency(profile);
  const reportingCurrencyId = fiat.find(
    (instrument) => isoCode(instrument) === settings.data?.reporting_currency,
  )?.id;
  // "From the file" stops applying when the profile changes to one that can't state it.
  const selectedCurrency =
    currency === undefined || (currency === FROM_FILE && !fromFileAllowed)
      ? reportingCurrencyId
      : currency;

  const mutation = useMutation({
    mutationFn: async (body: BodyStageImport) =>
      (await stageImport({ body, throwOnError: true })).data,
    onSuccess: async (batch) => {
      await queryClient.invalidateQueries({ queryKey: ["imports"] });
      await navigate({ to: "/imports/$batchId", params: { batchId: batch.id } });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!accountId || !profileId || !file || !selectedCurrency) {
      setIncomplete(true);
      return;
    }
    setIncomplete(false);
    mutation.mutate({
      account_id: accountId,
      profile_id: profileId,
      file,
      // Left out, the server takes each row's currency from the file.
      ...(selectedCurrency === FROM_FILE ? {} : { instrument_id: selectedCurrency }),
    });
  }

  const error = accounts.error ?? profiles.error ?? instruments.error ?? settings.error;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Upload statement</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
        {accounts.data && importable.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No account accepts imports. Imports need an open manual account whose balance mode
            is Transactions or Hybrid.
          </p>
        )}
        {profiles.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No profiles yet. <Link to="/import-profiles">Add one</Link> first.
          </p>
        )}
        <form aria-label="Upload statement" onSubmit={submit} className="grid gap-4 sm:grid-cols-4">
          <LabelledSelect
            label="Account"
            value={accountId}
            options={importable.map((account) => ({ value: account.id, label: account.name }))}
            onChange={setAccountId}
            placeholder="Account"
          />
          <LabelledSelect
            label="Profile"
            value={profileId}
            options={(profiles.data ?? []).map((p) => ({ value: p.id, label: p.name }))}
            onChange={setProfileId}
            placeholder="Profile"
          />
          <LabelledSelect
            label="Currency"
            value={selectedCurrency}
            options={[
              ...(fromFileAllowed ? [{ value: FROM_FILE, label: "From the file" }] : []),
              ...fiat.map((instrument) => ({ value: instrument.id, label: isoCode(instrument) })),
            ]}
            onChange={setCurrency}
            placeholder="Currency"
          />
          <div className="grid gap-1.5">
            <Label htmlFor={fileId}>File</Label>
            <Input
              id={fileId}
              type="file"
              onChange={(e) => setFile(e.target.files?.[0])}
            />
          </div>
          <p className="text-sm text-muted-foreground sm:col-span-4">
            The currency is used for rows whose file states none.
          </p>
          <div className="flex items-center gap-3 sm:col-span-4">
            <Button type="submit" disabled={mutation.isPending}>
              Upload
            </Button>
            {incomplete && (
              <p className="text-sm text-destructive">
                Choose an account, a profile, a currency and a file
              </p>
            )}
            {mutation.isError && (
              <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function BatchList() {
  const batches = useQuery({
    queryKey: ["imports"],
    queryFn: async () => (await listImportBatches({ throwOnError: true })).data,
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
  });
  const accountName = (id: string) => accounts.data?.find((a) => a.id === id)?.name ?? "";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Imports</CardTitle>
      </CardHeader>
      <CardContent>
        {batches.error && (
          <p className="text-sm text-destructive">{errorMessage(batches.error)}</p>
        )}
        {batches.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">No imports yet</p>
        )}
        {batches.data && batches.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>File</TableHead>
                <TableHead>Account</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Rows</TableHead>
                <TableHead>Uploaded</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {batches.data.map((batch) => (
                <TableRow key={batch.id}>
                  <TableCell className="font-medium">
                    <Link
                      to="/imports/$batchId"
                      params={{ batchId: batch.id }}
                      className="hover:underline"
                    >
                      {batch.filename}
                    </Link>
                  </TableCell>
                  <TableCell>{accountName(batch.account_id)}</TableCell>
                  <TableCell>{BATCH_STATUS_LABELS[batch.status]}</TableCell>
                  <TableCell>{countsSummary(batch.counts)}</TableCell>
                  <TableCell>{formatTimestamp(batch.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

export function ImportsPage() {
  return (
    <div className="space-y-6">
      <UploadForm />
      <BatchList />
    </div>
  );
}
