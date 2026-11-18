import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";

import { createImportProfile, updateImportProfile } from "../api";
import { ImportProfilesPage } from "./ImportProfilesPage";

const PDF_CONFIG = { format: "pdf", layout: "hsbc_uk_statement" };
const OFX_CONFIG = { format: "ofx" };

vi.mock("../api", () => ({
  listImportProfiles: vi.fn(async () => ({
    data: [
      {
        id: "0190a0b0-0000-7000-8000-0000000000d1",
        name: "Example Bank CSV",
        version: 2,
        updated_at: "2026-11-01T10:00:00Z",
        config: {
          format: "csv",
          columns: { date: "Date", description: ["Description"], amount: "Amount" },
          date_format: "%d/%m/%Y",
        },
      },
    ],
  })),
  getImportCatalog: vi.fn(async () => ({
    data: [
      {
        institution: "HSBC UK",
        label: "OFX",
        format: "ofx",
        recommended: false,
        config: OFX_CONFIG,
      },
      {
        institution: "HSBC UK",
        label: "PDF statement",
        format: "pdf",
        recommended: true,
        config: PDF_CONFIG,
      },
    ],
  })),
  createImportProfile: vi.fn(async () => ({ data: {} })),
  updateImportProfile: vi.fn(async () => ({ data: {} })),
}));

beforeEach(() => {
  vi.mocked(createImportProfile).mockClear();
  vi.mocked(updateImportProfile).mockClear();
});

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ImportProfilesPage />
    </QueryClientProvider>,
  );
  return within(screen.getByRole("form", { name: "New profile" }));
}

it("test_invalid_json_not_sent", async () => {
  const user = userEvent.setup();
  const form = renderPage();
  await user.type(form.getByLabelText("Name"), "Example Bank CSV");
  fireEvent.change(form.getByLabelText("Config (JSON)"), { target: { value: "{not json" } });
  await user.click(form.getByRole("button", { name: "Create profile" }));

  expect(form.getByText("Config is not valid JSON")).toBeInTheDocument();
  expect(createImportProfile).not.toHaveBeenCalled();
});

it("sends the parsed config", async () => {
  const user = userEvent.setup();
  const form = renderPage();
  await user.type(form.getByLabelText("Name"), "Example OFX");
  fireEvent.change(form.getByLabelText("Config (JSON)"), {
    target: { value: '{"format": "ofx"}' },
  });
  await user.click(form.getByRole("button", { name: "Create profile" }));

  expect(createImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({ body: { name: "Example OFX", config: { format: "ofx" } } }),
  );
});

it("test_server_validation_message_shown", async () => {
  vi.mocked(createImportProfile).mockRejectedValueOnce({
    detail: [{ msg: "Value error, date_format must contain a day" }],
  });
  const user = userEvent.setup();
  const form = renderPage();
  await user.type(form.getByLabelText("Name"), "Example Bank CSV");
  fireEvent.change(form.getByLabelText("Config (JSON)"), {
    target: { value: '{"format": "csv"}' },
  });
  await user.click(form.getByRole("button", { name: "Create profile" }));

  expect(
    await form.findByText("Value error, date_format must contain a day"),
  ).toBeInTheDocument();
});

it("test_catalog_marks_recommended", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(screen.getByRole("button", { name: "Add from catalog" }));
  const catalog = within(await screen.findByRole("list", { name: "HSBC UK" }));
  const entries = catalog.getAllByRole("listitem");

  expect(entries[0]).toHaveTextContent("PDF statement");
  expect(entries[0]).toHaveTextContent("Recommended");
  expect(entries[1]).toHaveTextContent("OFX");
  expect(entries[1]).not.toHaveTextContent("Recommended");

  await user.click(within(entries[0]).getByRole("button", { name: "Add" }));
  expect(createImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({ body: { name: "HSBC UK PDF statement", config: PDF_CONFIG } }),
  );
});

it("edits an existing profile", async () => {
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "Edit Example Bank CSV" }));
  const form = within(screen.getByRole("form", { name: "Edit profile" }));
  expect(form.getByLabelText("Name")).toHaveValue("Example Bank CSV");
  fireEvent.change(form.getByLabelText("Config (JSON)"), {
    target: { value: '{"format": "ofx"}' },
  });
  await user.click(form.getByRole("button", { name: "Save profile" }));

  expect(updateImportProfile).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { profile_id: "0190a0b0-0000-7000-8000-0000000000d1" },
      body: { name: "Example Bank CSV", config: { format: "ofx" } },
    }),
  );
});
