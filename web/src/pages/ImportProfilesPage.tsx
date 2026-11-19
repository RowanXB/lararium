import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useId, useState } from "react";

import {
  createImportProfile,
  getImportCatalog,
  type ImportCatalogEntryOut,
  type ImportProfileCreate,
  type ImportProfileOut,
  listImportProfiles,
  type ProfileConfig,
  updateImportProfile,
} from "../api";
import { TextField } from "../components/TextField";
import { Button } from "../components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { Label } from "../components/ui/label";
import { errorMessage } from "../lib/errors";

const PROFILES = ["import-profiles"];

const CONFIG_PLACEHOLDER = `{
  "format": "csv",
  "columns": {"date": "Date", "description": ["Description"], "amount": "Amount"},
  "date_format": "%d/%m/%Y"
}`;

function ProfileForm({
  editing,
  onDone,
}: {
  editing: ImportProfileOut | undefined;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const configId = useId();
  const [name, setName] = useState(editing?.name ?? "");
  const [config, setConfig] = useState(
    editing ? JSON.stringify(editing.config, null, 2) : "",
  );
  const [invalidJson, setInvalidJson] = useState(false);

  const mutation = useMutation({
    mutationFn: async (body: ImportProfileCreate) =>
      editing
        ? (
            await updateImportProfile({
              path: { profile_id: editing.id },
              body,
              throwOnError: true,
            })
          ).data
        : (await createImportProfile({ body, throwOnError: true })).data,
    onSuccess: async () => {
      setName("");
      setConfig("");
      await queryClient.invalidateQueries({ queryKey: PROFILES });
      onDone();
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    let parsed: unknown;
    try {
      parsed = JSON.parse(config);
    } catch {
      setInvalidJson(true);
      return;
    }
    setInvalidJson(false);
    // The server validates the config's shape and answers 422 with what's wrong.
    mutation.mutate({ name: name.trim(), config: parsed as ProfileConfig });
  }

  const title = editing ? "Edit profile" : "New profile";
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <form aria-label={title} onSubmit={submit} className="grid gap-4">
          <TextField label="Name" value={name} onChange={setName} required />
          <div className="grid gap-1.5">
            <Label htmlFor={configId}>Config (JSON)</Label>
            <textarea
              id={configId}
              value={config}
              required
              rows={10}
              spellCheck={false}
              placeholder={CONFIG_PLACEHOLDER}
              aria-invalid={invalidJson || undefined}
              onChange={(e) => setConfig(e.target.value)}
              className="w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 font-mono text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 aria-invalid:border-destructive"
            />
          </div>
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={mutation.isPending}>
              {editing ? "Save profile" : "Create profile"}
            </Button>
            {editing && (
              <Button type="button" variant="outline" onClick={onDone}>
                Cancel
              </Button>
            )}
            {invalidJson && <p className="text-sm text-destructive">Config is not valid JSON</p>}
            {mutation.isError && (
              <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
            )}
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

// Each institution's formats, the recommended one first.
function byInstitution(entries: ImportCatalogEntryOut[]): [string, ImportCatalogEntryOut[]][] {
  const groups = new Map<string, ImportCatalogEntryOut[]>();
  for (const entry of entries) {
    groups.set(entry.institution, [...(groups.get(entry.institution) ?? []), entry]);
  }
  return [...groups].map(([institution, group]) => [
    institution,
    [...group].sort((a, b) => (b.recommended ? 1 : 0) - (a.recommended ? 1 : 0)),
  ]);
}

function Catalog() {
  const queryClient = useQueryClient();
  const catalog = useQuery({
    queryKey: ["import-catalog"],
    queryFn: async () => (await getImportCatalog({ throwOnError: true })).data,
  });
  const mutation = useMutation({
    mutationFn: async (entry: ImportCatalogEntryOut) =>
      (
        await createImportProfile({
          body: { name: `${entry.institution} ${entry.label}`, config: entry.config },
          throwOnError: true,
        })
      ).data,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: PROFILES });
    },
  });

  return (
    <div className="space-y-4 rounded-lg border p-4">
      {catalog.error && <p className="text-sm text-destructive">{errorMessage(catalog.error)}</p>}
      {byInstitution(catalog.data ?? []).map(([institution, entries]) => (
        <section key={institution} className="space-y-2">
          <h3 className="text-sm font-medium">{institution}</h3>
          <ul aria-label={institution} className="divide-y">
            {entries.map((entry) => (
              <li key={entry.label} className="flex items-center gap-3 py-2 text-sm">
                <span>{entry.label}</span>
                {entry.recommended && (
                  <span className="rounded-md bg-primary/10 px-1.5 py-0.5 text-xs font-medium text-primary">
                    Recommended
                  </span>
                )}
                <span className="text-muted-foreground uppercase">{entry.format}</span>
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto"
                  disabled={mutation.isPending}
                  onClick={() => mutation.mutate(entry)}
                >
                  Add
                </Button>
              </li>
            ))}
          </ul>
        </section>
      ))}
      {mutation.isError && (
        <p className="text-sm text-destructive">{errorMessage(mutation.error)}</p>
      )}
      {mutation.isSuccess && <p className="text-sm text-muted-foreground">Profile added</p>}
    </div>
  );
}

export function ImportProfilesPage() {
  const profiles = useQuery({
    queryKey: PROFILES,
    queryFn: async () => (await listImportProfiles({ throwOnError: true })).data,
  });
  const [showCatalog, setShowCatalog] = useState(false);
  const [editingId, setEditingId] = useState<string>();
  const editing = profiles.data?.find((profile) => profile.id === editingId);

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Import profiles</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {profiles.error && (
            <p className="text-sm text-destructive">{errorMessage(profiles.error)}</p>
          )}
          {profiles.data?.length === 0 && (
            <p className="text-sm text-muted-foreground">No profiles yet</p>
          )}
          <ul className="divide-y">
            {profiles.data?.map((profile) => (
              <li key={profile.id} className="flex items-center gap-3 py-2 text-sm">
                <span className="font-medium">{profile.name}</span>
                <span className="text-muted-foreground">
                  {profile.config.format.toUpperCase()} · version {profile.version}
                </span>
                <Button
                  size="sm"
                  variant="outline"
                  className="ml-auto"
                  aria-label={`Edit ${profile.name}`}
                  onClick={() => setEditingId(profile.id)}
                >
                  Edit
                </Button>
              </li>
            ))}
          </ul>
          <Button variant="outline" onClick={() => setShowCatalog((shown) => !shown)}>
            Add from catalog
          </Button>
          {showCatalog && <Catalog />}
        </CardContent>
      </Card>
      {/* A new key resets the form's fields when switching between profiles. */}
      <ProfileForm
        key={editing?.id ?? "new"}
        editing={editing}
        onDone={() => setEditingId(undefined)}
      />
    </div>
  );
}
