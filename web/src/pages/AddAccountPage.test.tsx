import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { beforeEach, expect, it, vi } from "vitest";

import { createAccount, createImportProfile, listImportProfiles, stageImport } from "../api";
import { AddAccountPage } from "./AddAccountPage";

const navigate = vi.fn();

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
  Link: ({ children, to }: { children: ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

const PDF = {
  institution: "Example Bank",
  label: "PDF statement",
  recommended: true,
  config: { format: "pdf", layout: "example" },
};
const CSV = {
  institution: "Example Bank",
  label: "CSV",
  recommended: false,
  config: { format: "csv", columns: { date: "Date", description: ["Details"], amount: "Amount" } },
};

vi.mock("../api", () => ({
  getImportCatalog: vi.fn(async () => ({ data: [CSV, PDF] })),
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

it("offers every way to add an account, with each bank's recommended file", async () => {
  renderPage();

  for (const name of ["Monzo", "Interactive Brokers", "Trading 212", "eToro"]) {
    expect(screen.getByRole("button", { name: new RegExp(`^${name}`) })).toBeInTheDocument();
  }
  expect(
    await screen.findByRole("button", { name: /Example Bank.*Recommended: PDF statement/ }),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Another bank's file/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Type it in yourself/ })).toBeInTheDocument();
});

it("recognises the bank's file, makes its profile, and uploads to a new account", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Example Bank/ }));

  const formats = screen.getByRole("list", { name: "Example Bank formats" });
  expect(within(formats).getByText(/PDF statement · Recommended/)).toBeInTheDocument();

  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.type(form.getByLabelText("New account name"), "Example current");
  const file = new File(["%PDF-1.7"], "November.pdf", { type: "application/pdf" });
  await user.upload(form.getByLabelText("File"), file);
  expect(await form.findByText("Recognised as Example Bank PDF statement")).toBeInTheDocument();
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() => expect(stageImport).toHaveBeenCalled());
  expect(createImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({ body: { name: "Example Bank PDF statement", config: PDF.config } }),
  );
  expect(createAccount).toHaveBeenCalledWith(
    expect.objectContaining({ body: expect.objectContaining({ name: "Example current" }) }),
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
      { id: "profile-old", name: "Mine", config: { layout: "example", format: "pdf" } },
    ],
  } as never);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Example Bank/ }));
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.type(form.getByLabelText("New account name"), "Example current");
  await user.upload(form.getByLabelText("File"), new File(["%PDF"], "a.pdf"));
  await form.findByText("Recognised as Example Bank PDF statement");
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
  await user.click(await screen.findByRole("button", { name: /Example Bank/ }));
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.upload(form.getByLabelText("File"), new File(["x"], "export.qif"));

  expect(await form.findByRole("alert")).toHaveTextContent(
    "This isn't a file Example Bank exports. Upload one of: PDF statement, CSV.",
  );
  await user.click(form.getByRole("button", { name: "Upload" }));
  expect(stageImport).not.toHaveBeenCalled();
});

it("connects a provider and then shows the connections", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(screen.getByRole("button", { name: /^eToro/ }));
  expect(screen.getByRole("heading", { name: "Connect eToro" })).toBeInTheDocument();
  expect(screen.getByRole("form", { name: "Add eToro" })).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "All ways to add" }));
  expect(screen.getByRole("heading", { name: "Add an account" })).toBeInTheDocument();
});

it("makes a new profile when one of the same name reads files differently", async () => {
  vi.mocked(listImportProfiles).mockResolvedValue({
    data: [
      {
        id: "profile-edited",
        name: "Example Bank PDF statement",
        config: { format: "pdf", layout: "example", negate: true },
      },
    ],
  } as never);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: /Example Bank/ }));
  const form = within(await screen.findByRole("form", { name: "Upload statement" }));
  await user.type(form.getByLabelText("New account name"), "Example current");
  await user.upload(form.getByLabelText("File"), new File(["%PDF"], "a.pdf"));
  await form.findByText("Recognised as Example Bank PDF statement");
  await user.click(form.getByRole("button", { name: "Upload" }));

  await waitFor(() =>
    expect(stageImport).toHaveBeenCalledWith(
      expect.objectContaining({ body: expect.objectContaining({ profile_id: "profile-new" }) }),
    ),
  );
  expect(createImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({
      body: { name: "Example Bank PDF statement (2)", config: PDF.config },
    }),
  );
});
