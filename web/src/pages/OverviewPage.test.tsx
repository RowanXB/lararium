import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import { getCashFlow, getHoldings, getNetWorth, listAccounts, listTransfers } from "../api";
import { presetRange } from "../lib/range";
import { OverviewPage } from "./OverviewPage";

const ACCOUNT = { id: "a1", name: "Bank", system: false, excluded_from_reports: false };
const FLOW = {
  start: "2026-11-01",
  end: "2026-11-26",
  bucket: "day",
  income: "2000.00",
  spending: "500.00",
  net: "1500.00",
  missing_count: 0,
  periods: [{ start: "2026-11-01", income: "2000.00", spending: "500.00" }],
  categories: [
    { category: "eating_out", income: "0", spending: "300.00" },
    { category: null, income: "2000.00", spending: "200.00" },
  ],
  payees: [{ name: "Corner cafe", spending: "300.00" }],
  hidden_accounts: [],
};

vi.mock("../api", () => ({
  getNetWorth: vi.fn(),
  getHoldings: vi.fn(),
  getCashFlow: vi.fn(),
  listAccounts: vi.fn(),
  listTransfers: vi.fn(),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
}));

// A canvas has nothing to draw on in jsdom; what the charts are given is tested beside them.
vi.mock("../components/charts/EChart", () => ({ EChart: () => null }));

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to }: { children: ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getNetWorth).mockResolvedValue({
    data: [
      { day: "2026-11-10", value: "900.00", missing_count: 0 },
      { day: "2026-11-11", value: "1500.00", missing_count: 2 },
    ],
  } as never);
  vi.mocked(getHoldings).mockResolvedValue({
    data: [
      {
        account_id: "a1",
        account_name: "Bank",
        instrument_id: "i1",
        symbol: "GBP",
        quantity: "1500",
        value: "1500.00",
        status: "ok",
      },
    ],
  } as never);
  vi.mocked(getCashFlow).mockResolvedValue({ data: FLOW } as never);
  vi.mocked(listAccounts).mockResolvedValue({ data: [ACCOUNT] } as never);
  vi.mocked(listTransfers).mockResolvedValue({ data: [] } as never);
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <OverviewPage />
    </QueryClientProvider>,
  );
}

it("shows net worth, its change, and warns about missing prices", async () => {
  renderPage();

  expect(await screen.findByText("£1,500.00", { selector: "p.text-4xl" })).toBeInTheDocument();
  expect(screen.getByText(/£600\.00 since/)).toBeInTheDocument();
  expect(
    screen.getByText("2 items are missing a price or FX rate and are excluded from net worth"),
  ).toBeInTheDocument();
});

it("warns when earlier days in the chart are incomplete although the latest is not", async () => {
  vi.mocked(getNetWorth).mockResolvedValue({
    data: [
      { day: "2026-11-09", value: "900.00", missing_count: 1 },
      { day: "2026-11-10", value: "900.00", missing_count: 1 },
      { day: "2026-11-11", value: "1500.00", missing_count: 0 },
    ],
  } as never);
  renderPage();

  expect(
    await screen.findByText(
      "2 earlier days in the chart leave out items missing a price or FX rate (marked in amber)",
    ),
  ).toBeInTheDocument();
});

it("shows income, spending and what is left, by category and payee", async () => {
  renderPage();

  expect(await screen.findByText("£2,000.00")).toBeInTheDocument();
  expect(screen.getByText("£500.00")).toBeInTheDocument();
  expect(screen.getByText("75% of income")).toBeInTheDocument();
  // A category is free text from the source; none is "Uncategorised".
  expect(screen.getByText("Eating out")).toBeInTheDocument();
  expect(screen.getByText("Uncategorised")).toBeInTheDocument();
  expect(screen.getByText("Corner cafe")).toBeInTheDocument();
});

it("asks for everything over the chosen preset", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "12 months" }));

  const range = presetRange("1y");
  await waitFor(() =>
    expect(getCashFlow).toHaveBeenLastCalledWith(
      expect.objectContaining({ query: { ...range, bucket: "month" } }),
    ),
  );
  expect(getNetWorth).toHaveBeenLastCalledWith(expect.objectContaining({ query: range }));
  expect(getHoldings).toHaveBeenLastCalledWith(
    expect.objectContaining({ query: { date: range.end } }),
  );
});

it("takes any two days, and waits until they make a range", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "Custom" }));

  fireEvent.change(screen.getByLabelText("to"), { target: { value: "2026-01-31" } });
  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-02-10" } });
  expect(screen.getByText("Pick a start on or before the end, up to today")).toBeInTheDocument();
  expect(getNetWorth).not.toHaveBeenCalledWith(
    expect.objectContaining({ query: expect.objectContaining({ start: "2026-02-10" }) }),
  );

  fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-01-01" } });
  await waitFor(() =>
    expect(getCashFlow).toHaveBeenLastCalledWith(
      expect.objectContaining({
        query: { start: "2026-01-01", end: "2026-01-31", bucket: "day" },
      }),
    ),
  );
});

it("points at transfers to review and at hidden accounts", async () => {
  vi.mocked(listAccounts).mockResolvedValue({
    data: [ACCOUNT, { ...ACCOUNT, id: "a2", excluded_from_reports: true }],
  } as never);
  vi.mocked(listTransfers).mockResolvedValue({
    data: [{ status: "awaiting", review_reason: null, overdue: true }],
  } as never);
  renderPage();

  expect(await screen.findByRole("link", { name: "1 transfer needs a look" })).toHaveAttribute(
    "href",
    "/transfers",
  );
  expect(
    screen.getByRole("link", { name: "1 account is hidden from these numbers" }),
  ).toHaveAttribute("href", "/accounts");
});

it("leads to adding a first account when there is none", async () => {
  // The ledger's own accounts don't count as the owner's.
  vi.mocked(listAccounts).mockResolvedValue({ data: [{ ...ACCOUNT, system: true }] } as never);
  renderPage();

  expect(await screen.findByRole("link", { name: "Add your first account" })).toHaveAttribute(
    "href",
    "/accounts/new",
  );
});

it("shows a category its refunds took below zero without a share", async () => {
  vi.mocked(getCashFlow).mockResolvedValue({
    data: {
      ...FLOW,
      spending: "20.00",
      net: "1980.00",
      categories: [
        { category: "eating_out", income: "0", spending: "100.00" },
        { category: "shopping", income: "0", spending: "-80.00" },
      ],
    },
  } as never);
  renderPage();

  // Shares are of the categories that spent; a refunded one shows its amount alone.
  expect(await screen.findByText("Shopping")).toBeInTheDocument();
  expect(screen.getByText("100%")).toBeInTheDocument();
  expect(screen.queryByText(/-\d+%/)).not.toBeInTheDocument();
  expect(screen.queryByText("500%")).not.toBeInTheDocument();
});

it("shows money moved to and from hidden accounts apart from categories", async () => {
  vi.mocked(getCashFlow).mockResolvedValue({
    data: {
      ...FLOW,
      spending: "700.00",
      net: "1300.00",
      hidden_accounts: [
        { account_id: "a9", name: "Pension", moved_in: "0", moved_out: "200.00" },
      ],
    },
  } as never);
  renderPage();

  expect(await screen.findByText("Moved to and from hidden accounts")).toBeInTheDocument();
  expect(screen.getByText("Pension")).toBeInTheDocument();
  expect(screen.getByText("Moved in £200.00")).toBeInTheDocument();
  // Categories' shares are of what categories hold, not of spending with the moves in it.
  expect(screen.getByText("60%")).toBeInTheDocument();
});
