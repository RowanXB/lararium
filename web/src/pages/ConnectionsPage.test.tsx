import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  authorizeConnection,
  createBinanceConnection,
  createBitcoinConnection,
  createEtoroConnection,
  createIbkrConnection,
  createMonzoConnection,
  createTrading212Connection,
  listConnections,
  previewBitcoinWallet,
  updateBinanceConnection,
  updateBitcoinGaps,
  updateEtoroConnection,
  updateIbkrConnection,
  updateTrading212Connection,
  uploadEtoroStatement,
  uploadIbkrStatement,
  uploadTrading212Export,
} from "../api";
import { ConnectForm, ConnectionsPage } from "./ConnectionsPage";

function apiType(key: string, institution: string, connector: string, config = {}) {
  return {
    key,
    institution,
    label: "",
    name: institution,
    shape: null,
    api: { connector, config },
    files: [],
    funding: [],
    family: null,
  };
}

// One API type per connector; the Stocks ISA stands for Trading 212.
const API_TYPES = [
  apiType("monzo", "Monzo", "monzo"),
  apiType("ibkr", "Interactive Brokers", "ibkr"),
  apiType("trading212.stocks_isa", "Trading 212", "trading212", { account_kind: "isa" }),
  apiType("etoro", "eToro", "etoro"),
  apiType("binance", "Binance", "binance"),
  apiType("bitcoin_wallet", "Bitcoin", "bitcoin"),
];

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
  createBinanceConnection: vi.fn(async () => ({ data: {} })),
  updateBinanceConnection: vi.fn(async () => ({ data: {} })),
  createBitcoinConnection: vi.fn(async () => ({ data: {} })),
  updateBitcoinGaps: vi.fn(async () => ({ data: {} })),
  previewBitcoinWallet: vi.fn(async () => ({
    data: {
      first_receiving_address: "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu",
      script_type: "p2wpkh",
    },
  })),
  listAccounts: vi.fn(async () => ({
    data: [{ id: "0190a0b0-0000-7000-8000-0000000000a1", name: "Binance Spot" }],
  })),
  createTrading212Connection: vi.fn(async () => ({ data: {} })),
  updateTrading212Connection: vi.fn(async () => ({ data: {} })),
  uploadIbkrStatement: vi.fn(async () => ({ data: { raw_records: 3 } })),
  createEtoroConnection: vi.fn(async () => ({ data: {} })),
  updateEtoroConnection: vi.fn(async () => ({ data: {} })),
  uploadEtoroStatement: vi.fn(async () => ({ data: { raw_records: 7 } })),
  uploadTrading212Export: vi.fn(async () => ({ data: { raw_records: 2 } })),
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
      {/* The add forms live on the add-account page; they're tested here beside the list. */}
      {API_TYPES.map((type) => (
        <ConnectForm key={type.key} type={type} />
      ))}
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
  expect(await within(form).findByText("Interactive Brokers connected")).toBeInTheDocument();
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
  await user.type(within(form).getByLabelText("Token"), " new-tok ");
  await user.type(within(form).getByLabelText("Token expiry date"), "2028-03-01");
  await user.type(within(form).getByLabelText("Query ID"), " 111 ");
  await user.click(within(form).getByRole("button", { name: "Update token" }));

  expect(updateIbkrConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { connection_id: IBKR.id },
      body: { token: "new-tok", token_expires_on: "2028-03-01", query_id: "111" },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Token")).toHaveValue(""));
  expect(document.body.textContent).not.toContain("new-tok");
  expect(await within(form).findByText("Token updated")).toBeInTheDocument();
});

it("keeps the stored query when the update leaves Query ID empty", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [IBKR] } as never);
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Update token" });
  expect(within(form).getByText("Leave empty to keep the current query")).toBeInTheDocument();
  await user.type(within(form).getByLabelText("Token"), "new-tok");
  await user.type(within(form).getByLabelText("Token expiry date"), "2028-03-01");
  await user.click(within(form).getByRole("button", { name: "Update token" }));
  await waitFor(() =>
    expect(updateIbkrConnection).toHaveBeenCalledWith(
      expect.objectContaining({ body: { token: "new-tok", token_expires_on: "2028-03-01" } }),
    ),
  );
});

