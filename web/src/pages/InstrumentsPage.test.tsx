import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { createInstrument } from "../api";
import { InstrumentsPage } from "./InstrumentsPage";

vi.mock("../api", () => ({
  listInstruments: vi.fn(async () => ({ data: [] })),
  createInstrument: vi.fn(async () => ({ data: {} })),
  updateInstrument: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(createInstrument).mockClear();
});

async function newInstrument(kind: string, symbol: string, name: string) {
  const user = userEvent.setup();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <InstrumentsPage />
    </QueryClientProvider>,
  );
  const form = within(screen.getByRole("form", { name: "New instrument" }));
  await user.click(form.getByLabelText("Kind"));
  await user.click(await screen.findByRole("option", { name: kind }));
  await user.type(form.getByLabelText("Symbol"), symbol);
  await user.type(form.getByLabelText("Name"), name);
  await user.click(form.getByRole("button", { name: "Create instrument" }));
}

it("gives a new fiat currency its ISO 4217 identifier so FX rates can find it", async () => {
  await newInstrument("Fiat", "NGN", "Naira");

  expect(createInstrument).toHaveBeenCalledWith(
    expect.objectContaining({
      body: expect.objectContaining({
        kind: "fiat",
        symbol: "NGN",
        identifiers: [{ scheme: "iso4217", value: "NGN" }],
      }),
    }),
  );
});

it("gives other instruments no ISO 4217 identifier", async () => {
  await newInstrument("Equity", "AAPL", "Apple");

  expect(createInstrument).toHaveBeenCalledWith(
    expect.objectContaining({
      body: expect.objectContaining({ kind: "equity", symbol: "AAPL", identifiers: [] }),
    }),
  );
});
