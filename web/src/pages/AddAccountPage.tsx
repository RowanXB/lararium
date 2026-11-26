import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowLeft, FileText, Landmark, PencilLine, RefreshCw } from "lucide-react";
import { type ReactNode, useState } from "react";

import { getImportCatalog, type ImportCatalogEntryOut } from "../api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { errorMessage } from "../lib/errors";
import { NewAccountForm } from "./AccountsPage";
import { ConnectForm, type Provider, PROVIDERS } from "./ConnectionsPage";
import { UploadForm } from "./ImportsPage";

type Choice =
  | { kind: "connect"; provider: Provider; label: string }
  | { kind: "statement"; institution: string; formats: ImportCatalogEntryOut[] }
  | { kind: "other-file" }
  | { kind: "manual" };

// Each bank's formats, the recommended one (the most complete) first.
function byInstitution(entries: ImportCatalogEntryOut[]): [string, ImportCatalogEntryOut[]][] {
  const groups = new Map<string, ImportCatalogEntryOut[]>();
  for (const entry of entries) {
    groups.set(entry.institution, [...(groups.get(entry.institution) ?? []), entry]);
  }
  return [...groups].map(([institution, group]) => [
    institution,
    [...group].sort((a, b) => Number(b.recommended) - Number(a.recommended)),
  ]);
}

function Tile({
  title,
  detail,
  icon,
  onClick,
}: {
  title: string;
  detail: ReactNode;
  icon: ReactNode;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex items-center gap-3 rounded-xl border bg-card px-4 py-3 text-left transition-colors hover:border-primary focus-visible:border-primary focus-visible:outline-none"
    >
      <span className="shrink-0 text-primary">{icon}</span>
      <span className="grid min-w-0">
        <span className="truncate font-medium">{title}</span>
        <span className="truncate text-sm text-muted-foreground">{detail}</span>
      </span>
    </button>
  );
}

function Group({ title, note, children }: { title: string; note: string; children: ReactNode }) {
  return (
    <section className="space-y-3">
      <div>
        <h2 className="font-medium">{title}</h2>
        <p className="text-sm text-muted-foreground">{note}</p>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{children}</div>
    </section>
  );
}

function Picker({ onPick }: { onPick: (choice: Choice) => void }) {
  const catalog = useQuery({
    queryKey: ["import-catalog"],
    queryFn: async () => (await getImportCatalog({ throwOnError: true })).data,
  });
  return (
    <div className="space-y-8">
      <Group title="Connect" note="Balances and payments arrive on their own, every day.">
        {PROVIDERS.map((provider) => (
          <Tile
            key={provider.value}
            title={provider.label}
            detail="Syncs automatically"
            icon={<RefreshCw className="size-5" aria-hidden />}
            onClick={() => onPick({ kind: "connect", provider: provider.value, label: provider.label })}
          />
        ))}
      </Group>
      <Group
        title="Upload statements"
        note="Download a statement from your bank's website or app, then upload it here."
      >
        {catalog.error && (
          <p className="text-sm text-destructive">{errorMessage(catalog.error)}</p>
        )}
        {byInstitution(catalog.data ?? []).map(([institution, formats]) => {
          const best = formats.find((entry) => entry.recommended) ?? formats[0];
          return (
            <Tile
              key={institution}
              title={institution}
              detail={
                <>
                  Recommended: <span className="text-foreground">{best.label}</span>
                </>
              }
              icon={<Landmark className="size-5" aria-hidden />}
              onClick={() => onPick({ kind: "statement", institution, formats })}
            />
          );
        })}
      </Group>
      <Group title="Something else" note="Any other bank, or anything you'd rather type in.">
        <Tile
          title="Another bank's file"
          detail="CSV, OFX or QIF, with a format you describe once"
          icon={<FileText className="size-5" aria-hidden />}
          onClick={() => onPick({ kind: "other-file" })}
        />
        <Tile
          title="Type it in yourself"
          detail="A house, cash, a loan: enter the balance by hand"
          icon={<PencilLine className="size-5" aria-hidden />}
          onClick={() => onPick({ kind: "manual" })}
        />
      </Group>
    </div>
  );
}

function StatementPanel({
  institution,
  formats,
}: {
  institution: string;
  formats: ImportCatalogEntryOut[];
}) {
  const best = formats.find((entry) => entry.recommended) ?? formats[0];
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>Which file to download from {institution}</CardTitle>
          <CardDescription>
            Any of these works, and the upload recognises which one it is. The recommended one
            carries the most detail about each payment.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul aria-label={`${institution} formats`} className="flex flex-wrap gap-2">
            {formats.map((entry) => (
              <li
                key={entry.label}
                className={
                  entry === best
                    ? "rounded-full border border-primary bg-primary/10 px-3 py-1 text-sm text-primary"
                    : "rounded-full border px-3 py-1 text-sm text-muted-foreground"
                }
              >
                {entry.label}
                {entry === best && " · Recommended"}
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
      <UploadForm formats={formats} />
    </div>
  );
}

export function AddAccountPage() {
  const navigate = useNavigate();
  const [choice, setChoice] = useState<Choice>();
  const title =
    choice === undefined
      ? "Add an account"
      : choice.kind === "connect"
        ? `Connect ${choice.label}`
        : choice.kind === "statement"
          ? `Upload a ${choice.institution} statement`
          : choice.kind === "other-file"
            ? "Upload another bank's file"
            : "Type in an account";

  return (
    <div className="space-y-6">
      <div className="space-y-1">
        {choice !== undefined ? (
          <button
            type="button"
            onClick={() => setChoice(undefined)}
            className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-4" aria-hidden />
            All ways to add
          </button>
        ) : (
          <Link to="/accounts" className="text-sm text-muted-foreground hover:text-foreground">
            Back to accounts
          </Link>
        )}
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
      </div>
      {choice === undefined && <Picker onPick={setChoice} />}
      {choice?.kind === "connect" && (
        <ConnectForm
          provider={choice.provider}
          // Its accounts appear once it syncs; Monzo first asks to be authorised there.
          onAdded={() => void navigate({ to: "/connections" })}
        />
      )}
      {choice?.kind === "statement" && (
        <StatementPanel institution={choice.institution} formats={choice.formats} />
      )}
      {choice?.kind === "other-file" && (
        <div className="space-y-3">
          <p className="text-sm text-muted-foreground">
            The first time, describe the file's columns in{" "}
            <Link to="/import-profiles" className="text-primary hover:underline">
              Settings → Import profiles
            </Link>
            ; after that, pick it as the profile here.
          </p>
          <UploadForm />
        </div>
      )}
      {choice?.kind === "manual" && (
        <NewAccountForm onAdded={() => void navigate({ to: "/accounts" })} />
      )}
    </div>
  );
}