it("gives every IBKR form its own inputs", async () => {
  const second = { ...IBKR, id: "0190a0b0-0000-7000-8000-0000000000e2", display_name: "IBKR two" };
  vi.mocked(listConnections).mockResolvedValue({ data: [IBKR, second] } as never);
  renderPage();
  await screen.findAllByRole("form", { name: "Update token" });
  for (const label of ["Token", "Query ID", "Token expiry date"]) {
    const inputs = screen.getAllByLabelText(label);
    expect(inputs).toHaveLength(3);
    expect(new Set(inputs.map((input) => input.id)).size).toBe(3);
  }
});

it.each(["created", "disabled"])("does not offer Authorise for a %s IBKR connection", async (status) => {
  vi.mocked(listConnections).mockResolvedValue({ data: [{ ...IBKR, status }] } as never);
  renderPage();
  expect(await screen.findByText("IBKR main")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Authorise" })).not.toBeInTheDocument();
});

it("shows why a statement upload failed", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [IBKR] } as never);
  vi.mocked(uploadIbkrStatement).mockRejectedValueOnce({
    detail: { error: "not_xml", message: "upload the Flex statement as an XML file" },
  });
  const user = userEvent.setup();
  renderPage();
  await user.upload(
    await screen.findByLabelText("Upload a Flex statement"),
    new File(["x"], "statement.xml", { type: "text/xml" }),
  );
  expect(await screen.findByText("upload the Flex statement as an XML file")).toBeInTheDocument();
});

it("highlights a token expiring within 14 days", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [ibkrExpiringIn(5)] } as never);
  renderPage();
  expect(await screen.findByText(/Token expires on/)).toHaveClass("text-warning");
});

it("does not highlight a token expiring later", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [ibkrExpiringIn(40)] } as never);
  renderPage();
  expect(await screen.findByText(/Token expires on/)).not.toHaveClass("text-warning");
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

const TRADING212 = {
  ...CONNECTION,
  id: "0190a0b0-0000-7000-8000-0000000000f1",
  connector: "trading212",
  display_name: "Trading 212 ISA",
  status: "needs_reauth",
  redirect_uri: null,
};

it("adds a Trading 212 connection and clears the keys", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  renderPage(client);
  const form = await screen.findByRole("form", { name: "Add Trading 212" });
  expect(
    within(form).getByText(
      "Create an API key in the Trading 212 app under Settings → API (Beta), for this account only, with read permissions.",
    ),
  ).toBeInTheDocument();
  await user.type(within(form).getByLabelText("Display name"), "Trading 212 ISA");
  expect(within(form).queryByLabelText("Account type")).toBeNull();
  const key = within(form).getByLabelText("API key");
  const secret = within(form).getByLabelText("API secret");
  expect(key).toHaveAttribute("type", "password");
  expect(secret).toHaveAttribute("type", "password");
  await user.type(key, "key-123");
  await user.type(secret, "secret-456");
  await user.click(within(form).getByRole("button", { name: "Add Trading 212" }));

  expect(createTrading212Connection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        display_name: "Trading 212 ISA",
        account_kind: "isa",
        api_key: "key-123",
        api_secret: "secret-456",
      },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("API key")).toHaveValue(""));
  expect(within(form).getByLabelText("API secret")).toHaveValue("");
  expect(await within(form).findByText("Trading 212 connected")).toBeInTheDocument();
  expect(document.body.textContent).not.toContain("key-123");
  await waitFor(() => {
    const kept = JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables));
    expect(kept).not.toContain("key-123");
    expect(kept).not.toContain("secret-456");
  });
});

