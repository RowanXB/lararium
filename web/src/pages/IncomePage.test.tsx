import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { getIncome, listAccounts, setTransactionCategories } from "../api";
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
  hidden_moved_in: "0",
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
      { id: "a1", name: "Current", system: false },
      { id: "a2", name: "Savings", system: false },
      { id: "sys", name: "In transit", system: true },
    ],
  } as never);
  vi.mocked(setTransactionCategories).mockResolvedValue({ data: undefined } as never);
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <IncomePage />
    </QueryClientProvider>,
  );
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

it("filters by account and by category", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("ACME LTD");

  const select = await screen.findByRole("combobox", { name: "Account" });
  await user.click(select);
  expect(screen.queryByRole("option", { name: "In transit" })).toBeNull();
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

  await user.type(rowOf("ACME LTD").getByLabelText("Category"), "salary{Enter}");

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
  expect(row.getByLabelText("Category")).toHaveValue("family");
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
