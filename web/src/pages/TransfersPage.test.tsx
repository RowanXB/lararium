import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  addAccountIdentifier,
  listAccounts,
  listTransferCandidates,
  listTransfers,
  pairTransfer,
  rejectTransfer,
  unpairTransfer,
  unrejectTransfer,
} from "../api";
import { TransfersPage } from "./TransfersPage";

const MONZO = "0190a0b0-0000-7000-8000-000000000001";
const BANK_A = "0190a0b0-0000-7000-8000-000000000002";

function transfer(overrides: Record<string, unknown>) {
  return {
    id: "0190a0b0-0000-7000-8000-0000000000f0",
    status: "awaiting",
    review_reason: null,
    overdue: false,
    days_in_transit: null,
    from_account_id: MONZO,
    from_account_name: "Monzo",
    to_account_id: BANK_A,
    to_account_name: "Bank A",
    instrument_id: "0190a0b0-0000-7000-8000-0000000000b1",
    currency: "GBP",
    amount: "100.000000000000000000",
    out_transaction_id: "0190a0b0-0000-7000-8000-0000000000c1",
    in_transaction_id: null,
    placeholder_transaction_id: null,
    opened_at: "2026-11-10T09:00:00Z",
    matched_at: null,
    ...overrides,
  };
}

const TRANSFERS = [
  transfer({
    id: "0190a0b0-0000-7000-8000-0000000000f1",
    review_reason: "several_candidates",
  }),
  transfer({
    id: "0190a0b0-0000-7000-8000-0000000000f2",
    amount: "25.5",
    days_in_transit: 3,
  }),
  transfer({
    id: "0190a0b0-0000-7000-8000-0000000000f3",
    status: "inferred",
    placeholder_transaction_id: "0190a0b0-0000-7000-8000-0000000000c3",
  }),
  transfer({
    id: "0190a0b0-0000-7000-8000-0000000000f4",
    status: "matched",
    amount: "40",
    in_transaction_id: "0190a0b0-0000-7000-8000-0000000000c4",
  }),
];

const CANDIDATE = {
  transaction_id: "0190a0b0-0000-7000-8000-0000000000e1",
  account_id: BANK_A,
  account_name: "Bank A",
  occurred_at: "2026-11-11T09:00:00Z",
  quantity: "100",
  description: "From Monzo",
  reason: "exact",
};

const SUGGESTION = {
  identifier_id: "0190a0b0-0000-7000-8000-0000000000d1",
  sort_code: "112233",
  account_number: "12345678",
  names: ["Test Owner"],
  outgoing: 2,
  incoming: 1,
  latest: "2026-11-12T09:00:00Z",
};

vi.mock("../api", () => ({
  listTransfers: vi.fn(),
  listTransferCandidates: vi.fn(),
  listTransferSuggestions: vi.fn(async () => ({ data: [SUGGESTION] })),
  listAccounts: vi.fn(async () => ({
    data: [
      {
        id: MONZO,
        name: "Monzo",
        ledger_type: "asset",
        subtype: "bank",
        balance_mode: "transactions",
        status: "open",
        system: false,
      },
      {
        id: BANK_A,
        name: "Bank A",
        ledger_type: "asset",
        subtype: "bank",
        balance_mode: "hybrid",
        status: "open",
        system: false,
      },
      {
        id: "0190a0b0-0000-7000-8000-000000000006",
        name: "Lloyds",
        ledger_type: "asset",
        subtype: "bank",
        balance_mode: "snapshots",
        status: "open",
        system: false,
      },
      {
        id: "0190a0b0-0000-7000-8000-000000000004",
        name: "In transit",
        ledger_type: "asset",
        subtype: "in_transit",
        balance_mode: "transactions",
        status: "open",
        system: true,
      },
      {
        id: "0190a0b0-0000-7000-8000-000000000005",
        name: "Cash in the post",
        ledger_type: "asset",
        subtype: "in_transit",
        balance_mode: "hybrid",
        status: "open",
        system: false,
      },
      {
        id: "0190a0b0-0000-7000-8000-000000000003",
        name: "Groceries",
        ledger_type: "expense",
        subtype: "other",
        balance_mode: "transactions",
        status: "open",
        system: false,
      },
    ],
  })),
  pairTransfer: vi.fn(async () => ({ data: {} })),
  rejectTransfer: vi.fn(async () => ({ data: {} })),
  unrejectTransfer: vi.fn(async () => ({ data: {} })),
  unpairTransfer: vi.fn(async () => ({ data: {} })),
  addAccountIdentifier: vi.fn(async () => ({ data: {} })),
  dismissTransferSuggestion: vi.fn(async () => ({ data: {} })),
}));

