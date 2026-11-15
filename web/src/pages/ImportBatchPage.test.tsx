import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import {
  commitImport,
  discardImport,
  getImportBatch,
  type ImportBatchDetailOut,
  type ImportRowOut,
  listInstruments,
  rollbackImport,
} from "../api";
import { ImportBatchPage } from "./ImportBatchPage";

const BATCH_ID = "0190a0b0-0000-7000-8000-0000000000e1";
const ACCOUNT_ID = "0190a0b0-0000-7000-8000-000000000001";
const GBP_ID = "0190a0b0-0000-7000-8000-0000000000b1";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: ReactNode }) => <a href="#">{children}</a>,
}));

vi.mock("../api", () => ({
  getImportBatch: vi.fn(),
  listAccounts: vi.fn(async () => ({ data: [] })),
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
        identifiers: [{ scheme: "iso4217", value: "GBP" }],
      },
    ],
  })),
  commitImport: vi.fn(async () => ({ data: {} })),
  discardImport: vi.fn(async () => ({ data: {} })),
  rollbackImport: vi.fn(async () => ({ data: {} })),
}));

function row(position: number, overrides: Partial<ImportRowOut> = {}): ImportRowOut {
  return {
    id: `0190a0b0-0000-7000-8000-0000000001${String(position + 10).padStart(2, "0")}`,
    position,
    kind: "statement",
    status: "new",
    booking_date: "2026-10-03",
    occurred_at: "2026-10-02T23:00:00Z",
    value_date: null,
    description: "Example Coffee",
    payee: null,
    memo: null,
    check_number: null,
    counterparty_name: null,
    counterparty_sort_code: null,
    counterparty_account_number: null,
    counterparty_bank: null,
    payment_reference: null,
    payment_kind: null,
    bank_reference: null,
    purpose_code: null,
    remittance_info: null,
    bank_type: null,
    quantity: "-3.50",
    fee: null,
    balance: null,
    instrument_id: GBP_ID,
    external_id: null,
    duplicate_of: null,
    is_gap: null,
    error: null,
    note: null,
    raw: {},
    transaction_id: null,
    ...overrides,
  };
}

function batch(overrides: Partial<ImportBatchDetailOut> = {}): ImportBatchDetailOut {
  return {
    id: BATCH_ID,
    account_id: ACCOUNT_ID,
    profile_id: "0190a0b0-0000-7000-8000-0000000000d1",
    profile_version: 1,
    document_id: "0190a0b0-0000-7000-8000-0000000000f0",
    filename: "example-october.csv",
    status: "staged",
    counts: { new: 1 },
    warnings: [],
    statement: null,
    created_at: "2026-11-01T10:00:00Z",
    committed_at: null,
    rolled_back_at: null,
    rows: [row(0)],
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(commitImport).mockClear();
  vi.mocked(discardImport).mockClear();
  vi.mocked(rollbackImport).mockClear();
});

function show(detail: ImportBatchDetailOut) {
  vi.mocked(getImportBatch).mockResolvedValue({ data: detail } as never);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ImportBatchPage batchId={BATCH_ID} />
    </QueryClientProvider>,
  );
}

it("test_commit_disabled_with_errors", async () => {
  show(
    batch({
      counts: { new: 1, error: 1 },
      rows: [
        row(0),
        row(1, {
          status: "error",
          quantity: null,
          instrument_id: null,
          error: "bad date: 31/02/2026",
        }),
      ],
    }),
  );

  expect(await screen.findByRole("button", { name: "Commit" })).toBeDisabled();
  expect(
    screen.getByText("Fix the errors in the file or profile and upload again"),
  ).toBeInTheDocument();
  expect(screen.getByText("bad date: 31/02/2026")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Discard" })).toBeEnabled();
});

it("commits a batch without errors", async () => {
  const user = userEvent.setup();
  show(batch());
  await user.click(await screen.findByRole("button", { name: "Commit" }));
  expect(commitImport).toHaveBeenCalledWith(
    expect.objectContaining({ path: { batch_id: BATCH_ID } }),
  );
});

it("test_rollback_requires_confirmation", async () => {
  const user = userEvent.setup();
  show(
    batch({
      status: "committed",
      committed_at: "2026-11-01T10:05:00Z",
      counts: { committed: 2 },
      rows: [
        row(0, { status: "committed", transaction_id: "0190a0b0-0000-7000-8000-000000000201" }),
        row(1, { status: "committed", transaction_id: "0190a0b0-0000-7000-8000-000000000202" }),
      ],
    }),
  );

  await user.click(await screen.findByRole("button", { name: "Roll back" }));
  expect(rollbackImport).not.toHaveBeenCalled();
  expect(screen.getByText("This voids 2 transactions")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "Confirm roll back" }));
  expect(rollbackImport).toHaveBeenCalledWith(
    expect.objectContaining({ path: { batch_id: BATCH_ID } }),
  );
});

