import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import {
  addAccountIdentifier,
  listAccountIdentifiers,
  listAccounts,
  recordSnapshot,
  removeAccountIdentifier,
  setAccountBalanceMode,
  setMainAccount,
} from "../api";
import { AccountsPage } from "./AccountsPage";

vi.mock("../api", () => ({
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
    ],
  })),
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
  createAccount: vi.fn(),
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
}));

beforeEach(() => {
  vi.clearAllMocks();
});

async function creditCardForm() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  return within(await screen.findByRole("form", { name: "Snapshot for Credit card" }));
}

it("sends liability balances as negative quantities", async () => {
  const user = userEvent.setup();
  const form = await creditCardForm();
  await user.type(form.getByLabelText("Amount owed"), "500");
  await user.click(form.getByRole("button", { name: "Save snapshot" }));

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
  await user.click(form.getByRole("button", { name: "Save snapshot" }));

  expect(
    form.getByText("Enter the amount owed as a positive number, e.g. 500"),
  ).toBeInTheDocument();
  expect(recordSnapshot).not.toHaveBeenCalled();
});

it("offers no snapshot form for accounts whose balance comes from transactions", async () => {
  await creditCardForm();
  expect(screen.queryByRole("form", { name: "Snapshot for Ledger account" })).toBeNull();
  expect(
    screen.getByText("Balance comes from transactions, so snapshots don't apply"),
  ).toBeInTheDocument();
});

async function identifiersOf(name: string) {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
  const region = await screen.findByRole("group", { name: `Identifiers of ${name}` });
  await user.click(within(region).getByText("Identifiers"));
  return { user, region: within(region) };
}

it("adds and removes an identifier", async () => {
  const { user, region } = await identifiersOf("Ledger account");
  expect(await region.findByText("11-22-33 · 12345678")).toBeInTheDocument();

  await user.type(region.getByLabelText("Sort code"), "65-43-21");
  await user.type(region.getByLabelText("Account number"), "87654321");
  await user.click(region.getByRole("button", { name: "Add identifier" }));
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

it("offers the hybrid switch for a snapshots account", async () => {
  const { user, region } = await identifiersOf("Credit card");
  expect(region.getByText("Switch to hybrid to register identifiers")).toBeInTheDocument();
  expect(region.queryByLabelText("Sort code")).toBeNull();
  await user.click(region.getByRole("button", { name: "Switch to hybrid" }));
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
  await screen.findByRole("group", { name: "Identifiers of Ledger account" });
  expect(
    screen.getByRole("group", { name: "Identifiers of Cash in the post" }),
  ).toBeInTheDocument();
  // The ledger's own account is listed, but can't have identifiers.
  expect(screen.getAllByText("In transit").length).toBeGreaterThan(0);
  expect(screen.queryByRole("group", { name: "Identifiers of In transit" })).toBeNull();
});

it("sets a manual account's main account", async () => {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AccountsPage />
    </QueryClientProvider>,
  );
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
