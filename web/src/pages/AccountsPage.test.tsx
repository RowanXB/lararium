import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import {
  addAccountIdentifier,
  createAccount,
  listAccountIdentifiers,
  listAccounts,
  listAccountTypes,
  recordSnapshot,
  removeAccountIdentifier,
  setAccountBalanceMode,
  setAccountReporting,
  setMainAccount,
} from "../api";
import type { ReactNode } from "react";

import { AccountsPage, NewAccountForm } from "./AccountsPage";

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => vi.fn(),
  Link: ({ children, to }: { children: ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

vi.mock("../api", () => ({
  getHoldings: vi.fn(async () => ({ data: [] })),
  listAccounts: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-000000000001",
        name: "Credit card",
        ledger_type: "liability",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "snapshots",
        source_kind: "manual",
        source_connection_id: null,
        status: "open",
        opened_on: null,
        closed_on: null,
        system: false,
      },
      {
        id: "0190a0b0-0000-7000-8000-000000000002",
        name: "Ledger account",
        ledger_type: "asset",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "transactions",
        source_kind: "manual",
        source_connection_id: null,
        status: "open",
        opened_on: null,
        closed_on: null,
        system: false,
      },
{
        id: "0190a0b0-0000-7000-8000-000000000011",
        name: "Cash ISA",
        ledger_type: "asset",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "transactions",
        source_kind: "manual",
        source_connection_id: null,
        status: "open",
        opened_on: null,
        closed_on: null,
        system: false,
        type_key: "trading212.cash_isa",
      },
{
        id: "0190a0b0-0000-7000-8000-000000000012",
        name: "Invest",
        ledger_type: "asset",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "transactions",
        source_kind: "connection",
        source_connection_id: null,
        status: "open",
        opened_on: null,
        closed_on: null,
        system: false,
        type_key: "trading212.invest",
      },
{
        id: "0190a0b0-0000-7000-8000-000000000013",
        name: "Untyped",
        ledger_type: "asset",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "transactions",
        source_kind: "manual",
        source_connection_id: null,
        status: "open",
        opened_on: null,
        closed_on: null,
        system: false,
        type_key: null,
      },
{
        id: "0190a0b0-0000-7000-8000-000000000014",
        name: "Gone type",
        ledger_type: "asset",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "transactions",
        source_kind: "manual",
        source_connection_id: null,
        status: "open",
        opened_on: null,
        closed_on: null,
        system: false,
        type_key: "gone",
      },
{
        id: "0190a0b0-0000-7000-8000-000000000015",
        name: "Closed ISA",
        ledger_type: "asset",
        subtype: "bank",
        institution_entity_id: null,
        owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
        tax_wrapper: "none",
        parent_account_id: null,
        balance_mode: "transactions",
        source_kind: "manual",
        source_connection_id: null,
        status: "closed",
        opened_on: null,
        closed_on: null,
        system: false,
        type_key: "trading212.cash_isa",
      },
    ],
  })),
  listAccountTypes: vi.fn(async () => ({
    data: [
      {
        key: "trading212.cash_isa",
        institution: "Trading 212",
        label: "Cash ISA",
        name: "Trading 212 Cash ISA",
        shape: null,
        api: null,
        funding: [],
        family: "trading212",
        files: [
          {
            label: "History CSV",
            format: "csv",
            recommended: true,
            config: { format: "csv", columns: { date: "Time", amount: "Total" }, date_format: "%Y-%m-%d" },
          },
        ],
      },
      {
        key: "trading212.invest",
        institution: "Trading 212",
        label: "Invest",
        name: "Trading 212 Invest",
        shape: null,
        api: { connector: "trading212", config: {} },
        funding: [],
        family: "trading212",
        files: [{ label: "History CSV", format: "csv", recommended: false, config: null }],
      },
    ],
  })),
  listImportProfiles: vi.fn(async () => ({ data: [] })),
  stageImport: vi.fn(async () => ({ data: {} })),
  createImportProfile: vi.fn(async () => ({ data: {} })),
  listImportBatches: vi.fn(async () => ({ data: [] })),
  listInstruments: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000b1",
        kind: "fiat",
        symbol: "GBP",
        name: "Pound sterling",
        decimals: 2,
        quote_instrument_id: null,
        hidden: false,
        identifiers: [],
      },
    ],
  })),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
  recordSnapshot: vi.fn(async () => ({ data: {} })),
  createAccount: vi.fn(async () => ({ data: {} })),
  listAccountIdentifiers: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000d1",
        kind: "uk_bank_account",
        value: "112233:12345678",
        sort_code: "112233",
        account_number: "12345678",
        account_id: "0190a0b0-0000-7000-8000-000000000002",
        dismissed_at: null,
        created_by: "user",
        created_at: "2026-11-15T09:00:00Z",
      },
    ],
  })),
  addAccountIdentifier: vi.fn(async () => ({ data: {} })),
  removeAccountIdentifier: vi.fn(async () => ({ data: {} })),
  setAccountBalanceMode: vi.fn(async () => ({ data: {} })),
  setMainAccount: vi.fn(async () => ({ data: {} })),
  setAccountReporting: vi.fn(async () => ({ data: {} })),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AccountsPage />
    </QueryClientProvider>,
  );
}

