import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import {
  createAccount,
  createImportProfile,
  createTrading212Connection,
  listImportProfiles,
  stageImport,
} from "../api";
import { AddAccountPage } from "./AddAccountPage";

const navigate = vi.fn();

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
  Link: ({ children, to }: { children: ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

// HSBC UK's files as the backend catalog lists them.
const HSBC_CARD_TYPES = [")))", "VIS", "ATM", "CHQ"];
const HSBC_PDF = {
  label: "PDF statement",
  format: "pdf",
  recommended: true,
  config: { format: "pdf", layout: "hsbc_uk_statement", card_types: HSBC_CARD_TYPES },
};
const HSBC_FILES = [
  HSBC_PDF,
  {
    label: "midata CSV",
    format: "csv",
    recommended: false,
    config: {
      format: "csv",
      columns: {
        date: "Date",
        type: "Type",
        description: ["Merchant/Description"],
        amount: "Debit/Credit",
        balance: "Balance",
      },
      date_format: "%Y-%m-%d",
      row_order: "newest_first",
      skip_footer_rows: 1,
      overdraft_footer_label: "Arranged overdraft limit",
      amount_format: { thousands_separator: "," },
      card_types: HSBC_CARD_TYPES,
    },
  },
  { label: "OFX", format: "ofx", recommended: false, config: { format: "ofx" } },
  {
    label: "CSV (transaction list)",
    format: "csv",
    recommended: false,
    config: {
      format: "csv",
      header: ["Date", "Description", "Amount"],
      columns: { date: "Date", description: ["Description"], amount: "Amount" },
      date_format: "%d/%m/%Y",
      row_order: "newest_first",
      amount_format: { thousands_separator: "," },
    },
  },
  { label: "QIF", format: "qif", recommended: false, config: { format: "qif", date_format: "%d/%m/%Y" } },
];

const HISTORY_CSV = { label: "History CSV", format: "csv", recommended: false, config: null };
const CASH_ISA_CONFIG = {
  format: "csv",
  columns: {
    date: "Time (UTC)",
    type: "Action",
    description: ["Action"],
    memo: "Notes",
    external_id: "ID",
    amount: "Total",
    currency: "Currency (Total)",
  },
  date_format: "%Y-%m-%d %H:%M:%S%z",
  timezone: "UTC",
  card_types: [],
};

function type(fields: Record<string, unknown>) {
  return { label: "", shape: null, api: null, files: [], funding: [], family: null, ...fields };
}

const TYPES = [
  type({ key: "monzo", institution: "Monzo", name: "Monzo", api: { connector: "monzo", config: {} } }),
  type({
    key: "trading212.invest",
    institution: "Trading 212",
    label: "Invest",
    name: "Trading 212 Invest",
    api: { connector: "trading212", config: { account_kind: "invest" } },
    files: [HISTORY_CSV],
    family: "trading212",
  }),
  type({
    key: "trading212.stocks_isa",
    institution: "Trading 212",
    label: "Stocks ISA",
    name: "Trading 212 Stocks ISA",
    api: { connector: "trading212", config: { account_kind: "isa" } },
    files: [HISTORY_CSV],
    family: "trading212",
  }),
  type({
    key: "trading212.cash_isa",
    institution: "Trading 212",
    label: "Cash ISA",
    name: "Trading 212 Cash ISA",
    shape: { ledger_type: "asset", subtype: "savings_pot", tax_wrapper: "isa", balance_mode: "transactions" },
    files: [{ label: "History CSV", format: "csv", recommended: true, config: CASH_ISA_CONFIG }],
    family: "trading212",
  }),
  type({
    key: "hsbc_uk",
    institution: "HSBC UK",
    name: "HSBC UK",
    shape: { ledger_type: null, subtype: "bank", tax_wrapper: "none", balance_mode: "transactions" },
    files: HSBC_FILES,
  }),
];

vi.mock("../api", () => ({
  listAccountTypes: vi.fn(async () => ({ data: TYPES })),
  listImportProfiles: vi.fn(async () => ({ data: [] })),
  createImportProfile: vi.fn(async () => ({ data: { id: "profile-new" } })),
  listAccounts: vi.fn(async () => ({ data: [] })),
  listInstruments: vi.fn(async () => ({
    data: [{ id: "gbp", kind: "fiat", symbol: "GBP", identifiers: [] }],
  })),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
  createAccount: vi.fn(async () => ({ data: { id: "account-new" } })),
  stageImport: vi.fn(async () => ({ data: { id: "batch-1" } })),
  listConnections: vi.fn(async () => ({ data: [] })),
  createTrading212Connection: vi.fn(async () => ({ data: {} })),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listImportProfiles).mockResolvedValue({ data: [] } as never);
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <AddAccountPage />
    </QueryClientProvider>,
  );
}

it("lists the account types under their institutions", async () => {
  renderPage();

  for (const name of ["Trading 212", "Monzo", "HSBC UK"]) {
    expect(await screen.findByRole("heading", { name })).toBeInTheDocument();
  }
  expect(screen.getByRole("button", { name: /^Monzo.*Syncs automatically$/ })).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /^Invest.*Syncs automatically · CSV export optional/ }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Stocks ISA/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Cash ISA.*Upload its History CSV/ })).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /^HSBC UK.*Upload statements · PDF statement recommended/ }),
  ).toBeInTheDocument();
  expect(screen.queryByRole("heading", { name: "Connect" })).toBeNull();
  expect(screen.queryByRole("heading", { name: "Upload statements" })).toBeNull();
  expect(screen.getByRole("button", { name: /Another bank's file/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Type it in yourself/ })).toBeInTheDocument();
});

it("connects a Stocks ISA with the ISA key form", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^Stocks ISA/ }));
  expect(
    screen.getByRole("heading", { name: "Connect Trading 212 Stocks ISA" }),
  ).toBeInTheDocument();

  const form = within(screen.getByRole("form", { name: "Add Trading 212" }));
  expect(form.queryByLabelText("Account type")).toBeNull();
  await user.type(form.getByLabelText("Display name"), "ISA");
  await user.type(form.getByLabelText("API key"), "k");
  await user.type(form.getByLabelText("API secret"), "s");
  await user.click(form.getByRole("button", { name: "Add Trading 212" }));

  expect(createTrading212Connection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: { display_name: "ISA", account_kind: "isa", api_key: "k", api_secret: "s" },
    }),
  );
  await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/connections" }));

  await user.click(screen.getByRole("button", { name: "All ways to add" }));
  expect(screen.getByRole("heading", { name: "Add an account" })).toBeInTheDocument();
});

