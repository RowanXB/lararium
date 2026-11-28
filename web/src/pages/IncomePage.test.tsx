import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { getIncome, listAccounts, setTransactionCategories } from "../api";
import { presetRange } from "../lib/range";
import { IncomePage } from "./IncomePage";

const ENTRY = {
  account_id: "a1",
  account_name: "Current",
  amount: "1500.00",
  booking_date: "2026-11-20",
  category: null,
  counterparty_name: "ACME LTD",
  description: "Pay",
  income_kind: null,
  instrument: "GBP",
  owner_category: null,
  quantity: "1500.00",
  source_category: null,
  transaction_id: "t1",
  hidden_account_name: null,
};
const INTEREST = {
  ...ENTRY,
  account_id: "a2",
  account_name: "Savings",
  amount: "12.00",
  category: "interest",
  counterparty_name: null,
  description: "Gross Interest",
  income_kind: "interest",
  quantity: "12.00",
  transaction_id: "t2",
};
const INCOME = {
  start: "2026-08-29",
  end: "2026-11-27",
  bucket: "week",
  total: "1512.00",
  missing_count: 0,
  known_categories: ["salary"],
  periods: [{ start: "2026-11-16", total: "1512.00", by_category: [] }],
  categories: [
    { category: null, amount: "1500.00" },
    { category: "interest", amount: "12.00" },
  ],
  entries: [ENTRY, INTEREST],
};

vi.mock("../api", () => ({
  getIncome: vi.fn(),
  listAccounts: vi.fn(),
  setTransactionCategories: vi.fn(),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
}));
vi.mock("../components/charts/EChart", () => ({ EChart: () => null }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getIncome).mockResolvedValue({ data: INCOME } as never);
  vi.mocked(listAccounts).mockResolvedValue({
    data: [
      { id: "a1", name: "Current", system: false, excluded_from_reports: false },
      { id: "a2", name: "Savings", system: false, excluded_from_reports: false },
      { id: "a3", name: "Pension", system: false, excluded_from_reports: true },
      { id: "sys", name: "In transit", system: true, excluded_from_reports: false },
    ],
  } as never);
  vi.mocked(setTransactionCategories).mockResolvedValue({ data: undefined } as never);
});

function renderPage(initialRange?: Parameters<typeof IncomePage>[0]["initialRange"]) {
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <IncomePage initialRange={initialRange} />
    </QueryClientProvider>,
  );
  return client;
}

function rowOf(text: string) {
  return within(screen.getByText(text).closest("tr") as HTMLElement);
}

it("shows totals, categories and entries", async () => {
  renderPage();

  expect(await screen.findByText("ACME LTD")).toBeInTheDocument();
  expect(screen.getByText("Total").nextSibling).toHaveTextContent("£1,512.00");
  expect(screen.getByText("Uncategorised", { selector: "p" }).nextSibling).toHaveTextContent(
    "£1,500.00",
  );
  expect(screen.getByRole("button", { name: /Interest/ })).toBeInTheDocument();
  expect(screen.getByText("Gross Interest")).toBeInTheDocument();
});

it("marks a move from a hidden account with where it came from", async () => {
  const moved = {
    ...ENTRY,
    counterparty_name: null,
    description: "Pension payout",
    hidden_account_name: "Pension",
    transaction_id: "t3",
  };
  vi.mocked(getIncome).mockResolvedValue({ data: { ...INCOME, entries: [moved, ENTRY] } } as never);
  renderPage();

  expect(await screen.findByText("From Pension")).toBeInTheDocument();
  expect(rowOf("ACME LTD").queryByText(/^From /)).toBeNull();
});

it("filters by account and by category", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");

  const select = await screen.findByRole("combobox", { name: "Account" });
  await user.click(select);
  expect(screen.queryByRole("option", { name: "In transit" })).toBeNull();
  // An account left out of reports has no entries here to filter to.
  expect(screen.queryByRole("option", { name: "Pension" })).toBeNull();
  await user.click(await screen.findByRole("option", { name: "Savings" }));
  await waitFor(() =>
    expect(getIncome).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: expect.objectContaining({ account_id: "a2" }) }),
    ),
  );

  await user.click(screen.getByRole("button", { name: /Interest/ }));
  await waitFor(() =>
    expect(getIncome).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: expect.objectContaining({ category: "interest" }) }),
    ),
  );
  // Clicking it again clears the filter.
  await user.click(screen.getByRole("button", { name: /Interest/ }));
  await waitFor(() =>
    expect(vi.mocked(getIncome).mock.lastCall?.[0]?.query).not.toHaveProperty("category"),
  );
});