// An account's card, found by its name.
async function card(name: string) {
  return within(await screen.findByRole("listitem", { name }));
}

async function creditCardForm() {
  renderPage();
  await userEvent.setup().click((await card("Credit card")).getByRole("button", { name: "Update balance" }));
  return within(await screen.findByRole("form", { name: "Snapshot for Credit card" }));
}

it("sends liability balances as negative quantities", async () => {
  const user = userEvent.setup();
  const form = await creditCardForm();
  await user.type(form.getByLabelText("Amount owed"), "500");
  await user.click(form.getByRole("button", { name: "Save balance" }));

  expect(recordSnapshot).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: "0190a0b0-0000-7000-8000-000000000001" },
      body: expect.objectContaining({
        instrument_id: "0190a0b0-0000-7000-8000-0000000000b1",
        quantity: "-500",
      }),
    }),
  );
});

it("rejects a negative amount owed instead of storing a positive liability", async () => {
  const user = userEvent.setup();
  const form = await creditCardForm();
  await user.type(form.getByLabelText("Amount owed"), "-500");
  await user.click(form.getByRole("button", { name: "Save balance" }));

  expect(
    form.getByText("Enter the amount owed as a positive number, e.g. 500"),
  ).toBeInTheDocument();
  expect(recordSnapshot).not.toHaveBeenCalled();
});

it("shows a failed account types fetch instead of leaving the page silent", async () => {
  vi.mocked(listAccountTypes).mockRejectedValueOnce(new Error("account types unavailable"));
  renderPage();
  expect(await screen.findByText("account types unavailable")).toBeInTheDocument();
});

it("offers no balance to type for accounts whose balance comes from transactions", async () => {
  renderPage();
  const ledger = await card("Ledger account");
  expect(ledger.queryByRole("button", { name: "Update balance" })).toBeNull();
  expect((await card("Credit card")).getByRole("button", { name: "Update balance" })).toBeInTheDocument();
});

it("slides a panel open and shut, one at a time", async () => {
  const user = userEvent.setup();
  renderPage();
  const credit = await card("Credit card");
  const balance = credit.getByRole("button", { name: "Update balance" });
  const bank = credit.getByRole("button", { name: "Bank details" });
  const panel = document.getElementById(balance.getAttribute("aria-controls")!)!;
  expect(panel).toHaveAttribute("data-open", "false");

  await user.click(balance);
  expect(balance).toHaveAttribute("aria-expanded", "true");
  expect(panel).toHaveAttribute("data-open", "true");

  await user.click(bank);
  expect(balance).toHaveAttribute("aria-expanded", "false");
  expect(panel).toHaveAttribute("data-open", "false");
  expect(bank).toHaveAttribute("aria-expanded", "true");
});

async function identifiersOf(name: string) {
  const user = userEvent.setup();
  renderPage();
  await user.click((await card(name)).getByRole("button", { name: "Bank details" }));
  const region = await screen.findByRole("group", { name: `Bank details of ${name}` });
  return { user, region: within(region) };
}

