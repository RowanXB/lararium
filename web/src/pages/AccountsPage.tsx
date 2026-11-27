import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  ArrowLeftRight,
  ChartCandlestick,
  ChevronDown,
  CircleDashed,
  Coins,
  CreditCard,
  Eye,
  EyeOff,
  House,
  Landmark,
  type LucideIcon,
  PiggyBank,
  Plus,
  Wallet as WalletIcon,
} from "lucide-react";
import { type FormEvent, type ReactNode, useId, useMemo, useState } from "react";

import {
  type AccountCreate,
  type AccountOut,
  type AccountTypeOut,
  addAccountIdentifier,
  createAccount,
  getFinanceSettings,
  getHoldings,
  type InstrumentOut,
  listAccountIdentifiers,
  listAccounts,
  listAccountTypes,
  listInstruments,
  recordSnapshot,
  removeAccountIdentifier,
  setAccountBalanceMode,
  setAccountReporting,
  setMainAccount,
  type SnapshotCreate,
} from "../api";
import { LabelledSelect } from "../components/LabelledSelect";
import { Reveal } from "../components/Reveal";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Segmented } from "../components/Segmented";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../components/ui/card";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { statementFormats, typeOf } from "../lib/accountTypes";
import { errorMessage } from "../lib/errors";
import { isImportable } from "../lib/imports";
import { todayForInput } from "../lib/dates";
import {
  formatMoney,
  formatSortCode,
  isDecimalString,
  negateDecimalString,
  sumDecimalStrings,
} from "../lib/format";
import {
  accountBody,
  BALANCE_SOURCES,
  type BalanceSource,
  SIDES,
  type Side,
} from "../lib/newAccount";
import { refreshAfterTransferWrite } from "../lib/refresh";
import { UploadForm } from "./ImportsPage";

