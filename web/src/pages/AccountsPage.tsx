import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import {
  type AccountCreate,
  type AccountOut,
  createAccount,
  getFinanceSettings,
  type InstrumentOut,
  listAccounts,
  listInstruments,
  recordSnapshot,
  type SnapshotCreate,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { errorMessage } from "../lib/errors";
import { negateDecimalString } from "../lib/format";

const DECIMAL = /^-?\d+(\.\d+)?$/;

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
    if (!selectedInstrumentId || !DECIMAL.test(entered)) {
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
      {invalid && <p className="text-sm text-destructive">Enter a number, e.g. 1234.56</p>}
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
      {mutation.isSuccess && <p className="text-sm text-muted-foreground">Saved</p>}
    </form>
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
                <SnapshotForm
                  account={account}
                  instruments={instruments.data ?? []}
                  defaultInstrumentId={defaultInstrumentId}
                />
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <NewAccountForm />
    </div>
  );
}