it("adds and removes an identifier", async () => {
  const { user, region } = await identifiersOf("Ledger account");
  expect(await region.findByText("11-22-33 · 12345678")).toBeInTheDocument();

  await user.type(region.getByLabelText("Sort code"), "65-43-21");
  await user.type(region.getByLabelText("Account number"), "87654321");
  await user.click(region.getByRole("button", { name: "Add" }));
  expect(addAccountIdentifier).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: "0190a0b0-0000-7000-8000-000000000002" },
      body: { kind: "uk_bank_account", value: "65-43-21:87654321" },
    }),
  );
  await vi.waitFor(() => expect(listAccountIdentifiers).toHaveBeenCalledTimes(2));

  await user.click(region.getByRole("button", { name: "Remove" }));
  expect(removeAccountIdentifier).toHaveBeenCalledWith(
    expect.objectContaining({
      path: {
        account_id: "0190a0b0-0000-7000-8000-000000000002",
        identifier_id: "0190a0b0-0000-7000-8000-0000000000d1",
      },
    }),
  );
});

it("adds a chain address as a single value", async () => {
  const { user, region } = await identifiersOf("Ledger account");
  await user.click(await region.findByRole("combobox", { name: "Kind" }));
  await user.click(await screen.findByRole("option", { name: "Chain address" }));
  expect(region.queryByLabelText("Sort code")).toBeNull();
  expect(region.queryByLabelText("Account number")).toBeNull();
  await user.type(region.getByLabelText("Chain address"), `0x${"A".repeat(40)}`);
  await user.click(region.getByRole("button", { name: "Add" }));
  expect(addAccountIdentifier).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: "0190a0b0-0000-7000-8000-000000000002" },
      body: { kind: "chain_address", value: `0x${"A".repeat(40)}` },
    }),
  );
});

it("shows the API's message when a chain address is refused", async () => {
  vi.mocked(addAccountIdentifier).mockRejectedValueOnce({
    detail: { error: "invalid", message: "not a valid chain address" },
  });
  const { user, region } = await identifiersOf("Ledger account");
  await user.click(await region.findByRole("combobox", { name: "Kind" }));
  await user.click(await screen.findByRole("option", { name: "Chain address" }));
  await user.type(region.getByLabelText("Chain address"), "nope");
  await user.click(region.getByRole("button", { name: "Add" }));
  expect(await region.findByText("not a valid chain address")).toBeInTheDocument();
});

it("offers the hybrid switch for a snapshots account", async () => {
  const { user, region } = await identifiersOf("Credit card");
  expect(
    region.getByText(
      "Bank details need this account to take transactions as well as typed balances",
    ),
  ).toBeInTheDocument();
  expect(region.queryByLabelText("Sort code")).toBeNull();
  await user.click(region.getByRole("button", { name: "Allow transactions" }));
  expect(setAccountBalanceMode).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: "0190a0b0-0000-7000-8000-000000000001" },
      body: { balance_mode: "hybrid" },
    }),
  );
});

it("offers no identifiers on In transit", async () => {
  const account = {
    institution_entity_id: null,
    owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
    tax_wrapper: "none",
    parent_account_id: null,
    source_kind: "manual",
    source_connection_id: null,
    status: "open",
    opened_on: null,
    closed_on: null,
    ledger_type: "asset",
    subtype: "in_transit",
  };
  const inTransit = {
    ...account,
    id: "0190a0b0-0000-7000-8000-000000000003",
    name: "In transit",
    balance_mode: "transactions",
    system: true,
  };
  // An owner's account of the same subtype is theirs, and can have identifiers.
  const posted = {
    ...account,
    id: "0190a0b0-0000-7000-8000-000000000004",
    name: "Cash in the post",
    balance_mode: "hybrid",
    system: false,
  };
  const { data } = await listAccounts();
  vi.mocked(listAccounts).mockResolvedValueOnce({
    data: [...(data ?? []), inTransit, posted],
  } as never);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  expect((await card("Cash in the post")).getByRole("button", { name: "Bank details" })).toBeInTheDocument();
  // The ledger's own account is listed, but can't have bank details.
  expect((await card("In transit")).queryByRole("button", { name: "Bank details" })).toBeNull();
});