beforeEach(() => {
  vi.mocked(listTransfers).mockResolvedValue({ data: TRANSFERS } as never);
  vi.mocked(listTransferCandidates).mockResolvedValue({ data: [CANDIDATE] } as never);
});

afterEach(() => {
  vi.clearAllMocks();
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <TransfersPage />
    </QueryClientProvider>,
  );
}

async function section(name: string) {
  return within(await screen.findByRole("region", { name }));
}

it("renders sections from the API", async () => {
  renderPage();
  const attention = await section("Needs attention");
  expect(await attention.findByText("Monzo → Bank A · 100.00 GBP")).toBeInTheDocument();
  expect(attention.getByText("Several candidates")).toBeInTheDocument();
  expect(await attention.findByText(/From Monzo/)).toBeInTheDocument();

  const inTransit = await section("In transit");
  expect(
    inTransit.getByText("Monzo → Bank A · 25.50 GBP · 3 days in transit"),
  ).toBeInTheDocument();

  const suggestions = await section("Suggestions");
  expect(await suggestions.findByText("11-22-33 · 12345678")).toBeInTheDocument();

  const recent = await section("Recent");
  expect(recent.getByText("Recent (2)").closest("details")).not.toHaveAttribute("open");
  expect(recent.getByText("Monzo → Bank A · 100.00 GBP · inferred")).toBeInTheDocument();
  expect(recent.getByText("Monzo → Bank A · 40.00 GBP · matched")).toBeInTheDocument();
});

it("pairs a candidate", async () => {
  const user = userEvent.setup();
  renderPage();
  const attention = await section("Needs attention");
  await user.click(await attention.findByRole("button", { name: "Pair" }));

  expect(pairTransfer).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { transfer_id: "0190a0b0-0000-7000-8000-0000000000f1" },
      body: { transaction_id: CANDIDATE.transaction_id },
    }),
  );
  // The transfer's id may change after a write, so the lists are fetched again.
  await vi.waitFor(() => expect(listTransfers).toHaveBeenCalledTimes(2));
});

it("rejects a transfer", async () => {
  const user = userEvent.setup();
  renderPage();
  const recent = await section("Recent");
  await user.click(await recent.findByText("Recent (2)"));
  const inferred = recent.getByText("Monzo → Bank A · 100.00 GBP · inferred").closest("li")!;
  await user.click(within(inferred).getByRole("button", { name: "Reject" }));
  expect(rejectTransfer).toHaveBeenCalledWith(
    expect.objectContaining({ path: { transfer_id: "0190a0b0-0000-7000-8000-0000000000f3" } }),
  );

  const matched = recent.getByText("Monzo → Bank A · 40.00 GBP · matched").closest("li")!;
  await user.click(within(matched).getByRole("button", { name: "Unpair" }));
  expect(unpairTransfer).toHaveBeenCalledWith(
    expect.objectContaining({ path: { transfer_id: "0190a0b0-0000-7000-8000-0000000000f4" } }),
  );
});

it("undoes a rejection whose records were all rolled back", async () => {
  const rejected = transfer({
    id: "0190a0b0-0000-7000-8000-0000000000f5",
    status: "rejected",
    out_transaction_id: null,
    in_transaction_id: null,
  });
  vi.mocked(listTransfers).mockResolvedValue({ data: [rejected] } as never);
  const user = userEvent.setup();
  renderPage();
  const recent = await section("Recent");
  await user.click(await recent.findByText("Recent (1)"));
  const item = recent.getByText("Monzo → Bank A · 100.00 GBP · rejected").closest("li")!;
  await user.click(within(item).getByRole("button", { name: "Undo" }));
  expect(unrejectTransfer).toHaveBeenCalledWith(
    expect.objectContaining({ path: { transfer_id: "0190a0b0-0000-7000-8000-0000000000f5" } }),
  );
});

it("shows the API's message when a write fails", async () => {
  vi.mocked(rejectTransfer).mockRejectedValueOnce({
    detail: { error: "query_timeout", message: "The ledger is busy; try again" },
  });
  const user = userEvent.setup();
  renderPage();
  const attention = await section("Needs attention");
  await user.click(await attention.findByRole("button", { name: "Reject" }));
  expect(await attention.findByText("The ledger is busy; try again")).toBeInTheDocument();
});