it("uploads a Trading 212 export", async () => {
  vi.mocked(listConnections).mockResolvedValue({
    data: [{ ...TRADING212, status: "healthy" }],
  } as never);
  const user = userEvent.setup();
  renderPage();
  expect(
    await screen.findByText(
      "Optional: adds details the API doesn't give. Export it in the Trading 212 app under History.",
    ),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("Upload eToro statement (xlsx)")).not.toBeInTheDocument();
  const file = new File(["Action,ID"], "export.csv", { type: "text/csv" });
  await user.upload(screen.getByLabelText("Upload Trading 212 export (CSV, optional)"), file);

  await waitFor(() =>
    expect(uploadTrading212Export).toHaveBeenCalledWith(
      expect.objectContaining({ path: { connection_id: TRADING212.id }, body: { file } }),
    ),
  );
  expect(await screen.findByText("Uploaded export.csv: 2 records ingested.")).toBeInTheDocument();
});

it.each([
  ["Trading 212", () => TRADING212],
  ["eToro", () => ETORO],
])("offers Update keys on a disabled %s connection, which brings it back", async (_, made) => {
  vi.mocked(listConnections).mockResolvedValue({
    data: [{ ...made(), status: "disabled" }],
  } as never);
  renderPage();
  const form = await screen.findByRole("form", { name: "Update keys" });
  expect(within(form).getByRole("button", { name: "Update keys" })).toBeEnabled();
});

it("shows why Trading 212 refused the key", async () => {
  vi.mocked(createTrading212Connection).mockRejectedValueOnce({
    detail: { error: "key_rejected", message: "Trading 212 rejected this key" },
  });
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Add Trading 212" });
  await user.type(within(form).getByLabelText("Display name"), "T");
  await user.type(within(form).getByLabelText("API key"), "k");
  await user.type(within(form).getByLabelText("API secret"), "s");
  await user.click(within(form).getByRole("button", { name: "Add Trading 212" }));
  expect(await within(form).findByText("Trading 212 rejected this key")).toBeInTheDocument();
});

it("updates the keys of a Trading 212 connection", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [TRADING212] } as never);
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Update keys" });
  await user.type(within(form).getByLabelText("API key"), " new-key ");
  await user.type(within(form).getByLabelText("API secret"), "new-secret");
  await user.click(within(form).getByRole("button", { name: "Update keys" }));

  expect(updateTrading212Connection).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { connection_id: TRADING212.id },
      body: { api_key: "new-key", api_secret: "new-secret" },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("API key")).toHaveValue(""));
  expect(within(form).getByLabelText("API secret")).toHaveValue("");
  expect(await within(form).findByText("Keys updated")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Authorise" })).not.toBeInTheDocument();
});

const ETORO = {
  ...CONNECTION,
  id: "0190a0b0-0000-7000-8000-0000000000a1",
  connector: "etoro",
  display_name: "eToro",
  status: "healthy",
  redirect_uri: null,
};

it("adds an eToro connection", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  renderPage(client);
  const form = await screen.findByRole("form", { name: "Add eToro" });
  expect(
    within(form).getByText(
      "Create a key in eToro under Settings → Trading → API Key Management, Environment Real, Permission Read.",
    ),
  ).toBeInTheDocument();
  await user.type(within(form).getByLabelText("Display name"), "eToro");
  const publicKey = within(form).getByLabelText("Public API Key");
  const userKey = within(form).getByLabelText("User Key");
  expect(publicKey).toHaveAttribute("type", "password");
  expect(userKey).toHaveAttribute("type", "password");
  await user.type(publicKey, " public-123 ");
  await user.type(userKey, "user-456");
  await user.click(within(form).getByRole("button", { name: "Add eToro" }));

  expect(createEtoroConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: { display_name: "eToro", public_api_key: "public-123", user_key: "user-456" },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Public API Key")).toHaveValue(""));
  expect(within(form).getByLabelText("User Key")).toHaveValue("");
  expect(await within(form).findByText("eToro connected")).toBeInTheDocument();
  expect(document.body.textContent).not.toContain("public-123");
  await waitFor(() => {
    const kept = JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables));
    expect(kept).not.toContain("public-123");
    expect(kept).not.toContain("user-456");
  });
});

