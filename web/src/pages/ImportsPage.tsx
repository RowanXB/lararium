import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { type FormEvent, useId, useState } from "react";

import {
  type BodyStageImport,
  createAccount,
  createImportProfile,
  type ImportCatalogEntryOut,
  getFinanceSettings,
  type ImportProfileOut,
  listAccounts,
  listImportBatches,
  listImportProfiles,
  listInstruments,
  stageImport,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { Segmented } from "../components/Segmented";
import { TextField } from "../components/TextField";
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
import { type Detection, detectFormat, fileFormat, firstLineOf } from "../lib/detectFormat";
import { accountBody, SIDES, type Side } from "../lib/newAccount";

// The account select's value for "a new account, named here".
const NEW_ACCOUNT = "new";

// The currency select's value for "use the currency the file states".
const FROM_FILE = "file";

// Rows without a currency of their own take the one chosen at upload. OFX files and PDF
// statements state one, and so do CSVs that map a currency column or whose profile sets
// the one the file states once, so the choice can be left to the file (a row still without
// one becomes an error the review page shows).
function fileStatesCurrency(config: ImportProfileOut["config"] | undefined): boolean {
  if (config === undefined) return false;
  if (config.format === "ofx" || config.format === "pdf") return true;
  return config.format === "csv" && Boolean(config.columns.currency || config.currency);
}

// A catalog format's profile: the one already made from it (the catalog names it so),
// else a new one.
function catalogProfileName(entry: ImportCatalogEntryOut): string {
  return `${entry.institution} ${entry.label}`;
}

type UploadFormProps = {
  // One bank's formats from the catalog: the form asks for a format instead of a profile,
  // and makes the profile itself.
  formats?: ImportCatalogEntryOut[];
};

export function UploadForm({ formats }: UploadFormProps = {}) {
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
  const [newName, setNewName] = useState("");
  const [newSide, setNewSide] = useState<Side>("have");
  const [profileId, setProfileId] = useState<string>();
  // Which of the bank's formats the chosen file is; picked by hand only when it can't be told.
  const [detection, setDetection] = useState<Detection>();
  const [formatLabel, setFormatLabel] = useState<string>();
  const [currency, setCurrency] = useState<string>();
  const [file, setFile] = useState<File>();
  const [incomplete, setIncomplete] = useState(false);

  const importable = (accounts.data ?? []).filter(isImportable);
  // With no account to pick, the form starts at a new one.
  const selectedAccount = accountId ?? (accounts.data && importable.length === 0 ? NEW_ACCOUNT : undefined);
  const creating = selectedAccount === NEW_ACCOUNT;
  const fiat = (instruments.data ?? []).filter((instrument) => instrument.kind === "fiat");
  const format =
    detection?.kind === "found"
      ? detection.entry
      : detection?.kind === "ambiguous"
        ? detection.entries.find((entry) => entry.label === formatLabel)
        : undefined;

  async function chooseFile(chosen: File | undefined) {
    setFile(chosen);
    setFormatLabel(undefined);
    if (!formats || !chosen) {
      setDetection(undefined);
      return;
    }
    const firstLine = fileFormat(chosen.name) === "csv" ? await firstLineOf(chosen) : "";
    setDetection(detectFormat(formats, chosen.name, firstLine));
  }
  const profile = profiles.data?.find((p) => p.id === profileId);
  const fromFileAllowed = fileStatesCurrency(formats ? format?.config : profile?.config);
  const reportingCurrencyId = fiat.find(
    (instrument) => isoCode(instrument) === settings.data?.reporting_currency,
  )?.id;
  // "From the file" stops applying when the profile changes to one that can't state it.
  const selectedCurrency =
    currency === undefined || (currency === FROM_FILE && !fromFileAllowed)
      ? reportingCurrencyId
      : currency;

  const mutation = useMutation({
    mutationFn: async (body: Omit<BodyStageImport, "account_id"> & { account_id?: string }) => {
      let account_id = body.account_id;
      if (account_id === undefined) {
        const created = (
          await createAccount({
            body: accountBody({ name: newName, side: newSide, balance: "files" }),
            throwOnError: true,
          })
        ).data;
        account_id = created.id;
        // Picked from now on, so trying again after a failed upload makes no second account.
        setAccountId(created.id);
        await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      }
      let profile_id = body.profile_id;
      if (format) {
        const name = catalogProfileName(format);
        profile_id =
          profiles.data?.find((p) => p.name === name)?.id ??
          (
            await createImportProfile({
              body: { name, config: format.config },
              throwOnError: true,
            })
          ).data.id;
        await queryClient.invalidateQueries({ queryKey: ["import-profiles"] });
      }
      return (
        await stageImport({ body: { ...body, account_id, profile_id }, throwOnError: true })
      ).data;
    },
    onSuccess: async (batch) => {
      await queryClient.invalidateQueries({ queryKey: ["imports"] });
      await navigate({ to: "/imports/$batchId", params: { batchId: batch.id } });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    if (
      !selectedAccount ||
      (creating && !newName.trim()) ||
      !(formats ? format : profileId) ||
      !file ||
      !selectedCurrency
    ) {
      setIncomplete(true);
      return;
    }
    setIncomplete(false);
    mutation.mutate({
      account_id: creating ? undefined : selectedAccount,
      // A catalog format's profile is found or made just before staging.
      profile_id: profileId ?? "",
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
        {!formats && profiles.data?.length === 0 && (
          <p className="text-sm text-muted-foreground">
            No profiles yet. <Link to="/import-profiles">Add one</Link> first.
          </p>
        )}
        <form aria-label="Upload statement" onSubmit={submit} className="grid gap-4 sm:grid-cols-4">
          <LabelledSelect
            label="Account"
            value={selectedAccount}
            options={[
              ...importable.map((account) => ({ value: account.id, label: account.name })),
              { value: NEW_ACCOUNT, label: "New account…" },
            ]}
            onChange={setAccountId}
            placeholder="Account"
          />
          {creating && (
            <div className="flex flex-wrap items-end gap-4 rounded-lg border border-dashed p-3 sm:col-span-4">
              <div className="min-w-56">
                <TextField label="New account name" value={newName} onChange={setNewName} />
              </div>
              <Segmented label="What it holds" value={newSide} options={SIDES} onChange={setNewSide} />
              <p className="w-full text-sm text-muted-foreground">
                A credit card or a loan is money you owe. The account is created when you upload.
              </p>
            </div>
          )}
          {!formats && (
            <LabelledSelect
              label="Profile"
              value={profileId}
              options={(profiles.data ?? []).map((p) => ({ value: p.id, label: p.name }))}
              onChange={setProfileId}
              placeholder="Profile"
            />
          )}
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
              onChange={(e) => void chooseFile(e.target.files?.[0])}
            />
          </div>
          {formats && detection && (
            <div className="sm:col-span-4">
              {detection.kind === "found" && (
                <p role="status" className="text-sm text-income">
                  Recognised as {detection.entry.institution} {detection.entry.label}
                </p>
              )}
              {detection.kind === "ambiguous" && (
                <div className="max-w-sm">
                  <LabelledSelect
                    label="Which file is it?"
                    value={formatLabel}
                    options={detection.entries.map((entry) => ({
                      value: entry.label,
                      label: entry.label,
                    }))}
                    onChange={setFormatLabel}
                    placeholder="File format"
                  />
                </div>
              )}
              {detection.kind === "unknown" && (
                <p role="alert" className="text-sm text-warning">
                  This isn't a file {formats[0]?.institution} exports. Upload one of:{" "}
                  {formats.map((entry) => entry.label).join(", ")}.
                </p>
              )}
            </div>
          )}
          <p className="text-sm text-muted-foreground sm:col-span-4">
            The currency is used for rows whose file states none.
          </p>
          <div className="flex items-center gap-3 sm:col-span-4">
            <Button type="submit" disabled={mutation.isPending}>
              Upload
            </Button>
            {incomplete && (
              <p className="text-sm text-destructive">
                {creating && !newName.trim()
                  ? "Name the new account"
                  : formats
                    ? file && !format
                      ? "Choose which file it is"
                      : "Choose an account, a currency and a file"
                    : "Choose an account, a profile, a currency and a file"}
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