it("sets one entry's category", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");

  await user.type(rowOf("ACME LTD").getByLabelText(/^Category for/), "salary{Enter}");

  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenCalledWith({
      body: { transaction_ids: ["t1"], category: "salary" },
      throwOnError: true,
    }),
  );
  // Both queries that show income are read again.
  await waitFor(() => expect(getIncome).toHaveBeenCalledTimes(2));
});

it("sets many at once and clears", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");

  await user.click(rowOf("ACME LTD").getByRole("checkbox"));
  await user.click(rowOf("Gross Interest").getByRole("checkbox"));
  expect(screen.getByText("2 selected")).toBeInTheDocument();
  await user.type(screen.getByLabelText("Category for selected"), "family");
  await user.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenLastCalledWith({
      body: { transaction_ids: ["t1", "t2"], category: "family" },
      throwOnError: true,
    }),
  );

  await user.click(rowOf("ACME LTD").getByRole("checkbox"));
  await user.click(rowOf("Gross Interest").getByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "Clear category" }));
  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenLastCalledWith({
      body: { transaction_ids: ["t1", "t2"], category: null },
      throwOnError: true,
    }),
  );
});

it("sends a selection larger than the API takes in turns", async () => {
  const many = Array.from({ length: 501 }, (_, i) => ({
    ...ENTRY,
    description: `Reward ${i}`,
    transaction_id: `r${i}`,
  }));
  vi.mocked(getIncome).mockResolvedValue({ data: { ...INCOME, entries: many } } as never);
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("Reward 500");

  await user.click(screen.getByLabelText("Select all"));
  expect(screen.getByText("501 selected")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Clear category" }));

  await waitFor(() => expect(setTransactionCategories).toHaveBeenCalledTimes(2));
  const sent = vi.mocked(setTransactionCategories).mock.calls.map(
    ([options]) => (options as { body: { transaction_ids: string[] } }).body.transaction_ids,
  );
  expect(sent.map((ids) => ids.length)).toEqual([500, 1]);
  expect(sent.flat()).toEqual(many.map((entry) => entry.transaction_id));
});

it("shows the source column unchanged after categorising", async () => {
  vi.mocked(getIncome).mockResolvedValue({
    data: {
      ...INCOME,
      entries: [{ ...INTEREST, owner_category: "family", category: "family" }],
    },
  } as never);
  renderPage();

  await screen.findByText("Gross Interest");
  const row = rowOf("Gross Interest");
  expect(row.getByText("Interest")).toBeInTheDocument();
  expect(row.getByLabelText(/^Category for/)).toHaveValue("family");
});

it("shows a coin reward's quantity and No price when unpriced", async () => {
  vi.mocked(getIncome).mockResolvedValue({
    data: {
      ...INCOME,
      entries: [
        {
          ...ENTRY,
          transaction_id: "t3",
          description: "Earn reward",
          counterparty_name: null,
          amount: null,
          instrument: "BTC",
          quantity: "0.00001",
          income_kind: "reward",
        },
      ],
    },
  } as never);
  renderPage();

  await screen.findByText("Earn reward");
  const row = rowOf("Earn reward");
  expect(row.getByText("No price")).toBeInTheDocument();
  expect(row.getByText("0.00001 BTC")).toBeInTheDocument();
  expect(row.getByText("Reward")).toBeInTheDocument();
});

async function pickAccount(user: ReturnType<typeof userEvent.setup>, name: string) {
  await user.click(await screen.findByRole("combobox", { name: "Account" }));
  await user.click(await screen.findByRole("option", { name }));
}

it("drops the bulk selection when the view changes, and sends nothing", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");
  await user.click(rowOf("ACME LTD").getByRole("checkbox"));
  expect(screen.getByText("1 selected")).toBeInTheDocument();

  await pickAccount(user, "Savings");

  await waitFor(() => expect(screen.queryByText(/selected$/)).toBeNull());
  expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
  expect(setTransactionCategories).not.toHaveBeenCalled();
});

it("keeps the bulk selection when one row is written", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");
  await user.click(rowOf("Gross Interest").getByRole("checkbox"));

  await user.type(rowOf("ACME LTD").getByLabelText(/^Category for/), "salary{Enter}");
  await waitFor(() => expect(setTransactionCategories).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(getIncome).toHaveBeenCalledTimes(2));

  expect(screen.getByText("1 selected")).toBeInTheDocument();
});