it("shows why eToro refused the key", async () => {
  vi.mocked(createEtoroConnection).mockRejectedValueOnce({
    detail: {
      error: "key_not_read_only",
      message: "This eToro key can trade or move money. Create a key with Read permission.",
    },
  });
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Add eToro" });
  await user.type(within(form).getByLabelText("Display name"), "E");
  await user.type(within(form).getByLabelText("Public API Key"), "p");
  await user.type(within(form).getByLabelText("User Key"), "u");
  await user.click(within(form).getByRole("button", { name: "Add eToro" }));
  expect(
    await within(form).findByText(
      "This eToro key can trade or move money. Create a key with Read permission.",
    ),
  ).toBeInTheDocument();
});

it("updates the keys of an eToro connection that needs them", async () => {
  vi.mocked(listConnections).mockResolvedValue({
    data: [{ ...ETORO, status: "needs_reauth" }],
  } as never);
  const user = userEvent.setup();
  renderPage();
  expect(
    await screen.findByText("eToro rejected the keys. Update them to resume syncing."),
  ).toHaveAttribute("role", "alert");
  const form = screen.getByRole("form", { name: "Update keys" });
  await user.type(within(form).getByLabelText("Public API Key"), "new-public");
  await user.type(within(form).getByLabelText("User Key"), "new-user");
  await user.click(within(form).getByRole("button", { name: "Update keys" }));

  expect(updateEtoroConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { connection_id: ETORO.id },
      body: { public_api_key: "new-public", user_key: "new-user" },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Public API Key")).toHaveValue(""));
  expect(within(form).getByLabelText("User Key")).toHaveValue("");
  expect(await within(form).findByText("Keys updated")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Authorise" })).not.toBeInTheDocument();
});

it("uploads an eToro statement", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [ETORO] } as never);
  const user = userEvent.setup();
  renderPage();
  expect(
    await screen.findByText(
      "Download it from eToro's web app: Portfolio → History → Account Statement, as Excel. The first one must start on or before the day you opened the account.",
    ),
  ).toBeInTheDocument();
  expect(screen.queryByLabelText("Upload a Flex statement")).not.toBeInTheDocument();
  const file = new File(["PK"], "statement.xlsx", {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  await user.upload(screen.getByLabelText("Upload eToro statement (xlsx)"), file);

  await waitFor(() =>
    expect(uploadEtoroStatement).toHaveBeenCalledWith(
      expect.objectContaining({ path: { connection_id: ETORO.id }, body: { file } }),
    ),
  );
  expect(await screen.findByText("Uploaded statement.xlsx: 7 records ingested.")).toBeInTheDocument();
});

it("shows why an eToro statement upload failed", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [ETORO] } as never);
  vi.mocked(uploadEtoroStatement).mockRejectedValueOnce({
    detail: { error: "statement_invalid", message: "this statement is for another eToro user" },
  });
  const user = userEvent.setup();
  renderPage();
  await user.upload(
    await screen.findByLabelText("Upload eToro statement (xlsx)"),
    new File(["PK"], "statement.xlsx"),
  );
  expect(await screen.findByText("this statement is for another eToro user")).toBeInTheDocument();
});

const BINANCE = {
  ...CONNECTION,
  id: "0190a0b0-0000-7000-8000-0000000000b1",
  connector: "binance",
  display_name: "Binance main",
  status: "healthy",
  redirect_uri: null,
  webhook_enabled: false,
  binance: {
    wallets: ["Spot", "Funding"],
    first_sync_done: true,
    differences: [],
    flags: [],
  },
};