it("registers a suggestion against an account", async () => {
  const user = userEvent.setup();
  renderPage();
  const suggestions = await section("Suggestions");
  await user.click(await suggestions.findByRole("combobox", { name: "Account" }));
  // Only asset and liability accounts can own a bank account.
  expect(screen.queryByRole("option", { name: "Groceries" })).toBeNull();
  // In transit is the ledger's own account, which can't have identifiers; an owner's
  // account of the same subtype can.
  expect(screen.queryByRole("option", { name: "In transit" })).toBeNull();
  expect(screen.getByRole("option", { name: "Cash in the post" })).toBeInTheDocument();
  // A snapshots-only account can't have identifiers (the API refuses them); the section
  // says so once, naming it, instead of offering it.
  expect(screen.queryByRole("option", { name: "Lloyds" })).toBeNull();
  expect(screen.getByRole("option", { name: "Monzo" })).toBeInTheDocument();
  expect(
    suggestions.getByText(
      "Not listed: Lloyds. Snapshots-only accounts can't have identifiers; switch them to hybrid on Accounts.",
    ),
  ).toBeInTheDocument();
  await user.click(await screen.findByRole("option", { name: "Bank A" }));
  await user.click(suggestions.getByRole("button", { name: "Register" }));

  expect(addAccountIdentifier).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { account_id: BANK_A },
      body: { kind: "uk_bank_account", value: "112233:12345678" },
    }),
  );
});

it("shows no snapshots hint when every account can have identifiers", async () => {
  const accounts = (await listAccounts()).data as { balance_mode: string }[];
  vi.mocked(listAccounts).mockResolvedValueOnce({
    data: accounts.filter((a) => a.balance_mode !== "snapshots"),
  } as never);
  renderPage();
  const suggestions = await section("Suggestions");
  expect(await suggestions.findByText("11-22-33 · 12345678")).toBeInTheDocument();
  expect(suggestions.queryByText(/Not listed/)).toBeNull();
});

it("pairs again a record the owner unpaired", async () => {
  vi.mocked(listTransferCandidates).mockResolvedValue({
    data: [{ ...CANDIDATE, reason: "unpaired" }],
  } as never);
  const user = userEvent.setup();
  renderPage();
  // An unpaired transfer is in transit again; its candidates are a click away.
  const inTransit = await section("In transit");
  const item = (await inTransit.findByText(/25.50 GBP · 3 days in transit/)).closest("li")!;
  await user.click(within(item).getByText("Candidates"));
  expect(await within(item).findByText("Unpaired")).toBeInTheDocument();
  const pairButton = within(item).getByRole("button", { name: "Pair" });
  await vi.waitFor(() => expect(pairButton).toBeEnabled());
  await user.click(pairButton);

  expect(pairTransfer).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { transfer_id: "0190a0b0-0000-7000-8000-0000000000f2" },
      body: { transaction_id: CANDIDATE.transaction_id },
    }),
  );
});

it("disables every transfer action while a write is in flight", async () => {
  let finish: (value: unknown) => void = () => {};
  vi.mocked(pairTransfer).mockImplementationOnce(
    () => new Promise((resolve) => (finish = resolve)) as never,
  );
  const user = userEvent.setup();
  renderPage();
  const attention = await section("Needs attention");
  await user.click(await attention.findByRole("button", { name: "Pair" }));

  // Another item's transfer id may be gone once the write rematches, so it waits too.
  const recent = await section("Recent");
  await user.click(recent.getByText("Recent (2)"));
  const matched = recent.getByText("Monzo → Bank A · 40.00 GBP · matched").closest("li")!;
  expect(within(matched).getByRole("button", { name: "Unpair" })).toBeDisabled();
  expect(attention.getByRole("button", { name: "Reject" })).toBeDisabled();

  finish({ data: {} });
  await vi.waitFor(() =>
    expect(within(matched).getByRole("button", { name: "Unpair" })).toBeEnabled(),
  );
});

it("clears an item's error when another of its actions runs", async () => {
  vi.mocked(rejectTransfer).mockRejectedValueOnce({
    detail: { error: "query_timeout", message: "The ledger is busy; try again" },
  });
  const user = userEvent.setup();
  renderPage();
  const attention = await section("Needs attention");
  await user.click(await attention.findByRole("button", { name: "Reject" }));
  expect(await attention.findByText("The ledger is busy; try again")).toBeInTheDocument();

  await vi.waitFor(() => expect(attention.getByRole("button", { name: "Pair" })).toBeEnabled());
  await user.click(attention.getByRole("button", { name: "Pair" }));

  expect(attention.queryByText("The ledger is busy; try again")).toBeNull();
});
