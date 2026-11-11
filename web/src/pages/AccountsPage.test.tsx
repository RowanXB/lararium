import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { recordSnapshot } from "../api";
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
}));

beforeEach(() => {
  vi.mocked(recordSnapshot).mockClear();
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