it("adds a Binance connection with an HMAC key and clears the secrets", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  renderPage(client);
  const form = await screen.findByRole("form", { name: "Add Binance" });
  expect(
    within(form).getByText(
      (_, element) =>
        element?.tagName === "P" &&
        element.textContent ===
          "Create an API key on Binance with only Enable Reading ticked. Any other permission is refused.",
    ),
  ).toBeInTheDocument();
  expect(within(form).getByText("Enable Reading").tagName).toBe("STRONG");
  await user.type(within(form).getByLabelText("Display name"), "Binance main");
  const key = within(form).getByLabelText("API key");
  const secret = within(form).getByLabelText("Secret key");
  expect(key).toHaveAttribute("type", "password");
  expect(secret).toHaveAttribute("type", "password");
  expect(within(form).queryByLabelText("Private key")).toBeNull();
  await user.type(key, "key-123");
  await user.type(secret, "sec-456");
  await user.click(within(form).getByRole("button", { name: "Add Binance" }));

  expect(createBinanceConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        display_name: "Binance main",
        key_type: "hmac",
        api_key: "key-123",
        api_secret: "sec-456",
      },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("API key")).toHaveValue(""));
  expect(within(form).getByLabelText("Secret key")).toHaveValue("");
  expect(document.body.textContent).not.toContain("sec-456");
  await waitFor(() =>
    expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables))).not
      .toContain("sec-456"),
  );
});

it("adds a Binance connection with an Ed25519 key and its passphrase", async () => {
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Add Binance" });
  await user.type(within(form).getByLabelText("Display name"), "Binance ed");
  await user.click(within(form).getByRole("combobox", { name: "Key type" }));
  await user.click(await screen.findByRole("option", { name: "Ed25519" }));
  expect(within(form).queryByLabelText("Secret key")).toBeNull();
  await user.type(within(form).getByLabelText("API key"), "key-789");
  const pem = within(form).getByLabelText("Private key");
  expect(pem).toHaveAttribute("autocomplete", "off");
  await user.type(pem, "line-one{Enter}line-two");
  await user.type(within(form).getByLabelText("Passphrase (optional)"), "pass-1");
  await user.click(within(form).getByRole("button", { name: "Add Binance" }));

  expect(createBinanceConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        display_name: "Binance ed",
        key_type: "ed25519",
        api_key: "key-789",
        private_key: "line-one\nline-two",
        private_key_passphrase: "pass-1",
      },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Private key")).toHaveValue(""));
  expect(within(form).getByLabelText("Passphrase (optional)")).toHaveValue("");
});

it("shows the message of a rejected Binance key and clears the secrets", async () => {
  vi.mocked(createBinanceConnection).mockRejectedValueOnce({
    detail: { error: "key_rejected", message: "this key can do more than read: enableWithdrawals" },
  });
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Add Binance" });
  await user.type(within(form).getByLabelText("Display name"), "Binance main");
  await user.type(within(form).getByLabelText("API key"), "key-123");
  await user.type(within(form).getByLabelText("Secret key"), "sec-456");
  await user.click(within(form).getByRole("button", { name: "Add Binance" }));
  expect(
    await within(form).findByText("this key can do more than read: enableWithdrawals"),
  ).toBeInTheDocument();
  expect(within(form).getByLabelText("API key")).toHaveValue("");
  expect(within(form).getByLabelText("Secret key")).toHaveValue("");
});

it("shows a Binance connection's wallets, progress, differences and flags", async () => {
  vi.mocked(listConnections).mockResolvedValue({
    data: [
      {
        ...BINANCE,
        binance: {
          wallets: ["Spot", "Funding"],
          first_sync_done: false,
          differences: [
            {
              account_id: "0190a0b0-0000-7000-8000-0000000000a1",
              symbol: "AAA",
              from_at: "2026-03-01T10:15:00Z",
              to_at: "2026-03-02T11:45:30Z",
            },
            {
              account_id: "0190a0b0-0000-7000-8000-0000000000ff",
              symbol: "BBB",
              from_at: "2026-03-01T00:00:00Z",
              to_at: "2026-03-01T01:00:00Z",
            },
          ],
          flags: ["Portfolio Margin enabled"],
        },
      },
    ],
  } as never);
  renderPage();
  expect(await screen.findByText("First sync in progress")).toBeInTheDocument();
  expect(screen.getByText(/Spot, Funding/)).toBeInTheDocument();
  expect(
    await screen.findByText("Binance Spot: AAA, 2026-03-01 10:15 to 2026-03-02 11:45 (UTC)"),
  ).toBeInTheDocument();
  expect(
    screen.getByText("Binance account: BBB, 2026-03-01 00:00 to 2026-03-01 01:00 (UTC)"),
  ).toBeInTheDocument();
  expect(screen.getByText("Portfolio Margin enabled")).toBeInTheDocument();
});

