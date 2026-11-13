import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { authorizeConnection, createMonzoConnection, listConnections } from "../api";
import { ConnectionsPage } from "./ConnectionsPage";

const CONNECTION = {
  id: "0190a0b0-0000-7000-8000-0000000000d1",
  connector: "monzo",
  display_name: "Monzo main",
  status: "created",
  last_success_at: null,
  last_error: null,
  consecutive_failures: 0,
  credential_expires_at: null,
  warnings: [],
  redirect_uri: "https://lararium.example/api/v1/connections/oauth/monzo/callback",
  webhook_enabled: false,
};

vi.mock("../api", () => ({
  listConnections: vi.fn(),
  createMonzoConnection: vi.fn(async () => ({ data: {} })),
  authorizeConnection: vi.fn(async () => ({ data: { url: "https://auth.monzo.example/?x=1" } })),
  syncConnectionNow: vi.fn(async () => ({ data: {} })),
  disableConnection: vi.fn(async () => ({ data: {} })),
}));

beforeEach(() => {
  vi.mocked(listConnections).mockResolvedValue({ data: [CONNECTION] } as never);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
  vi.clearAllMocks();
});

function renderPage(client = new QueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <ConnectionsPage />
    </QueryClientProvider>,
  );
}

it("add form sends the secret and clears it", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  renderPage(client);
  expect(await screen.findByText(CONNECTION.redirect_uri)).toBeInTheDocument();
  await user.type(screen.getByLabelText("Display name"), "Joint");
  await user.type(screen.getByLabelText("Client ID"), "oauth2client_000");
  const secret = screen.getByLabelText("Client secret");
  expect(secret).toHaveAttribute("type", "password");
  await user.type(secret, "s3cret");
  await user.click(screen.getByRole("button", { name: "Add Monzo" }));

  expect(createMonzoConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: { display_name: "Joint", client_id: "oauth2client_000", client_secret: "s3cret" },
    }),
  );
  await waitFor(() => expect(screen.getByLabelText("Client secret")).toHaveValue(""));
  expect(document.body.textContent).not.toContain("s3cret");
  await waitFor(() =>
    expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables))).not
      .toContain("s3cret"),
  );
});

it("shows the server's error when adding fails", async () => {
  vi.mocked(createMonzoConnection).mockRejectedValueOnce({
    detail: { error: "bad", message: "Client ID rejected" },
  });
  const user = userEvent.setup();
  renderPage();
  await user.type(await screen.findByLabelText("Display name"), "Joint");
  await user.type(screen.getByLabelText("Client ID"), "id");
  await user.type(screen.getByLabelText("Client secret"), "s3cret");
  await user.click(screen.getByRole("button", { name: "Add Monzo" }));
  expect(await screen.findByText("Client ID rejected")).toBeInTheDocument();
});

it("shows this origin's callback URL before any connection exists", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [] } as never);
  vi.stubGlobal("location", { href: "", search: "", origin: "https://lararium.example" });
  renderPage();
  expect(
    await screen.findByText("https://lararium.example/api/v1/connections/oauth/monzo/callback"),
  ).toBeInTheDocument();
});

it("authorise redirects to the returned url", async () => {
  const location = { href: "http://localhost/connections", search: "" };
  vi.stubGlobal("location", location);
  const user = userEvent.setup();
  renderPage();
  await user.click(await screen.findByRole("button", { name: "Authorise" }));

  expect(authorizeConnection).toHaveBeenCalledWith(
    expect.objectContaining({ path: { connection_id: CONNECTION.id } }),
  );
  await waitFor(() => expect(location.href).toBe("https://auth.monzo.example/?x=1"));
});

it("shows the authorising notice and polls until the status changes", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.mocked(listConnections).mockResolvedValueOnce({
    data: [{ ...CONNECTION, status: "authorising" }],
  } as never);
  renderPage();
  expect(await screen.findByText(/Open the Monzo app and approve the request now/)).toBeVisible();
  expect(listConnections).toHaveBeenCalledTimes(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(listConnections).toHaveBeenCalledTimes(2);
  await waitFor(() =>
    expect(screen.queryByText(/Open the Monzo app and approve/)).not.toBeInTheDocument(),
  );

  await act(async () => {
    await vi.advanceTimersByTimeAsync(10000);
  });
  expect(listConnections).toHaveBeenCalledTimes(2);
});

it.each([
  ["state", "The authorisation link expired or was already used. Click Authorise again."],
  ["exchange", "Monzo didn't accept the authorisation. Click Authorise to try again."],
  [
    "status",
    "This connection can't be authorised right now. Disable it first, or wait until it needs re-authorisation.",
  ],
])("shows the %s error banner", async (code, message) => {
  window.history.replaceState({}, "", `/connections?error=${code}`);
  renderPage();
  expect(await screen.findByText(message)).toBeInTheDocument();
});

it("notes when webhooks are off", async () => {
  renderPage();
  expect(
    await screen.findByText(
      "Webhooks are off, so new Monzo transactions arrive within 30 minutes.",
    ),
  ).toBeInTheDocument();
});