it("test_amounts_rendered_from_strings", async () => {
  show(batch({ rows: [row(0, { quantity: "-1234.56" })] }));
  expect(await screen.findByText("-£1,234.56")).toBeInTheDocument();
});

it("shows warnings, the statement summary and row details", async () => {
  const warning =
    "committing this changes the opening adjustment of example-november.csv from 10.00 to 0";
  show(
    batch({
      warnings: [warning],
      statement: {
        period_from: "2026-10-01",
        period_to: "2026-10-31",
        currency: "GBP",
        opening_balance: "100.00",
        closing_balance: "96.50",
        closing_balance_date: "2026-10-31",
        money_in: "0.00",
        money_out: "3.50",
        payments_in: null,
        payments_out: null,
        account_number: "12345678",
        sort_code: "00-00-00",
        account_name: null,
        account_type: null,
        available_balance: null,
        available_balance_date: null,
        bic: null,
        branch: null,
        clearing_account_number: null,
        customer_number: null,
        document_date: null,
        iban: null,
        overdraft_limit: null,
        overdraft_limit_date: null,
        statement_reference: null,
        rates: [],
      },
      rows: [
        row(0, {
          payee: "Example Coffee Ltd",
          counterparty_name: "Example Payee",
          payment_reference: "REF 42",
          bank_type: "DEB",
        }),
      ],
    }),
  );

  expect(await screen.findByText(warning)).toBeInTheDocument();
  expect(screen.getByText("2026-10-01 to 2026-10-31")).toBeInTheDocument();
  expect(screen.getByText("£100.00")).toBeInTheDocument();
  expect(screen.getByText("£96.50")).toBeInTheDocument();
  expect(screen.getByText("Account ending 5678")).toBeInTheDocument();
  expect(screen.getByText(/Payee: Example Coffee Ltd/)).toBeInTheDocument();
  expect(screen.getByText(/Counterparty: Example Payee/)).toBeInTheDocument();
  expect(screen.getByText(/Type: DEB/)).toBeInTheDocument();
});

const adjustment = row(-1, {
  kind: "opening_adjustment",
  description: "Opening balance adjustment",
  quantity: "250.00",
  booking_date: "2026-09-30",
  is_gap: false,
});

it("shows the opening adjustment first", async () => {
  show(batch({ rows: [row(0), adjustment] }));
  const label = await screen.findByText("Opening balance adjustment");
  const body = label.closest("tbody") as HTMLElement;
  expect(within(body).getAllByRole("row")[0]).toHaveTextContent("Opening balance adjustment");
});

it("labels the adjustment as a gap when the server says it follows earlier history", async () => {
  show(batch({ rows: [row(0), { ...adjustment, is_gap: true }] }));
  expect(
    await screen.findByText("Balance gap: a statement period may be missing"),
  ).toBeInTheDocument();
  expect(screen.queryByText("Opening balance adjustment")).toBeNull();
});

it("shows a row's fee in its details", async () => {
  show(batch({ rows: [row(0, { quantity: "-100.00", fee: "1.50" })] }));
  expect(await screen.findByText(/Fee: £1\.50/)).toBeInTheDocument();
});

it("formats rows in hidden currencies too", async () => {
  vi.mocked(listInstruments).mockImplementationOnce((async (options?: {
    query?: { include_hidden?: boolean };
  }) => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000b2",
        kind: "fiat",
        symbol: "USD",
        name: "US dollar",
        decimals: 2,
        quote_instrument_id: null,
        hidden: true,
        identifiers: [{ scheme: "iso4217", value: "USD" }],
      },
    ].filter((instrument) => options?.query?.include_hidden || !instrument.hidden),
  })) as never);
  show(
    batch({
      rows: [row(0, { quantity: "-12.30", instrument_id: "0190a0b0-0000-7000-8000-0000000000b2" })],
    }),
  );
  expect(await screen.findByText("-US$12.30")).toBeInTheDocument();
});

it("discards a staged batch", async () => {
  const user = userEvent.setup();
  show(batch());
  await user.click(await screen.findByRole("button", { name: "Discard" }));
  expect(discardImport).toHaveBeenCalledWith(
    expect.objectContaining({ path: { batch_id: BATCH_ID } }),
  );
});

it("shows the note on a skipped row", async () => {
  show(
    batch({
      counts: { skipped: 1 },
      rows: [row(0, { status: "skipped", quantity: "0.00", note: "zero amount" })],
    }),
  );
  expect(await screen.findByText("zero amount")).toBeInTheDocument();
});