it("hides the first-sync notice once it is done", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [BINANCE] } as never);
  renderPage();
  expect(await screen.findByText("Binance main")).toBeInTheDocument();
  expect(screen.queryByText("First sync in progress")).toBeNull();
});

it("updates a Binance key and clears the secrets", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [BINANCE] } as never);
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Update key" });
  await user.type(within(form).getByLabelText("API key"), "key-new");
  await user.type(within(form).getByLabelText("Secret key"), "sec-new");
  await user.click(within(form).getByRole("button", { name: "Update key" }));
  expect(updateBinanceConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { connection_id: BINANCE.id },
      body: { key_type: "hmac", api_key: "key-new", api_secret: "sec-new" },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Secret key")).toHaveValue(""));
  expect(await within(form).findByText("Key updated")).toBeInTheDocument();
  expect(listConnections).toHaveBeenCalledTimes(2);
});

it("shows why a Binance key update failed", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [BINANCE] } as never);
  vi.mocked(updateBinanceConnection).mockRejectedValueOnce({
    detail: { error: "another_account", message: "this key belongs to another Binance account" },
  });
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Update key" });
  await user.type(within(form).getByLabelText("API key"), "k");
  await user.type(within(form).getByLabelText("Secret key"), "s");
  await user.click(within(form).getByRole("button", { name: "Update key" }));
  expect(
    await within(form).findByText("this key belongs to another Binance account"),
  ).toBeInTheDocument();
});