it("sets a manual account's main account", async () => {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  await user.click((await card("Credit card")).getByRole("button", { name: "Main account" }));
  const group = within(await screen.findByRole("group", { name: "Main account of Credit card" }));
  await user.click(group.getByRole("combobox", { name: "Main account" }));
  // The account itself isn't offered as its own main account.
  expect(screen.queryByRole("option", { name: "Credit card" })).toBeNull();
  await user.click(await screen.findByRole("option", { name: "Ledger account" }));
  expect(setMainAccount).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: "0190a0b0-0000-7000-8000-000000000001" },
      body: { main_account_id: "0190a0b0-0000-7000-8000-000000000002" },
    }),
  );
  await vi.waitFor(() => expect(listAccounts).toHaveBeenCalledTimes(2));
});

it("offers as main accounts only assets and liabilities whose postings count", async () => {
  const base = {
    institution_entity_id: null,
    owner_entity_id: "0190a0b0-0000-7000-8000-0000000000aa",
    tax_wrapper: "none",
    parent_account_id: null,
    source_kind: "manual",
    source_connection_id: null,
    status: "open",
    opened_on: null,
    closed_on: null,
    system: false,
  };
  const current = {
    ...base,
    id: "0190a0b0-0000-7000-8000-000000000005",
    name: "Current account",
    ledger_type: "asset",
    subtype: "bank",
    balance_mode: "hybrid",
  };
  const rent = {
    ...base,
    id: "0190a0b0-0000-7000-8000-000000000006",
    name: "Rent",
    ledger_type: "expense",
    subtype: "other",
    balance_mode: "transactions",
  };
  const { data } = await listAccounts();
  vi.mocked(listAccounts).mockResolvedValueOnce({
    data: [...(data ?? []), current, rent],
  } as never);
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  await user.click((await card("Ledger account")).getByRole("button", { name: "Main account" }));
  const group = within(
    await screen.findByRole("group", { name: "Main account of Ledger account" }),
  );
  await user.click(group.getByRole("combobox", { name: "Main account" }));
  expect(await screen.findByRole("option", { name: "Current account" })).toBeTruthy();
  // Valuation ignores a snapshots-only account's postings (Credit card), and an expense
  // account (Rent) is none of the owner's money: the API refuses both.
  expect(screen.queryByRole("option", { name: "Credit card" })).toBeNull();
  expect(screen.queryByRole("option", { name: "Rent" })).toBeNull();
});

it("hides an account from the Overview", async () => {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  await user.click(
    await screen.findByRole("button", { name: "Hide Credit card from Overview" }),
  );

  expect(setAccountReporting).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: "0190a0b0-0000-7000-8000-000000000001" },
      body: { excluded: true },
    }),
  );
});

it("links to adding an account", async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  expect(await screen.findByRole("link", { name: "Add account" })).toHaveAttribute(
    "href",
    "/accounts/new",
  );
});

it("adds an account from plain answers", async () => {
  const user = userEvent.setup();
  const onAdded = vi.fn();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NewAccountForm onAdded={onAdded} />
    </QueryClientProvider>,
  );
  const form = within(await screen.findByRole("form", { name: "New account" }));
  await user.type(form.getByLabelText("Name"), "Car loan");
  await user.click(form.getByRole("button", { name: "Money I owe" }));
  await user.click(form.getByRole("button", { name: "I'll type the balance" }));
  await user.click(form.getByRole("button", { name: "Add account" }));

  expect(createAccount).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        name: "Car loan",
        ledger_type: "liability",
        subtype: "bank",
        balance_mode: "snapshots",
        institution_name: null,
        tax_wrapper: "none",
      },
    }),
  );
  await waitFor(() => expect(onAdded).toHaveBeenCalled());
});

it("uploads a statement from an account of a statement type, with no account to pick", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click((await card("Cash ISA")).getByRole("button", { name: "Upload file" }));
  const form = within(await screen.findByRole("form", { name: /upload/i }));
  expect(form.queryByLabelText("Account")).toBeNull();
});

it("links a connected account's file to Connections", async () => {
  renderPage();
  const link = (await card("Invest")).getByRole("link", { name: "Upload file" });
  expect(link).toHaveAttribute("href", "/connections");
});

it("offers no upload without a known type, or on a closed account", async () => {
  renderPage();
  for (const name of ["Untyped", "Gone type", "Closed ISA"]) {
    const account = await card(name);
    expect(account.queryByRole("button", { name: "Upload file" })).toBeNull();
    expect(account.queryByRole("link", { name: "Upload file" })).toBeNull();
  }
});
