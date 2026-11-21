import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import {
  type AccountCreate,
  type AccountOut,
  addAccountIdentifier,
  createAccount,
  getFinanceSettings,
  type InstrumentOut,
  listAccountIdentifiers,
  listAccounts,
  listInstruments,
  recordSnapshot,
  removeAccountIdentifier,
  setAccountBalanceMode,
  setMainAccount,
  type SnapshotCreate,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { errorMessage } from "../lib/errors";
import { formatSortCode, isDecimalString, negateDecimalString } from "../lib/format";
import { refreshAfterTransferWrite } from "../lib/refresh";

const LEDGER_TYPES: { value: AccountCreate["ledger_type"]; label: string }[] = [
  { value: "asset", label: "Asset" },
  { value: "liability", label: "Liability" },
  { value: "income", label: "Income" },
  { value: "expense", label: "Expense" },
  { value: "equity", label: "Equity" },
];

const SUBTYPES: { value: AccountCreate["subtype"]; label: string }[] = [
  { value: "bank", label: "Bank" },
  { value: "savings_pot", label: "Savings pot" },
  { value: "broker", label: "Broker" },
  { value: "exchange", label: "Exchange" },
  { value: "wallet", label: "Wallet" },
  { value: "in_transit", label: "In transit" },
  { value: "manual_asset", label: "Manual asset" },
  { value: "other", label: "Other" },
];

const BALANCE_MODES: { value: AccountCreate["balance_mode"]; label: string }[] = [
  { value: "transactions", label: "Transactions" },
  { value: "snapshots", label: "Snapshots" },
  { value: "hybrid", label: "Hybrid" },
];

const TAX_WRAPPERS: { value: NonNullable<AccountCreate["tax_wrapper"]>; label: string }[] = [
  { value: "none", label: "None" },
  { value: "isa", label: "ISA" },
  { value: "sipp", label: "SIPP" },
  { value: "lisa", label: "LISA" },
  { value: "gia", label: "GIA" },
];

function labelOf<T extends string>(options: { value: T; label: string }[], value: T): string {
  return options.find((option) => option.value === value)?.label ?? value;
}

// The value a datetime-local input expects for the current local time.
function nowForInput(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function NewAccountForm() {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [ledgerType, setLedgerType] = useState<AccountCreate["ledger_type"]>("asset");
  const [subtype, setSubtype] = useState<AccountCreate["subtype"]>("bank");
  const [balanceMode, setBalanceMode] = useState<AccountCreate["balance_mode"]>("snapshots");
  const [institutionName, setInstitutionName] = useState("");
  const [taxWrapper, setTaxWrapper] = useState<NonNullable<AccountCreate["tax_wrapper"]>>("none");

  const mutation = useMutation({
    mutationFn: async (body: AccountCreate) => (await createAccount({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setName("");
      setInstitutionName("");
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate({
      name: name.trim(),
      ledger_type: ledgerType,
      subtype,
      balance_mode: balanceMode,
      institution_name: institutionName.trim() || null,
      tax_wrapper: taxWrapper,
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New account</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="New account" onSubmit={submit} className="grid gap-4 sm:grid-cols-3">
          <TextField label="Name" value={name} onChange={setName} required />
          <LabelledSelect
            label="Ledger type"
            value={ledgerType}
            options={LEDGER_TYPES}
            onChange={setLedgerType}
          />
          <LabelledSelect label="Subtype" value={subtype} options={SUBTYPES} onChange={setSubtype} />
          <LabelledSelect
            label="Balance mode"
            value={balanceMode}
            options={BALANCE_MODES}
            onChange={setBalanceMode}
          />
          <TextField
            label="Institution name"
            value={institutionName}
            onChange={setInstitutionName}
          />
          <LabelledSelect
            label="Tax wrapper"
            value={taxWrapper}
            options={TAX_WRAPPERS}
            onChange={setTaxWrapper}
          />
          <div className="flex items-center gap-3 sm:col-span-3">
            <Button type="submit" disabled={mutation.isPending}>
              Create account
            </Button>
            {mutation.isError && (
              <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

function SnapshotForm({
  account,
  instruments,
  defaultInstrumentId,
}: {
  account: AccountOut;
  instruments: InstrumentOut[];
  defaultInstrumentId: string | undefined;
}) {
  const queryClient = useQueryClient();
  const quantityId = useId();
  const asOfId = useId();
  const [instrumentId, setInstrumentId] = useState<string>();
  const [quantity, setQuantity] = useState("");
  const [asOf, setAsOf] = useState(nowForInput);
  const [invalid, setInvalid] = useState(false);

  const isLiability = account.ledger_type === "liability";
  const selectedInstrumentId = instrumentId ?? defaultInstrumentId;

  const mutation = useMutation({
    mutationFn: async (body: SnapshotCreate) =>
      (await recordSnapshot({ path: { account_id: account.id }, body, throwOnError: true })).data,
    onSuccess: async () => {
      setQuantity("");
      await queryClient.invalidateQueries({ queryKey: ["holdings"] });
      await queryClient.invalidateQueries({ queryKey: ["net-worth"] });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const entered = quantity.trim();
    // The amount owed is entered positive; a "-" would flip the stored sign to an asset.
    if (!selectedInstrumentId || !isDecimalString(entered, { unsigned: isLiability })) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    mutation.mutate({
      instrument_id: selectedInstrumentId,
      // Liabilities are entered as the positive amount owed and stored as negative quantities.
      quantity: isLiability ? negateDecimalString(entered) : entered,
      as_of: new Date(asOf).toISOString(),
    });
  }

  return (
    <form
      aria-label={`Snapshot for ${account.name}`}
      onSubmit={submit}
      className="flex flex-wrap items-end gap-3"
    >
      <LabelledSelect
        label="Instrument"
        value={selectedInstrumentId}
        options={instruments.map((instrument) => ({
          value: instrument.id,
          label: instrument.symbol,
        }))}
        onChange={setInstrumentId}
        placeholder="Instrument"
      />
      <div className="grid gap-1.5">
        <Label htmlFor={quantityId}>{isLiability ? "Amount owed" : "Quantity"}</Label>
        <Input
          id={quantityId}
          inputMode="decimal"
          value={quantity}
          aria-invalid={invalid || undefined}
          onChange={(e) => setQuantity(e.target.value)}
          className="w-36"
        />
      </div>
      <div className="grid gap-1.5">
        <Label htmlFor={asOfId}>As of</Label>
        <Input
          id={asOfId}
          type="datetime-local"
          value={asOf}
          required
          onChange={(e) => setAsOf(e.target.value)}
        />
      </div>
      <Button type="submit" disabled={mutation.isPending}>
        Save snapshot
      </Button>
      {invalid && (
        <p className="text-sm text-destructive">
          {isLiability
            ? "Enter the amount owed as a positive number, e.g. 500"
            : "Enter a number, e.g. 1234.56"}
        </p>
      )}
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
      {mutation.isSuccess && <p className="text-sm text-muted-foreground">Saved</p>}
    </form>
  );
}

function IdentifierList({ account }: { account: AccountOut }) {
  const queryClient = useQueryClient();
  const [sortCode, setSortCode] = useState("");
  const [accountNumber, setAccountNumber] = useState("");
  const identifiers = useQuery({
    queryKey: ["account-identifiers", account.id],
    queryFn: async () =>
      (await listAccountIdentifiers({ path: { account_id: account.id }, throwOnError: true }))
        .data,
  });
  const add = useMutation({
    mutationFn: async (value: string) =>
      addAccountIdentifier({
        path: { account_id: account.id },
        body: { kind: "uk_bank_account", value },
        throwOnError: true,
      }),
    onSuccess: () => {
      setSortCode("");
      setAccountNumber("");
    },
    onSettled: () => refreshAfterTransferWrite(queryClient),
  });
  const remove = useMutation({
    mutationFn: async (identifierId: string) =>
      removeAccountIdentifier({
        path: { account_id: account.id, identifier_id: identifierId },
        throwOnError: true,
      }),
    onSettled: () => refreshAfterTransferWrite(queryClient),
  });
  const error = add.error ?? remove.error ?? identifiers.error;

  function submit(event: FormEvent) {
    event.preventDefault();
    // The API accepts either part with spaces or dashes and checks the digits.
    add.mutate(`${sortCode.trim()}:${accountNumber.trim()}`);
  }

  return (
    <div className="space-y-3">
      {identifiers.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No identifiers yet</p>
      )}
      <ul className="space-y-2">
        {identifiers.data?.map((identifier) => (
          <li key={identifier.id} className="flex flex-wrap items-center gap-3 text-sm">
            <span>
              {identifier.sort_code && identifier.account_number
                ? `${formatSortCode(identifier.sort_code)} · ${identifier.account_number}`
                : identifier.value}
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={remove.isPending}
              onClick={() => remove.mutate(identifier.id)}
            >
              Remove
            </Button>
          </li>
        ))}
      </ul>
      <form
        aria-label={`New identifier for ${account.name}`}
        onSubmit={submit}
        className="flex flex-wrap items-end gap-3"
      >
        <TextField label="Sort code" value={sortCode} onChange={setSortCode} required />
        <TextField
          label="Account number"
          value={accountNumber}
          onChange={setAccountNumber}
          required
        />
        <Button type="submit" disabled={add.isPending}>
          Add identifier
        </Button>
      </form>
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
    </div>
  );
}

function HybridSwitch({ account }: { account: AccountOut }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: async () =>
      setAccountBalanceMode({
        path: { account_id: account.id },
        body: { balance_mode: "hybrid" },
        throwOnError: true,
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["accounts"] }),
  });
  return (
    <div className="flex flex-wrap items-center gap-3">
      <p className="text-sm text-muted-foreground">Switch to hybrid to register identifiers</p>
      <Button
        variant="outline"
        size="sm"
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        Switch to hybrid
      </Button>
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
    </div>
  );
}

// Select values can't be empty, so "no main account" is this.
const NO_MAIN_ACCOUNT = "none";

function MainAccountSelect({ account, accounts }: { account: AccountOut; accounts: AccountOut[] }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: async (mainAccountId: string) =>
      setMainAccount({
        path: { account_id: account.id },
        body: { main_account_id: mainAccountId === NO_MAIN_ACCOUNT ? null : mainAccountId },
        throwOnError: true,
      }),
    // Matching its transfers anew moves balances and transfers as well.
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      await refreshAfterTransferWrite(queryClient);
    },
  });
  // The API takes one level only: an open account of the owner's that has no main account.
  const options = accounts
    .filter(
      (other) =>
        other.id === account.parent_account_id ||
        (other.id !== account.id &&
          !other.system &&
          other.status === "open" &&
          other.parent_account_id === null),
    )
    .map((other) => ({ value: other.id, label: other.name }));
  return (
    <div role="group" aria-label={`Main account of ${account.name}`} className="space-y-2">
      <LabelledSelect
        label="Main account"
        value={account.parent_account_id ?? NO_MAIN_ACCOUNT}
        options={[{ value: NO_MAIN_ACCOUNT, label: "None" }, ...options]}
        onChange={(next) => mutation.mutate(next)}
      />
      <p className="text-sm text-muted-foreground">
        A savings account's bank transfers that name no bank account are paired with its main
        account.
      </p>
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
    </div>
  );
}

function Identifiers({ account }: { account: AccountOut }) {
  // Fetched only once opened, so the page doesn't make a request per account.
  const [open, setOpen] = useState(false);
  return (
    <details
      aria-label={`Identifiers of ${account.name}`}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm font-medium">Identifiers</summary>
      {open && (
        <div className="mt-3">
          {/* A snapshots-only account ignores postings, so the API refuses its identifiers. */}
          {account.balance_mode === "snapshots" ? (
            <HybridSwitch account={account} />
          ) : (
            <IdentifierList account={account} />
          )}
        </div>
      )}
    </details>
  );
}

export function AccountsPage() {
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => (await listAccounts({ throwOnError: true })).data,
  });
  const instruments = useQuery({
    queryKey: ["instruments", { includeHidden: false }],
    queryFn: async () => (await listInstruments({ throwOnError: true })).data,
  });
  const settings = useQuery({
    queryKey: ["finance-settings"],
    queryFn: async () => (await getFinanceSettings({ throwOnError: true })).data,
  });
  const error = accounts.error ?? instruments.error ?? settings.error;
  // Default the snapshot instrument to the reporting currency, which most balances are in.
  const defaultInstrumentId = (
    instruments.data?.find(
      (i) => i.kind === "fiat" && i.symbol === settings.data?.reporting_currency,
    ) ?? instruments.data?.[0]
  )?.id;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Accounts</CardTitle>
        </CardHeader>
        <CardContent>
          {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
          {accounts.data?.length === 0 && (
            <p className="text-sm text-muted-foreground">No accounts yet</p>
          )}
          <ul className="divide-y">
            {accounts.data?.map((account) => (
              <li key={account.id} className="space-y-3 py-4 first:pt-0 last:pb-0">
                <div>
                  <p className="font-medium">{account.name}</p>
                  <p className="text-sm text-muted-foreground">
                    {[
                      labelOf(LEDGER_TYPES, account.ledger_type),
                      labelOf(SUBTYPES, account.subtype),
                      labelOf(BALANCE_MODES, account.balance_mode),
                      account.tax_wrapper !== "none" && labelOf(TAX_WRAPPERS, account.tax_wrapper),
                      account.status === "closed" && "Closed",
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                </div>
                {/* Valuation ignores snapshots on transactions-mode accounts (v1_balances_asof). */}
                {account.balance_mode === "transactions" ? (
                  <p className="text-sm text-muted-foreground">
                    Balance comes from transactions, so snapshots don't apply
                  </p>
                ) : (
                  <SnapshotForm
                    account={account}
                    instruments={instruments.data ?? []}
                    defaultInstrumentId={defaultInstrumentId}
                  />
                )}
                {/* The ledger's own accounts (In transit) can't have identifiers. */}
                {(account.ledger_type === "asset" || account.ledger_type === "liability") &&
                  !account.system && <Identifiers account={account} />}
                {/* A connector sets a connected account's main account; one that is already
                    another's main account can't have one. */}
                {!account.system &&
                  account.source_kind === "manual" &&
                  !accounts.data?.some((other) => other.parent_account_id === account.id) && (
                    <MainAccountSelect account={account} accounts={accounts.data ?? []} />
                  )}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <NewAccountForm />
    </div>
  );
}