it("gives Binance connections no Authorise or IBKR forms", async () => {
  vi.mocked(listConnections).mockResolvedValue({ data: [BINANCE] } as never);
  renderPage();
  expect(await screen.findByText("Binance main")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Authorise" })).toBeNull();
  expect(screen.queryByRole("form", { name: "Update token" })).toBeNull();
  expect(screen.queryByText(/Webhooks are off/)).toBeNull();
});

// BIP84's published test account key (m/84'/0'/0' of "abandon ... about").
const ZPUB =
  "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs";

it("adds a Bitcoin wallet after showing its first receiving address", async () => {
  const user = userEvent.setup();
  const client = new QueryClient();
  renderPage(client);
  const form = await screen.findByRole("form", { name: "Add Bitcoin wallet" });
  expect(
    within(form).getByText(
      "In Electrum: Wallet → Information → Master Public Key. Never enter your seed phrase.",
    ),
  ).toBeInTheDocument();
  expect(
    within(form).getByText(
      "Lightning channel balances can't be seen from a public key; record them as a manual account.",
    ),
  ).toBeInTheDocument();
  await user.click(within(form).getByRole("combobox", { name: "Wallet form" }));
  expect((await screen.findAllByRole("option")).map((option) => option.textContent)).toEqual([
    "Electrum master public key",
    "Output descriptor",
    "Address list",
  ]);
  await user.click(screen.getByRole("option", { name: "Electrum master public key" }));
  await user.type(within(form).getByLabelText("Display name"), "Electrum");
  await user.click(within(form).getByLabelText("Master public key"));
  await user.paste(ZPUB);

  expect(await within(form).findByText("bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu")).toBeInTheDocument();
  expect(previewBitcoinWallet).toHaveBeenLastCalledWith(
    expect.objectContaining({
      body: { form: "electrum", electrum_type: "single", keys: [ZPUB] },
    }),
  );
  await user.click(within(form).getByText("Advanced"));
  expect(
    within(form).getByText("Compare the balance with Electrum's. If it is lower, raise the gap limit."),
  ).toBeInTheDocument();
  await user.type(within(form).getByLabelText("Receiving gap limit"), "50");
  await user.click(within(form).getByRole("button", { name: "Add Bitcoin wallet" }));

  expect(createBitcoinConnection).toHaveBeenCalledWith(
    expect.objectContaining({
      body: {
        display_name: "Electrum",
        form: "electrum",
        electrum_type: "single",
        keys: [ZPUB],
        gaps: { d0: { "0": 50 } },
      },
    }),
  );
  await waitFor(() => expect(within(form).getByLabelText("Master public key")).toHaveValue(""));
  await waitFor(() => expect(within(form).getByLabelText("Receiving gap limit")).toHaveValue(null));
  await waitFor(() =>
    expect(JSON.stringify(client.getMutationCache().getAll().map((m) => m.state.variables))).not
      .toContain(ZPUB),
  );
});

it("sends nothing for a seed phrase and says why", async () => {
  const user = userEvent.setup();
  renderPage();
  const form = await screen.findByRole("form", { name: "Add Bitcoin wallet" });
  await user.type(within(form).getByLabelText("Display name"), "Electrum");
  await user.type(
    within(form).getByLabelText("Master public key"),
    "legal winner thank year wave sausage worth useful legal winner thank yellow",
  );

  expect(await within(form).findByRole("alert")).toHaveTextContent(
    "This looks like a seed phrase. Never enter it anywhere: Lararium needs the master public key from Wallet → Information.",
  );
  await new Promise((resolve) => setTimeout(resolve, 400));
  expect(previewBitcoinWallet).not.toHaveBeenCalled();
  await user.click(within(form).getByRole("button", { name: "Add Bitcoin wallet" }));
  expect(createBitcoinConnection).not.toHaveBeenCalled();
  expect(within(form).getByLabelText("Master public key")).toHaveValue("");
});

it("shows a Bitcoin wallet's state, never its key", async () => {
  vi.mocked(listConnections).mockResolvedValue({
    data: [
      {
        ...CONNECTION,
        id: "0190a0b0-0000-7000-8000-0000000000c1",
        connector: "bitcoin",
        display_name: "Cold wallet",
        status: "healthy",
        redirect_uri: null,
        bitcoin: {
          form: "electrum",
          electrum_type: "multisig",
          scanned: { receiving: 21, change: 11 },
          used: { receiving: 1, change: 1 },
          last_check: "agrees",
          unconfirmed: 2,
          server: "mempool.space",
          gaps: { d0: { "0": 40 } },
        },
      },
    ],
  } as never);
  renderPage();
  expect(await screen.findByText("Cold wallet")).toBeInTheDocument();
  expect(screen.getByText("Electrum master public key, Multi-signature")).toBeInTheDocument();
  expect(
    screen.getByText("Addresses scanned: 21 receiving, 11 change; used: 1 receiving, 1 change"),
  ).toBeInTheDocument();
  expect(screen.getByText("Last balance check: agrees")).toBeInTheDocument();
  expect(screen.getByText("Unconfirmed transactions waiting: 2")).toBeInTheDocument();
  expect(screen.getByText("Server: mempool.space")).toBeInTheDocument();

  const gaps = screen.getByRole("form", { name: "Gap limits" });
  const user = userEvent.setup();
  const receiving = within(gaps).getByLabelText("Receiving gap limit");
  expect(receiving).toHaveValue(40);
  await user.clear(receiving);
  await user.type(receiving, "60");
  await user.type(within(gaps).getByLabelText("Change gap limit"), "15");
  await user.click(within(gaps).getByRole("button", { name: "Save gap limits" }));
  expect(updateBitcoinGaps).toHaveBeenCalledWith(
    expect.objectContaining({
      path: { connection_id: "0190a0b0-0000-7000-8000-0000000000c1" },
      body: { gaps: { d0: { "0": 60, "1": 15 } } },
    }),
  );
  expect(await within(gaps).findByText("Gap limits saved; syncing")).toBeInTheDocument();
});