const SET = { ...ENTRY, owner_category: "salary", category: "salary" };

it("sends nothing when a set row is emptied and left", async () => {
  const user = userEvent.setup();
  vi.mocked(getIncome).mockResolvedValue({ data: { ...INCOME, entries: [SET] } } as never);
  renderPage();
  await screen.findByText("ACME LTD");

  const field = rowOf("ACME LTD").getByLabelText(/^Category for/);
  await user.clear(field);
  await user.tab();

  expect(setTransactionCategories).not.toHaveBeenCalled();
  expect(field).toHaveValue("salary");
});

it("sends once when Enter is followed by a blur", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");

  await user.type(rowOf("ACME LTD").getByLabelText(/^Category for/), "salary{Enter}");
  await user.tab();

  await waitFor(() => expect(setTransactionCategories).toHaveBeenCalledTimes(1));
});

it("clears a row's category with its clear button", async () => {
  const user = userEvent.setup();
  vi.mocked(getIncome).mockResolvedValue({ data: { ...INCOME, entries: [SET] } } as never);
  renderPage();
  await screen.findByText("ACME LTD");

  await user.click(screen.getByRole("button", { name: "Clear category for Pay" }));

  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenCalledWith({
      body: { transaction_ids: ["t1"], category: null },
      throwOnError: true,
    }),
  );
});

it("lets the category filter be cleared when nothing matches", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");
  vi.mocked(getIncome).mockResolvedValue({
    data: { ...INCOME, total: "0", categories: [], entries: [], periods: [] },
  } as never);
  await user.click(screen.getByRole("button", { name: /Interest/ }));
  expect(await screen.findByText("Showing Interest")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Clear filter" }));

  await waitFor(() =>
    expect(vi.mocked(getIncome).mock.lastCall?.[0]?.query).not.toHaveProperty("category"),
  );
});

it("reads the cash flow again after a write", async () => {
  const user = userEvent.setup();
  const client = renderPage();
  const spy = vi.spyOn(client, "invalidateQueries");
  await screen.findByText("ACME LTD");

  await user.type(rowOf("ACME LTD").getByLabelText(/^Category for/), "salary{Enter}");

  await waitFor(() =>
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ queryKey: ["cash-flow"] })),
  );
});

it("sends the same category again after a failed write", async () => {
  const user = userEvent.setup();
  vi.mocked(setTransactionCategories).mockRejectedValueOnce(new Error("offline"));
  renderPage();
  await screen.findByText("ACME LTD");

  const field = rowOf("ACME LTD").getByLabelText(/^Category for/);
  await user.type(field, "salary{Enter}");
  await waitFor(() => expect(setTransactionCategories).toHaveBeenCalledTimes(1));
  expect(await screen.findByText(/offline/)).toBeInTheDocument();

  await user.clear(field);
  await user.type(field, "salary{Enter}");
  await waitFor(() => expect(setTransactionCategories).toHaveBeenCalledTimes(2));
});

it("opens on the dates it was given", async () => {
  renderPage({ preset: "custom", start: "2026-09-01", end: "2026-09-30" });
  await waitFor(() =>
    expect(getIncome).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ start: "2026-09-01", end: "2026-09-30" }),
      }),
    ),
  );
  expect(screen.getByRole("button", { name: "Custom" })).toHaveAttribute("aria-pressed", "true");
});

it("opens on the preset it was given", async () => {
  renderPage({ preset: "month" });
  await waitFor(() =>
    expect(getIncome).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: expect.objectContaining(presetRange("month")) }),
    ),
  );
  expect(screen.getByRole("button", { name: "This month" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});

it("opens a preset link from another day on the dates it was made with", async () => {
  // "This month" linked in January 2020 still shows January 2020, not this month.
  renderPage({ preset: "month", start: "2020-01-01", end: "2020-01-31" });
  await waitFor(() =>
    expect(getIncome).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: expect.objectContaining({ start: "2020-01-01", end: "2020-01-31" }),
      }),
    ),
  );
  expect(screen.getByRole("button", { name: "Custom" })).toHaveAttribute("aria-pressed", "true");
});

it("opens a preset link whose dates are today's as the preset", async () => {
  renderPage({ preset: "month", ...presetRange("month") });
  await waitFor(() =>
    expect(getIncome).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: expect.objectContaining(presetRange("month")) }),
    ),
  );
  expect(screen.getByRole("button", { name: "This month" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
});