// The ledger's terms, as the account list shows them.
const LEDGER_TYPES: { value: AccountCreate["ledger_type"]; label: string }[] = [
  { value: "asset", label: "Money I have" },
  { value: "liability", label: "Money I owe" },
  { value: "income", label: "Income" },
  { value: "expense", label: "Spending" },
  { value: "equity", label: "Ledger's own" },
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

export function NewAccountForm({ onAdded }: { onAdded?: () => void } = {}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [side, setSide] = useState<Side>("have");
  const [balance, setBalance] = useState<BalanceSource>("files");
  const [subtype, setSubtype] = useState<AccountCreate["subtype"]>("bank");
  const [institutionName, setInstitutionName] = useState("");
  const [taxWrapper, setTaxWrapper] = useState<NonNullable<AccountCreate["tax_wrapper"]>>("none");

  const mutation = useMutation({
    mutationFn: async (body: AccountCreate) => (await createAccount({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setName("");
      setInstitutionName("");
      await queryClient.invalidateQueries({ queryKey: ["accounts"] });
      onAdded?.();
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    mutation.mutate(accountBody({ name, side, balance, subtype, institutionName, taxWrapper }));
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Account details</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label="New account" onSubmit={submit} className="space-y-4">
          <div className="max-w-sm">
            <TextField label="Name" value={name} onChange={setName} required />
          </div>
          <Segmented label="What it holds" value={side} options={SIDES} onChange={setSide} />
          <Segmented
            label="Where its balance comes from"
            value={balance}
            options={BALANCE_SOURCES}
            onChange={setBalance}
          />
          <details className="group">
            <summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">
              More details
            </summary>
            <div className="mt-3 grid gap-4 sm:grid-cols-3">
              <LabelledSelect
                label="Kind"
                value={subtype}
                // In transit is the ledger's own.
                options={SUBTYPES.filter((option) => option.value !== "in_transit")}
                onChange={setSubtype}
              />
              <TextField
                label="Bank or provider"
                value={institutionName}
                onChange={setInstitutionName}
              />
              <LabelledSelect
                label="Tax wrapper"
                value={taxWrapper}
                options={TAX_WRAPPERS}
                onChange={setTaxWrapper}
              />
            </div>
          </details>
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={mutation.isPending}>
              Add account
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
        Save balance
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

type IdentifierKind = "uk_bank_account" | "chain_address";

const IDENTIFIER_KINDS: { value: IdentifierKind; label: string }[] = [
  { value: "uk_bank_account", label: "UK bank account" },
  { value: "chain_address", label: "Chain address" },
];

function IdentifierList({ account }: { account: AccountOut }) {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<IdentifierKind>("uk_bank_account");
  const [address, setAddress] = useState("");
  const [sortCode, setSortCode] = useState("");
  const [accountNumber, setAccountNumber] = useState("");
  const identifiers = useQuery({
    queryKey: ["account-identifiers", account.id],
    queryFn: async () =>
      (await listAccountIdentifiers({ path: { account_id: account.id }, throwOnError: true }))
        .data,
  });
  const add = useMutation({
    mutationFn: async (body: { kind: IdentifierKind; value: string }) =>
      addAccountIdentifier({
        path: { account_id: account.id },
        body,
        throwOnError: true,
      }),
    onSuccess: () => {
      setSortCode("");
      setAccountNumber("");
      setAddress("");
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
    add.mutate(
      kind === "chain_address"
        ? { kind, value: address.trim() }
        : { kind, value: `${sortCode.trim()}:${accountNumber.trim()}` },
    );
  }

  return (
    <div className="space-y-3">
      {identifiers.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No sort code and account number yet</p>
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
        <LabelledSelect label="Kind" value={kind} options={IDENTIFIER_KINDS} onChange={setKind} />
        {kind === "chain_address" ? (
          <TextField label="Chain address" value={address} onChange={setAddress} required />
        ) : (
          <>
            <TextField label="Sort code" value={sortCode} onChange={setSortCode} required />
            <TextField
              label="Account number"
              value={accountNumber}
              onChange={setAccountNumber}
              required
            />
          </>
        )}
        <Button type="submit" disabled={add.isPending}>
          Add
        </Button>
      </form>
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
    </div>
  );
}

// Hiding leaves the account and its records as they are; only the Overview's numbers (net
// worth, holdings, income and spending) stop counting it.
function ReportingToggle({ account }: { account: AccountOut }) {
  const queryClient = useQueryClient();
  const hidden = account.excluded_from_reports;
  const mutation = useMutation({
    mutationFn: async () =>
      setAccountReporting({
        path: { account_id: account.id },
        body: { excluded: !hidden },
        throwOnError: true,
      }),
    onSuccess: () =>
      Promise.all(
        [["accounts"], ["net-worth"], ["holdings"], ["cash-flow"]].map((queryKey) =>
          queryClient.invalidateQueries({ queryKey }),
        ),
      ),
  });
  const label = `${hidden ? "Show" : "Hide"} ${account.name} ${hidden ? "in" : "from"} Overview`;
  return (
    <div className="flex shrink-0 flex-col items-end gap-1">
      <Button
        variant="ghost"
        size="icon-sm"
        aria-pressed={hidden}
        aria-label={label}
        title={label}
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        {hidden ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
      </Button>
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
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
      <p className="text-sm text-muted-foreground">
        Bank details need this account to take transactions as well as typed balances
      </p>
      <Button
        variant="outline"
        size="sm"
        disabled={mutation.isPending}
        onClick={() => mutation.mutate()}
      >
        Allow transactions
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
  // The API takes one level only: an open account of the owner's that has no main account,
  // and, as for identifiers, an asset or liability whose postings count (not snapshots-only).
  const options = accounts
    .filter(
      (other) =>
        other.id === account.parent_account_id ||
        (other.id !== account.id &&
          !other.system &&
          other.status === "open" &&
          other.parent_account_id === null &&
          (other.ledger_type === "asset" || other.ledger_type === "liability") &&
          other.balance_mode !== "snapshots"),
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
        For a savings pot: its transfers pair with this account.
      </p>
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
    </div>
  );
}

type Panel = "balance" | "bank" | "main" | "upload";

const SUBTYPE_ICONS: Record<AccountOut["subtype"], LucideIcon> = {
  bank: Landmark,
  savings_pot: PiggyBank,
  broker: ChartCandlestick,
  exchange: Coins,
  wallet: WalletIcon,
  in_transit: ArrowLeftRight,
  manual_asset: House,
  other: CircleDashed,
};

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-full bg-secondary px-2 py-0.5 text-xs text-muted-foreground">
      {children}
    </span>
  );
}

function PanelButton({
  panel,
  open,
  controls,
  onToggle,
  children,
}: {
  panel: Panel;
  open: boolean;
  controls: string;
  onToggle: (panel: Panel) => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={() => onToggle(panel)}
      className="flex items-center gap-1 rounded-full border px-3 py-1 text-sm text-muted-foreground transition-colors hover:text-foreground aria-expanded:border-primary aria-expanded:text-foreground"
    >
      {children}
      <ChevronDown
        className="size-3.5 transition-transform duration-300 motion-reduce:transition-none"
        style={{ transform: open ? "rotate(180deg)" : undefined }}
        aria-hidden
      />
    </button>
  );
}

function AccountCard({
  account,
  accounts,
  types,
  value,
  currency,
  instruments,
  defaultInstrumentId,
}: {
  account: AccountOut;
  accounts: AccountOut[];
  types: AccountTypeOut[];
  value: string | undefined;
  currency: string | undefined;
  instruments: InstrumentOut[];
  defaultInstrumentId: string | undefined;
}) {
  const [panel, setPanel] = useState<Panel>();
  const panelId = useId();
  const Icon = account.ledger_type === "liability" ? CreditCard : SUBTYPE_ICONS[account.subtype];
  const ownMoney = account.ledger_type === "asset" || account.ledger_type === "liability";
  // Valuation ignores typed balances on a transactions-only account (v1_balances_asof).
  const typesBalance = account.balance_mode !== "transactions" && !account.system;
  // The ledger's own accounts (In transit) can't have bank details.
  const hasBankDetails = ownMoney && !account.system;
  // A connector sets a connected account's main account; one that is already another's
  // main account can't have one.
  const hasMain =
    !account.system &&
    account.source_kind === "manual" &&
    !accounts.some((other) => other.parent_account_id === account.id);
  // A type read by an upload gets an upload form here; one a connection also reads, a link
  // to the connection that takes the file.
  const type = typeOf(types, account);
  const formats = type ? statementFormats(type) : [];
  const canUpload = type !== undefined && formats.length > 0 && isImportable(account);
  const uploadsOnConnections =
    type !== undefined && type.api !== null && type.files.length > 0 && account.status === "open";
  const toggle = (next: Panel) => setPanel((current) => (current === next ? undefined : next));
  const muted = account.excluded_from_reports || account.status === "closed";

  return (
    <li
      aria-label={account.name}
      className="flex flex-col gap-3 rounded-xl border bg-card p-4 transition-colors hover:border-primary/40"
    >
      <div className="flex items-start gap-3">
        <span
          className={`grid size-9 shrink-0 place-items-center rounded-lg bg-secondary ${
            account.ledger_type === "liability" ? "text-spending" : "text-primary"
          }`}
        >
          <Icon className="size-4.5" aria-hidden />
        </span>
        <div className={`min-w-0 flex-1 ${muted ? "opacity-60" : ""}`}>
          <p className="truncate font-medium">{account.name}</p>
          <div className="mt-1 flex flex-wrap gap-1">
            <Chip>{labelOf(SUBTYPES, account.subtype)}</Chip>
            {account.ledger_type === "liability" && <Chip>Owed</Chip>}
            {!ownMoney && <Chip>{labelOf(LEDGER_TYPES, account.ledger_type)}</Chip>}
            {account.source_kind === "connection" && <Chip>Connected</Chip>}
            {account.tax_wrapper !== "none" && (
              <Chip>{labelOf(TAX_WRAPPERS, account.tax_wrapper)}</Chip>
            )}
            {account.status === "closed" && <Chip>Closed</Chip>}
            {account.excluded_from_reports && <Chip>Hidden from Overview</Chip>}
          </div>
        </div>
        <div className="flex shrink-0 items-start gap-1">
          {ownMoney && value !== undefined && currency && (
            <p
              className={`pt-1 text-right font-semibold tabular-nums ${muted ? "opacity-60" : ""}`}
            >
              {formatMoney(value, currency)}
            </p>
          )}
          {/* Only what net worth counts can be hidden: the API refuses the rest. */}
          {ownMoney && !account.system && <ReportingToggle account={account} />}
        </div>
      </div>
      {(typesBalance || hasBankDetails || hasMain || canUpload || uploadsOnConnections) && (
        <div>
          <div className="flex flex-wrap gap-2">
            {typesBalance && (
              <PanelButton
                panel="balance"
                open={panel === "balance"}
                controls={`${panelId}-balance`}
                onToggle={toggle}
              >
                Update balance
              </PanelButton>
            )}
            {hasBankDetails && (
              <PanelButton
                panel="bank"
                open={panel === "bank"}
                controls={`${panelId}-bank`}
                onToggle={toggle}
              >
                Bank details
              </PanelButton>
            )}
            {canUpload && (
              <PanelButton
                panel="upload"
                open={panel === "upload"}
                controls={`${panelId}-upload`}
                onToggle={toggle}
              >
                Upload file
              </PanelButton>
            )}
            {!canUpload && uploadsOnConnections && (
              <Link
                to="/connections"
                className="flex items-center rounded-full border px-3 py-1 text-sm text-muted-foreground transition-colors hover:text-foreground"
              >
                Upload file
              </Link>
            )}
            {hasMain && (
              <PanelButton
                panel="main"
                open={panel === "main"}
                controls={`${panelId}-main`}
                onToggle={toggle}
              >
                Main account
              </PanelButton>
            )}
          </div>
          {typesBalance && (
            <Reveal open={panel === "balance"} id={`${panelId}-balance`}>
              <SnapshotForm
                account={account}
                instruments={instruments}
                defaultInstrumentId={defaultInstrumentId}
              />
            </Reveal>
          )}
          {hasBankDetails && (
            <Reveal open={panel === "bank"} id={`${panelId}-bank`}>
              {/* Fetched only once opened, so the page doesn't make a request per account. */}
              <div role="group" aria-label={`Bank details of ${account.name}`}>
                {/* A snapshots-only account ignores postings, so the API refuses them. */}
                {account.balance_mode === "snapshots" ? (
                  <HybridSwitch account={account} />
                ) : (
                  <IdentifierList account={account} />
                )}
              </div>
            </Reveal>
          )}
          {canUpload && type && (
            <Reveal open={panel === "upload"} id={`${panelId}-upload`}>
              <UploadForm formats={formats} account={account} />
            </Reveal>
          )}
          {hasMain && (
            <Reveal open={panel === "main"} id={`${panelId}-main`}>
              <MainAccountSelect account={account} accounts={accounts} />
            </Reveal>
          )}
        </div>
      )}
    </li>
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
  const types = useQuery({
    queryKey: ["account-types"],
    queryFn: async () => (await listAccountTypes({ throwOnError: true })).data,
  });
  const today = todayForInput();
  // Every account's, hidden ones included: hiding is for the Overview, not for this page.
  const holdings = useQuery({
    queryKey: ["holdings", today, "all-accounts"],
    queryFn: async () =>
      (await getHoldings({ query: { date: today, all_accounts: true }, throwOnError: true }))
        .data,
  });
  const error = accounts.error ?? instruments.error ?? settings.error ?? types.error;
  // Default the snapshot instrument to the reporting currency, which most balances are in.
  const defaultInstrumentId = (
    instruments.data?.find(
      (i) => i.kind === "fiat" && i.symbol === settings.data?.reporting_currency,
    ) ?? instruments.data?.[0]
  )?.id;
  // What each account is worth today.
  const values = useMemo(() => {
    const byAccount = new Map<string, string[]>();
    for (const holding of holdings.data ?? []) {
      if (holding.value === null) continue;
      byAccount.set(holding.account_id, [...(byAccount.get(holding.account_id) ?? []), holding.value]);
    }
    return new Map([...byAccount].map(([id, parts]) => [id, sumDecimalStrings(parts)]));
  }, [holdings.data]);
  // The owner's open accounts first, the ledger's own and closed ones after.
  const list = [...(accounts.data ?? [])].sort(
    (a, b) =>
      Number(a.system) - Number(b.system) ||
      Number(a.status === "closed") - Number(b.status === "closed"),
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold tracking-tight">Accounts</h1>
        <Button asChild>
          <Link to="/accounts/new">
            <Plus aria-hidden />
            Add account
          </Link>
        </Button>
      </div>
      {error && <p className="text-sm text-destructive">{errorMessage(error)}</p>}
      {accounts.data?.length === 0 && (
        <p className="text-sm text-muted-foreground">No accounts yet</p>
      )}
      <ul aria-label="Accounts" className="grid items-start gap-3 lg:grid-cols-2">
        {list.map((account) => (
          <AccountCard
            key={account.id}
            account={account}
            accounts={accounts.data ?? []}
            types={types.data ?? []}
            value={values.get(account.id)}
            currency={settings.data?.reporting_currency}
            instruments={instruments.data ?? []}
            defaultInstrumentId={defaultInstrumentId}
          />
        ))}
      </ul>
    </div>
  );
}
