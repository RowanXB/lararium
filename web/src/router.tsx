import {
  createRootRoute,
  createRoute,
  createRouter,
  Link,
  Outlet,
  useRouterState,
} from "@tanstack/react-router";
import { DatabaseZap, LayoutDashboard, Settings2, Wallet } from "lucide-react";

import { AccountsPage } from "./pages/AccountsPage";
import { AddAccountPage } from "./pages/AddAccountPage";
import { ConnectionsPage } from "./pages/ConnectionsPage";
import { ImportBatchPage } from "./pages/ImportBatchPage";
import { ImportProfilesPage } from "./pages/ImportProfilesPage";
import { ImportsPage } from "./pages/ImportsPage";
import { InstrumentsPage } from "./pages/InstrumentsPage";
import { OverviewPage } from "./pages/OverviewPage";
import { PricesPage } from "./pages/PricesPage";
import { TransfersPage } from "./pages/TransfersPage";

// Four places, by what the owner comes to do; the pages inside one are its tabs.
const SECTIONS = [
  { label: "Overview", icon: LayoutDashboard, pages: [{ to: "/", label: "Overview" }] },
  {
    label: "Accounts",
    icon: Wallet,
    pages: [
      { to: "/accounts", label: "Accounts" },
      { to: "/transfers", label: "Transfers" },
    ],
  },
  {
    label: "Data",
    icon: DatabaseZap,
    pages: [
      { to: "/connections", label: "Connections" },
      { to: "/imports", label: "Statement files" },
    ],
  },
  {
    label: "Settings",
    icon: Settings2,
    pages: [
      { to: "/instruments", label: "Instruments" },
      { to: "/prices", label: "Prices" },
      { to: "/import-profiles", label: "Import profiles" },
    ],
  },
] as const;

function within(pathname: string, to: string): boolean {
  return to === "/" ? pathname === "/" : pathname === to || pathname.startsWith(`${to}/`);
}

function Layout() {
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const current = SECTIONS.find((section) => section.pages.some((p) => within(pathname, p.to)));
  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-10 border-b bg-background/80 backdrop-blur">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-8 gap-y-2 px-4 py-3">
          <Link to="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <span className="size-2.5 rounded-full bg-primary shadow-[0_0_12px_var(--primary)]" />
            Lararium
          </Link>
          <nav aria-label="Main" className="flex flex-wrap gap-1">
            {SECTIONS.map((section) => (
              <Link
                key={section.label}
                to={section.pages[0].to}
                aria-current={section === current ? "page" : undefined}
                className="flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground aria-[current=page]:bg-secondary aria-[current=page]:text-foreground"
              >
                <section.icon className="size-4" aria-hidden />
                {section.label}
              </Link>
            ))}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl space-y-6 px-4 py-6">
        {current && current.pages.length > 1 && (
          <nav aria-label={current.label} className="flex flex-wrap gap-6 border-b">
            {current.pages.map((page) => (
              <Link
                key={page.to}
                to={page.to}
                aria-current={within(pathname, page.to) ? "page" : undefined}
                className="-mb-px border-b-2 border-transparent pb-2 text-sm text-muted-foreground transition-colors hover:text-foreground aria-[current=page]:border-primary aria-[current=page]:text-foreground"
              >
                {page.label}
              </Link>
            ))}
          </nav>
        )}
        <Outlet />
      </main>
    </div>
  );
}

const rootRoute = createRootRoute({ component: Layout });

const importBatchRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/imports/$batchId",
  component: function ImportBatchRoute() {
    const { batchId } = importBatchRoute.useParams();
    return <ImportBatchPage batchId={batchId} />;
  },
});

const routeTree = rootRoute.addChildren([
  createRoute({ getParentRoute: () => rootRoute, path: "/", component: OverviewPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/accounts", component: AccountsPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/accounts/new", component: AddAccountPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/transfers", component: TransfersPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/instruments", component: InstrumentsPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/prices", component: PricesPage }),
  createRoute({ getParentRoute: () => rootRoute, path: "/imports", component: ImportsPage }),
  importBatchRoute,
  createRoute({
    getParentRoute: () => rootRoute,
    path: "/import-profiles",
    component: ImportProfilesPage,
  }),
  createRoute({ getParentRoute: () => rootRoute, path: "/connections", component: ConnectionsPage }),
]);

export const router = createRouter({ routeTree });

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
}
