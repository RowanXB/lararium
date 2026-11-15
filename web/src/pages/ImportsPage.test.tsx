import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import { stageImport } from "../api";
import { ImportsPage } from "./ImportsPage";

const GBP_ID = "0190a0b0-0000-7000-8000-0000000000b1";
const CURRENT_ID = "0190a0b0-0000-7000-8000-000000000001";
const PROFILE_ID = "0190a0b0-0000-7000-8000-0000000000d1";
const BATCH_ID = "0190a0b0-0000-7000-8000-0000000000e1";

const navigate = vi.fn();

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
  Link: ({ children }: { children: ReactNode }) => <a href="#">{children}</a>,
}));

function account(id: string, name: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    name,
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
    ...overrides,
  };
}

vi.mock("../api", () => ({
  listAccounts: vi.fn(async () => ({
    data: [
      account("0190a0b0-0000-7000-8000-000000000001", "Example Bank current"),
      account("0190a0b0-0000-7000-8000-000000000002", "Example Bank savings", {
        balance_mode: "hybrid",
      }),
      account("0190a0b0-0000-7000-8000-000000000003", "Example pension", {
        balance_mode: "snapshots",
      }),
      account("0190a0b0-0000-7000-8000-000000000004", "Old Example account", {
        status: "closed",
      }),
      account("0190a0b0-0000-7000-8000-000000000005", "Connected Example", {
        source_kind: "connection",
      }),
    ],
  })),
  listInstruments: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000b0",
        kind: "fiat",
        symbol: "EUR",
        name: "Euro",
        decimals: 2,
        quote_instrument_id: null,
        hidden: false,
        identifiers: [{ scheme: "iso4217", value: "EUR" }],
      },
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
      {
        id: "0190a0b0-0000-7000-8000-0000000000c1",
        kind: "crypto",
        symbol: "BTC",
        name: "Bitcoin",
        decimals: 8,
        quote_instrument_id: null,
        hidden: false,
        identifiers: [],
      },
    ],
  })),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
  listImportProfiles: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000d1",
        name: "Example Bank CSV",
        version: 1,
        updated_at: "2026-11-01T10:00:00Z",
        config: {
          format: "csv",
          columns: { date: "Date", description: ["Description"], amount: "Amount" },
          date_format: "%d/%m/%Y",
        },
      },
    ],
  })),
  listImportBatches: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000e0",
        account_id: "0190a0b0-0000-7000-8000-000000000001",
        profile_id: "0190a0b0-0000-7000-8000-0000000000d1",
        profile_version: 1,
        document_id: "0190a0b0-0000-7000-8000-0000000000f0",
        filename: "example-october.csv",
        status: "committed",
        counts: { committed: 12, duplicate: 1 },
        warnings: [],
        statement: null,
        created_at: "2026-11-01T10:00:00Z",
        committed_at: "2026-11-01T10:05:00Z",
        rolled_back_at: null,
      },
    ],
  })),
  stageImport: vi.fn(async () => ({ data: { id: "0190a0b0-0000-7000-8000-0000000000e1" } })),
}));

beforeEach(() => {
  vi.mocked(stageImport).mockClear();
  navigate.mockClear();
});

async function uploadForm() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ImportsPage />
    </QueryClientProvider>,
  );
  return within(await screen.findByRole("form", { name: "Upload statement" }));
}

async function choose(form: ReturnType<typeof within>, label: string, option: string) {
  const user = userEvent.setup();
  await user.click(form.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}

it("test_upload_sends_form_fields", async () => {
  const user = userEvent.setup();
  const form = await uploadForm();
  await waitFor(() =>
    expect(form.getByRole("combobox", { name: "Currency" })).toHaveTextContent("GBP"),
  );
  await choose(form, "Account", "Example Bank current");
  await choose(form, "Profile", "Example Bank CSV");
  const file = new File(["Date,Description,Amount\n"], "example.csv", { type: "text/csv" });
  await user.upload(form.getByLabelText("File"), file);
  await user.click(form.getByRole("button", { name: "Upload" }));

  expect(stageImport).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        account_id: CURRENT_ID,
        profile_id: PROFILE_ID,
        instrument_id: GBP_ID,
        file,
      },
    }),
  );
  await waitFor(() =>
    expect(navigate).toHaveBeenCalledWith({
      to: "/imports/$batchId",
      params: { batchId: BATCH_ID },
    }),
  );
});

it("test_only_importable_accounts_listed", async () => {
  const user = userEvent.setup();
  const form = await uploadForm();
  await user.click(form.getByRole("combobox", { name: "Account" }));
  const options = (await screen.findAllByRole("option")).map((o) => o.textContent);

  expect(options).toEqual(["Example Bank current", "Example Bank savings"]);
});

it("shows the server message when staging fails", async () => {
  vi.mocked(stageImport).mockRejectedValueOnce({
    detail: { error: "file_format", message: "the file is empty" },
  });
  const user = userEvent.setup();
  const form = await uploadForm();
  await choose(form, "Account", "Example Bank current");
  await choose(form, "Profile", "Example Bank CSV");
  await user.upload(form.getByLabelText("File"), new File([""], "empty.csv"));
  await user.click(form.getByRole("button", { name: "Upload" }));

  expect(await form.findByText("the file is empty")).toBeInTheDocument();
  expect(navigate).not.toHaveBeenCalled();
});

it("lists batches with their account and counts", async () => {
  await uploadForm();
  const row = (await screen.findByText("example-october.csv")).closest("tr");
  expect(row).not.toBeNull();
  const cells = within(row as HTMLElement);
  expect(cells.getByText("Example Bank current")).toBeInTheDocument();
  expect(cells.getByText("Committed")).toBeInTheDocument();
  expect(cells.getByText("12 committed · 1 duplicate")).toBeInTheDocument();
});
