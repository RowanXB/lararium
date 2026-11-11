import { createRootRoute, createRoute, createRouter, Link, Outlet } from "@tanstack/react-router";

import { AccountsPage } from "./pages/AccountsPage";
import { InstrumentsPage } from "./pages/InstrumentsPage";
import { NetWorthPage } from "./pages/NetWorthPage";

const NAV = [
  { to: "/", label: "Net worth" },
  { to: "/accounts", label: "Accounts" },
  { to: "/instruments", label: "Instruments" },
] as const;

function Layout() {
  return (
    <div className="min-h-screen">
      <header className="border-b">
        <nav className="mx-auto flex max-w-5xl items-center gap-6 px-4 py-3">
          <span className="font-semibold">financial-gate</span>
          {NAV.map((item) => (
            <Link
              key={item.to}
              to={item.to}
              activeOptions={{ exact: true }}
              className="text-sm text-muted-foreground hover:text-foreground"
              activeProps={{ className: "text-foreground font-medium" }}
            >
              {item.label}
            </Link>
          ))}
        </nav>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}

const rootRoute = createRootRoute({ component: Layout });

const routeTree = rootRoute.addChildren([
  createRoute({ getParentRoute: () => rootRoute, path: "/", component: NetWorthPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/accounts", component: AccountsPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/instruments", component: InstrumentsPage }),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
