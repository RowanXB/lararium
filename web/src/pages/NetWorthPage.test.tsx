import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { NetWorthPage } from "./NetWorthPage";

vi.mock("../api", () => ({
  getNetWorth: vi.fn(async () => ({
    data: [
      { day: "2026-11-10", value: "900.00", missing_count: 0 },
      { day: "2026-11-11", value: "1500.00", missing_count: 2 },
    ],
  })),
  getHoldings: vi.fn(async () => ({ data: [] })),
  getFinanceSettings: vi.fn(async () => ({ data: { reporting_currency: "GBP" } })),
}));

vi.mock("../components/NetWorthChart", () => ({
  NetWorthChart: () => null,
}));

it("shows the total and warns about missing prices", async () => {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NetWorthPage />
    </QueryClientProvider>,
  );

  expect(await screen.findByText("£1,500.00")).toBeInTheDocument();
  expect(
    screen.getByText("2 items are missing a price or FX rate and are excluded from net worth"),
  ).toBeInTheDocument();
});
