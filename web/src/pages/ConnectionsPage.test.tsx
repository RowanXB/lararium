import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  authorizeConnection,
  createIbkrConnection,
  createMonzoConnection,
  listConnections,
  updateIbkrConnection,
  uploadIbkrStatement,
} from "../api";
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
  createIbkrConnection: vi.fn(async () => ({ data: {} })),
  updateIbkrConnection: vi.fn(async () => ({ data: {} })),
  uploadIbkrStatement: vi.fn(async () => ({ data: { raw_records: 3 } })),
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
  const monzo = screen.getByRole("form", { name: "Add Monzo" });
  await user.type(within(monzo).getByLabelText("Display name"), "Joint");
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
  const monzo = await screen.findByRole("form", { name: "Add Monzo" });
  await user.type(within(monzo).getByLabelText("Display name"), "Joint");
  await user.type(screen.getByLabelText("Client ID"), "id");
  await user.type(screen.getByLabelText("Client secret"), "s3cret");
  await user.click(screen.getByRole("button", { name: "Add Monzo" }));
  expect(await screen.findByText("Client ID rejected")).toBeInTheDocument();
});

it("says how the callback URL is built before any connection exists", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [] } as never);
  // The page's own origin may differ from LARARIUM_PUBLIC_BASE_URL: it is never shown as the URL.
  vi.stubGlobal("location", { href: "", search: "", origin: "http://localhost:5173" });
  renderPage();
  expect(await screen.findByText("LARARIUM_PUBLIC_BASE_URL")).toBeInTheDocument();
  expect(screen.getByText("/api/v1/connections/oauth/monzo/callback")).toBeInTheDocument();
  expect(screen.queryByText(/localhost:5173/)).not.toBeInTheDocument();
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
  expect(await screen.findByText(/Approve the request in the Monzo app/)).toBeVisible();
  // Not offered while it waits or backfills: that would throw the approval away.
  expect(screen.queryByRole("button", { name: "Authorise" })).not.toBeInTheDocument();
  expect(listConnections).toHaveBeenCalledTimes(1);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(5000);
  });
  expect(listConnections).toHaveBeenCalledTimes(2);
  await waitFor(() =>
    expect(screen.queryByText(/Approve the request in the Monzo app/)).not.toBeInTheDocument(),
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

const DAY = 86400000;
// The server stores the start of the day after the expiry date (London).
const IBKR = {
  ...CONNECTION,
  id: "0190a0b0-0000-7000-8000-0000000000e1",
  connector: "ibkr",
  display_name: "IBKR main",
  status: "healthy",
  redirect_uri: null,
  credential_expires_at: "2027-03-02T00:00:00Z",
};

function ibkrExpiringIn(days: number) {
  const next = new Date(Date.now() + days * DAY);
  next.setUTCHours(0, 0, 0, 0);
  return { ...IBKR, credential_expires_at: next.toISOString() };
}

it("adds an IBKR connection and clears the token", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  renderPage(client);
  const form = await screen.findByRole("form", { name: "Add Interactive Brokers" });
  expect(
    within(form).getByText(/Create an Activity Flex Query and a Flex Web Service token/),
  ).toBeInTheDocument();
  await user.type(within(form).getByLabelText("Display name"), "IBKR main");
  const token = within(form).getByLabelText("Token");
  expect(token).toHaveAttribute("type", "password");
  await user.type(token, "tok-123");
  await user.type(within(form).getByLabelText("Token expiry date"), "2027-03-01");
  await user.type(within(form).getByLabelText("Query ID"), "987654");
  await user.click(within(form).getByRole("button", { name: "Add Interactive Brokers" }));

  expect(createIbkrConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        display_name: "IBKR main",
        token: "tok-123",
        token_expires_on: "2027-03-01",
        query_id: "987654",
      },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Token")).toHaveValue(""));
  expect(document.body.textContent).not.toContain("tok-123");
  await waitFor(() =>
    expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables))).not
      .toContain("tok-123"),
  );
});

it("updates the token of an IBKR connection", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [IBKR] } as never);
  const user = userEvent.setup();
  renderPage();
  expect(await screen.findByText("Token expires on 2027-03-01")).toBeInTheDocument();
  const form = screen.getByRole("form", { name: "Update token" });
  await user.type(within(form).getByLabelText("Token"), "new-tok");
  await user.type(within(form).getByLabelText("Token expiry date"), "2028-03-01");
  await user.type(within(form).getByLabelText("Query ID"), "111");
  await user.click(within(form).getByRole("button", { name: "Update token" }));

  expect(updateIbkrConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { connection_id: IBKR.id },
      body: { token: "new-tok", token_expires_on: "2028-03-01", query_id: "111" },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Token")).toHaveValue(""));
  expect(document.body.textContent).not.toContain("new-tok");
});

it("highlights a token expiring within 14 days", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [ibkrExpiringIn(5)] } as never);
  renderPage();
  expect(await screen.findByText(/Token expires on/)).toHaveClass("text-amber-600");
});

it("does not highlight a token expiring later", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [ibkrExpiringIn(40)] } as never);
  renderPage();
  expect(await screen.findByText(/Token expires on/)).not.toHaveClass("text-amber-600");
});

it("does not offer Authorise for IBKR and asks to update a rejected token", async () => {
  vi.mocked(listConnections).mockResolvedValue({
    data: [{ ...IBKR, status: "needs_reauth" }],
  } as never);
  renderPage();
  expect(
    await screen.findByText("IBKR rejected the token or query. Update them to resume syncing."),
  ).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Authorise" })).not.toBeInTheDocument();
  expect(screen.queryByText(/Webhooks are off/)).not.toBeInTheDocument();
});

it("uploads a Flex statement", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [IBKR] } as never);
  const user = userEvent.setup();
  renderPage();
  expect(
    await screen.findByText(/run your Flex query in Client Portal with closed accounts selected/),
  ).toBeInTheDocument();
  const file = new File(["<FlexQueryResponse/>"], "statement.xml", { type: "text/xml" });
  await user.upload(screen.getByLabelText("Upload a Flex statement"), file);

  await waitFor(() =>
    expect(uploadIbkrStatement).toHaveBeenCalledWith(
      expect.objectContaining({ path: { connection_id: IBKR.id }, body: { file } }),
    ),
  );
  expect(await screen.findByText("Uploaded statement.xml: 3 records ingested.")).toBeInTheDocument();
});