it("makes a Cash ISA account of its type from its History CSV", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^Cash ISA/ }));

  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  expect(form.getByLabelText("New account name")).toHaveValue("Trading 212 Cash ISA");
  expect(form.queryByRole("group", { name: "What it holds" })).toBeNull();
  expect(form.queryByRole("button", { name: "Money I owe" })).toBeNull();
  const file = new File(
    ["Action,Time (UTC),Notes,ID,Total,Currency (Total)\n"],
    "history.csv",
    { type: "text/csv" },
  );
  await user.upload(form.getByLabelText("File"), file);
  expect(await form.findByText("Recognised as Trading 212 Cash ISA History CSV")).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() => expect(stageImport).toHaveBeenCalled());
  expect(createAccount).toHaveBeenCalledWith(
    expect.objectContaining({
      body: expect.objectContaining({
        name: "Trading 212 Cash ISA",
        type_key: "trading212.cash_isa",
        subtype: "savings_pot",
        tax_wrapper: "isa",
        balance_mode: "transactions",
        ledger_type: "asset",
        institution_name: null,
      }),
    }),
  );
});

it("asks which side an HSBC UK account is on and sends its type", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^HSBC UK/ }));

  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  expect(form.getByLabelText("New account name")).toHaveValue("HSBC UK");
  await user.click(form.getByRole("button", { name: "Money I owe" }));
  await user.upload(form.getByLabelText("File"), new File(["%PDF"], "a.pdf"));
  await form.findByText("Recognised as HSBC UK PDF statement");
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() => expect(stageImport).toHaveBeenCalled());
  expect(createAccount).toHaveBeenCalledWith(
    expect.objectContaining({
      body: expect.objectContaining({
        type_key: "hsbc_uk",
        ledger_type: "liability",
        subtype: "bank",
        tax_wrapper: "none",
        balance_mode: "transactions",
        institution_name: null,
      }),
    }),
  );
});

