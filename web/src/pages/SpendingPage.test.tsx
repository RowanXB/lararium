import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { getSpending, listAccounts, setTransactionCategories } from "../api";
import { SpendingPage } from "./SpendingPage";

const SHOP = {
  account_id: "a1",
  account_name: "Current",
  amount: "42.50",
  booking_date: "2026-11-20",
  category: "shopping",
  counterparty_name: "Corner Shop",
  description: "Card payment",
  instrument: "GBP",
  owner_category: null,
  quantity: "42.50",
  refund: false,
  source_category: "shopping",
  transaction_id: "t1",
  hidden_account_name: null,
};
const REFUND = {
  ...SHOP,
  account_id: "a2",
  account_name: "Savings",
  amount: "-10.00",
  booking_date: "2026-11-21",
  counterparty_name: "Book Barn",
  description: "Returned book",
  quantity: "-10.00",
  refund: true,
  source_category: "eating_out",
  category: "eating_out",
  transaction_id: "t2",
};
const SPENDING = {
  start: "2026-08-29",
  end: "2026-11-27",
  bucket: "week",
  total: "32.50",
  missing_count: 0,
  known_categories: ["groceries"],
  periods: [{ start: "2026-11-16", total: "32.50" }],
  categories: [
    { category: "shopping", amount: "42.50" },
    { category: "eating_out", amount: "-10.00" },
  ],
  entries: [REFUND, SHOP],
};

vi.mock("../api", () => ({
  getSpending: vi.fn(),
  listAccounts: vi.fn(),
  setTransactionCategories: vi.fn(),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
}));
vi.mock("../components/charts/EChart", () => ({ EChart: () => null }));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getSpending).mockResolvedValue({ data: SPENDING } as never);
  vi.mocked(listAccounts).mockResolvedValue({
    data: [
      { id: "a1", name: "Current", system: false, excluded_from_reports: false },
      { id: "a2", name: "Savings", system: false, excluded_from_reports: false },
    ],
  } as never);
  vi.mocked(setTransactionCategories).mockResolvedValue({ data: undefined } as never);
});

function renderPage() {
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <SpendingPage />
    </QueryClientProvider>,
  );
  return client;
}

function rowOf(text: string) {
  return within(screen.getByText(text).closest("tr") as HTMLElement);
}

it("lists the entries with the source category, and a refund negative and marked", async () => {
  renderPage();

  expect(await screen.findByText("Corner Shop")).toBeInTheDocument();
  expect(screen.getByText("Total").nextSibling).toHaveTextContent("£32.50");
  expect(screen.getByText("To / description")).toBeInTheDocument();
  expect(rowOf("Corner Shop").getByText("Shopping")).toBeInTheDocument();
  expect(rowOf("Corner Shop").queryByText("Refund")).toBeNull();
  const refund = rowOf("Book Barn");
  expect(refund.getByText("-£10.00")).toBeInTheDocument();
  expect(refund.getByText("Refund")).toBeInTheDocument();
  expect(refund.getByText("Eating out")).toBeInTheDocument();
});

it("marks a move to a hidden account with where it went", async () => {
  const moved = {
    ...SHOP,
    counterparty_name: null,
    description: "Rainy day pot",
    hidden_account_name: "Rainy day pot",
    transaction_id: "t3",
  };
  vi.mocked(getSpending).mockResolvedValue({
    data: { ...SPENDING, entries: [moved, SHOP] },
  } as never);
  renderPage();

  expect(await screen.findByText("To Rainy day pot")).toBeInTheDocument();
  expect(rowOf("Corner Shop").queryByText(/^To /)).toBeNull();
  expect(screen.queryByText("Refund")).toBeNull();
});

it("filters by account and by category", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("Corner Shop");

  await user.click(await screen.findByRole("combobox", { name: "Account" }));
  await user.click(await screen.findByRole("option", { name: "Savings" }));
  await waitFor(() =>
    expect(getSpending).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: expect.objectContaining({ account_id: "a2" }) }),
    ),
  );

  await user.click(screen.getByRole("button", { name: /Shopping/ }));
  await waitFor(() =>
    expect(getSpending).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: expect.objectContaining({ category: "shopping" }) }),
    ),
  );
  await user.click(screen.getByRole("button", { name: /Shopping/ }));
  await waitFor(() =>
    expect(vi.mocked(getSpending).mock.lastCall?.[0]?.query).not.toHaveProperty("category"),
  );
});

it("sets and clears a category on one entry", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("Corner Shop");

  await user.type(rowOf("Corner Shop").getByLabelText(/^Category for/), "groceries{Enter}");
  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenCalledWith({
      body: { transaction_ids: ["t1"], category: "groceries" },
      throwOnError: true,
    }),
  );
  // Spending, income and the cash flow are all read again.
  await waitFor(() => expect(getSpending).toHaveBeenCalledTimes(2));

  vi.mocked(getSpending).mockResolvedValue({
    data: { ...SPENDING, entries: [{ ...SHOP, owner_category: "groceries" }] },
  } as never);
  await user.click(screen.getByRole("button", { name: /Shopping/ }));
  await user.click(await screen.findByRole("button", { name: "Clear category for Card payment" }));
  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenLastCalledWith({
      body: { transaction_ids: ["t1"], category: null },
      throwOnError: true,
    }),
  );
});

it("sets many at once and clears", async () => {
  const user = userEvent.setup();
  renderPage();
  await screen.findByText("Corner Shop");

  await user.click(rowOf("Corner Shop").getByRole("checkbox"));
  await user.click(rowOf("Book Barn").getByRole("checkbox"));
  expect(screen.getByText("2 selected")).toBeInTheDocument();
  await user.type(screen.getByLabelText("Category for selected"), "gifts");
  await user.click(screen.getByRole("button", { name: "Apply" }));
  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenLastCalledWith({
      body: { transaction_ids: ["t1", "t2"], category: "gifts" },
      throwOnError: true,
    }),
  );

  await user.click(rowOf("Corner Shop").getByRole("checkbox"));
  await user.click(rowOf("Book Barn").getByRole("checkbox"));
  await user.click(screen.getByRole("button", { name: "Clear category" }));
  await waitFor(() =>
    expect(setTransactionCategories).toHaveBeenLastCalledWith({
      body: { transaction_ids: ["t1", "t2"], category: null },
      throwOnError: true,
    }),
  );
});

it("offers the known categories, then the source's not among them, and no income ones", async () => {
  renderPage();
  await screen.findByText("Corner Shop");

  const list = document.getElementById(
    rowOf("Corner Shop").getByLabelText(/^Category for/).getAttribute("list")!,
  )!;
  expect([...list.querySelectorAll("option")].map((o) => o.getAttribute("value"))).toEqual([
    "groceries",
    "eating_out",
    "shopping",
  ]);
});

it("offers nothing made up when no category is known", async () => {
  vi.mocked(getSpending).mockResolvedValue({
    data: {
      ...SPENDING,
      known_categories: [],
      entries: [{ ...SHOP, source_category: null }],
    },
  } as never);
  renderPage();
  await screen.findByText("Corner Shop");

  const list = document.getElementById(
    rowOf("Corner Shop").getByLabelText(/^Category for/).getAttribute("list")!,
  )!;
  expect(list.querySelectorAll("option")).toHaveLength(0);
});
