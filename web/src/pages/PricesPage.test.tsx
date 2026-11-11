import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { recordFxRate, recordPrice } from "../api";
import { PricesPage } from "./PricesPage";

const BTC_ID = "0190a0b0-0000-7000-8000-0000000000c1";

vi.mock("../api", () => ({
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
  recordPrice: vi.fn(async () => ({ data: undefined })),
  recordFxRate: vi.fn(async () => ({ data: undefined })),
}));

beforeEach(() => {
  vi.mocked(recordPrice).mockClear();
  vi.mocked(recordFxRate).mockClear();
});

async function form(name: string) {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <PricesPage />
    </QueryClientProvider>,
  );
  return within(await screen.findByRole("form", { name }));
}

// The instrument defaults to the first one that isn't a currency, once they've loaded.
async function instrumentShown(form: ReturnType<typeof within>, symbol: string) {
  await waitFor(() =>
    expect(form.getByRole("combobox", { name: "Instrument" })).toHaveTextContent(symbol),
  );
}

it("records a price as decimal strings", async () => {
  const user = userEvent.setup();
  const price = await form("Record price");
  await instrumentShown(price, "BTC");
  await user.type(price.getByLabelText("Quote currency"), "GBP");
  await user.type(price.getByLabelText("Price"), "60000.50");
  fireEvent.change(price.getByLabelText("Date"), { target: { value: "2026-11-10" } });
  await user.click(price.getByRole("button", { name: "Record price" }));

  expect(recordPrice).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        instrument_id: BTC_ID,
        quote_currency: "GBP",
        price: "60000.50",
        as_of_date: "2026-11-10",
      },
    }),
  );
  expect(await price.findByText("Saved")).toBeInTheDocument();
});

it("records an FX rate as decimal strings", async () => {
  const user = userEvent.setup();
  const fx = await form("Record FX rate");
  await user.type(fx.getByLabelText("Base currency"), "USD");
  await user.type(fx.getByLabelText("Quote currency"), "GBP");
  await user.type(fx.getByLabelText("Rate"), "0.7900");
  fireEvent.change(fx.getByLabelText("Date"), { target: { value: "2026-11-10" } });
  await user.click(fx.getByRole("button", { name: "Record FX rate" }));

  expect(recordFxRate).toHaveBeenCalledWith(
    expect.objectContaining({
      body: { base: "USD", quote: "GBP", rate: "0.7900", as_of_date: "2026-11-10" },
    }),
  );
  expect(await fx.findByText("Saved")).toBeInTheDocument();
});

it.each(["abc", "-1", "1e3"])("rejects the price %s without sending it", async (value) => {
  const user = userEvent.setup();
  const price = await form("Record price");
  await instrumentShown(price, "BTC");
  await user.type(price.getByLabelText("Quote currency"), "GBP");
  await user.type(price.getByLabelText("Price"), value);
  await user.click(price.getByRole("button", { name: "Record price" }));

  expect(price.getByText("Enter a positive number, e.g. 1234.56")).toBeInTheDocument();
  expect(recordPrice).not.toHaveBeenCalled();
});