it("recognises the bank's file, makes its profile, and uploads to a new account", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^HSBC UK/ }));

  const formats = screen.getByRole("list", { name: "HSBC UK formats" });
  expect(within(formats).getByText(/PDF statement · Recommended/)).toBeInTheDocument();

  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.clear(form.getByLabelText("New account name"));
  await user.type(form.getByLabelText("New account name"), "HSBC current");
  const file = new File(["%PDF-1.7"], "November.pdf", { type: "application/pdf" });
  await user.upload(form.getByLabelText("File"), file);
  expect(await form.findByText("Recognised as HSBC UK PDF statement")).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() => expect(stageImport).toHaveBeenCalled());
  expect(createImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({ body: { name: "HSBC UK PDF statement", config: HSBC_PDF.config } }),
  );
  expect(createAccount).toHaveBeenCalledWith(
    expect.objectContaining({ body: expect.objectContaining({ name: "HSBC current" }) }),
  );
  expect(stageImport).toHaveBeenCalledWith(
    expect.objectContaining({
      body: expect.objectContaining({
        account_id: "account-new",
        profile_id: "profile-new",
        file,
      }),
    }),
  );
  await waitFor(() =>
    expect(navigate).toHaveBeenCalledWith({
      to: "/imports/$batchId",
      params: { batchId: "batch-1" },
    }),
  );
});

it("reuses the profile already made from the same format", async () => {
  vi.mocked(listImportProfiles).mockResolvedValue({
    // The same config, its keys in another order.
    data: [
      {
        id: "profile-old",
        name: "Mine",
        config: { card_types: HSBC_CARD_TYPES, layout: "hsbc_uk_statement", format: "pdf" },
      },
    ],
  } as never);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^HSBC UK/ }));
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.upload(form.getByLabelText("File"), new File(["%PDF"], "a.pdf"));
  await form.findByText("Recognised as HSBC UK PDF statement");
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() =>
    expect(stageImport).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.objectContaining({ profile_id: "profile-old" }) }),
    ),
  );
  expect(createImportProfile).not.toHaveBeenCalled();
});

it("says which files the bank exports when the file is none of them", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^HSBC UK/ }));
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.upload(form.getByLabelText("File"), new File(["x"], "export.xlsx"));

  expect(await form.findByRole("alert")).toHaveTextContent(
    "This isn't a file HSBC UK exports. Upload one of: PDF statement, midata CSV, OFX, CSV (transaction list), QIF.",
  );
  await user.click(form.getByRole("button", { name: "Upload" }));
  expect(stageImport).not.toHaveBeenCalled();
});

it("makes a new profile when one of the same name reads files differently", async () => {
  vi.mocked(listImportProfiles).mockResolvedValue({
    data: [
      {
        id: "profile-edited",
        // The database compares names without case or outer spaces.
        name: " hsbc uk pdf STATEMENT ",
        config: { ...HSBC_PDF.config, card_types: [] },
      },
    ],
  } as never);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /^HSBC UK/ }));
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.upload(form.getByLabelText("File"), new File(["%PDF"], "a.pdf"));
  await form.findByText("Recognised as HSBC UK PDF statement");
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() =>
    expect(stageImport).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.objectContaining({ profile_id: "profile-new" }) }),
    ),
  );
  expect(createImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({
      body: { name: "HSBC UK PDF statement (2)", config: HSBC_PDF.config },
    }),
  );
});

it("uploads another bank's file with a profile, asking its side", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(screen.getByRole("button", { name: /Another bank's file/ }));
  expect(screen.getByRole("heading", { name: "Upload another bank's file" })).toBeInTheDocument();
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  expect(form.getByLabelText("New account name")).toHaveValue("");
  expect(form.getByRole("button", { name: "Money I owe" })).toBeInTheDocument();
  expect(form.getByRole("combobox", { name: "Profile" })).toBeInTheDocument();
});

it("types in an account by hand", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(screen.getByRole("button", { name: /Type it in yourself/ }));
  expect(screen.getByRole("heading", { name: "Type in an account" })).toBeInTheDocument();
});
