"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type PortalBinding } from "@/lib/api";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Loader2 } from "lucide-react";

/**
 * Bind this project to a portal opportunity and sync its engagement
 * register. Paste-once: the customer id + edit token come from the
 * portal (GET /customers returns both to a signed-in SA); the token is
 * stored in the OS keychain server-side and never displayed again.
 *
 * A binding marked broken (the portal returned 403 — token revoked or
 * the opportunity re-tokenised) shows WHY and stops pushing until
 * re-bound; automatically retrying a dead token is silent failure, so
 * the state is surfaced instead.
 */
export function PortalSyncControls({ client, project, mode = "manage" }: {
  client: string;
  project: string;
  // "manage" (Clients tab) shows bind/re-bind/unbind; "sync-only"
  // (Engagements) shows just the push action for an existing binding —
  // configuration lives with the client setup, not the register view.
  mode?: "manage" | "sync-only";
}) {
  const [binding, setBinding] = useState<PortalBinding | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // The portal's connection block, pasted verbatim. Primary bind path.
  const [connection, setConnection] = useState("");
  const [manual, setManual] = useState(false);
  const [customerId, setCustomerId] = useState("");
  const [oppName, setOppName] = useState("");
  const [parentName, setParentName] = useState("");
  const [token, setToken] = useState("");

  // Light client-side peek at the paste, purely to enable the button
  // and preview the opportunity — the backend owns real validation.
  const pasted = (() => {
    const t = connection.trim().replace(/^```[a-zA-Z]*\s*|\s*```$/g, "");
    if (!t) return null;
    try {
      const b = JSON.parse(t) as Record<string, unknown>;
      return b && typeof b === "object" && b.api && b.customerId && b.editToken
        ? { opportunity: String(b.opportunity ?? "") } : null;
    } catch { return null; }
  })();

  const reload = useCallback(async () => {
    try {
      const all = await api.portalBindings();
      const hit = Object.values(all).find(
        (b) => b.client === client.toLowerCase()
          && b.project === project.toLowerCase());
      setBinding(hit ?? null);
    } catch {
      setBinding(null);
    }
  }, [client, project]);

  useEffect(() => { void reload(); }, [reload]);

  const bind = async () => {
    setBusy(true);
    try {
      await api.portalBind(
        !manual && connection.trim()
          ? { client, project, connection: connection.trim() }
          : {
              client, project, customer_id: customerId.trim(),
              opportunity_name: oppName.trim(), parent_name: parentName.trim(),
              edit_token: token.trim(),
            });
      // Cleared the moment the bind lands — the token (inside the
      // block or the field) now lives in the OS keychain and nowhere
      // in this page.
      setConnection("");
      setToken("");
      setOpen(false);
      toast.success("Project bound to the portal");
      await reload();
    } catch (e) {
      toast.error(`Bind failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  const sync = async () => {
    setBusy(true);
    try {
      const r = await api.portalSync({ client, project });
      toast.success(
        `Portal sync: ${r.added ?? 0} added, ${r.updated ?? 0} updated `
        + `(${r.items ?? "?"} items total)`);
      await reload();
    } catch (e) {
      toast.error(`${e instanceof Error ? e.message : e}`);
      await reload();  // a 403 marks the binding broken — show it
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex items-center gap-2">
      {binding && !binding.broken && (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void sync()}>
          {busy ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : null}
          Sync to portal
        </Button>
      )}
      {binding?.broken && (
        <span
          className="text-xs text-red-600 dark:text-red-400 max-w-[16rem] truncate"
          title={binding.broken_reason || "The portal rejected this project's edit token."}
        >
          Portal binding broken — re-bind
        </span>
      )}
      {mode === "manage" && (
        <Button size="sm" variant="ghost" onClick={() => setOpen((v) => !v)}>
          {binding ? "Portal…" : "Bind to portal…"}
        </Button>
      )}
      {mode === "manage" && open && (
        <div className="absolute z-20 mt-2 top-full right-0 w-80 rounded-lg border border-border bg-popover p-3 space-y-2 shadow-lg">
          <div className="text-xs text-muted-foreground">
            {binding
              ? `Bound to ${binding.opportunityName || binding.customerId}`
                + (binding.parentName ? ` (${binding.parentName})` : "")
              : "Paste the connection block from the portal — the whole JSON, exactly as shown. The edit token inside it goes to the OS keychain and is never shown again."}
          </div>
          {!manual && (
            <>
              <textarea
                className="w-full h-28 rounded-md border border-input bg-transparent px-3 py-2 text-xs font-mono shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                placeholder={'{\n  "portal": "https://…",\n  "api": "https://…",\n  "opportunity": "…",\n  "customerId": "…",\n  "editToken": "…"\n}'}
                value={connection}
                onChange={(e) => setConnection(e.target.value)}
              />
              {connection.trim() && (
                <div className={"text-xs " + (pasted ? "text-muted-foreground" : "text-red-600 dark:text-red-400")}>
                  {pasted
                    ? `Ready to bind${pasted.opportunity ? `: ${pasted.opportunity}` : ""}`
                    : "That doesn't look like the portal's connection block yet."}
                </div>
              )}
            </>
          )}
          {manual && (
            <>
              <Input placeholder="Customer ID" value={customerId}
                     onChange={(e) => setCustomerId(e.target.value)} />
              <Input placeholder="Opportunity name (optional)" value={oppName}
                     onChange={(e) => setOppName(e.target.value)} />
              <Input placeholder="Parent company (optional)" value={parentName}
                     onChange={(e) => setParentName(e.target.value)} />
              <Input placeholder="Edit token" type="password" value={token}
                     onChange={(e) => setToken(e.target.value)} />
            </>
          )}
          <div className="flex gap-2 items-center">
            <button
              type="button"
              className="text-xs text-muted-foreground underline underline-offset-2"
              onClick={() => setManual((v) => !v)}
            >
              {manual ? "Paste connection block instead" : "Enter fields manually"}
            </button>
            <div className="flex-1" />
            {binding && (
              <Button size="sm" variant="ghost" disabled={busy}
                onClick={async () => {
                  await api.portalUnbind({ client, project });
                  setOpen(false);
                  await reload();
                }}>
                Unbind
              </Button>
            )}
            <Button size="sm"
                    disabled={busy || (manual
                      ? !customerId.trim() || !token.trim()
                      : !pasted)}
                    onClick={() => void bind()}>
              {binding ? "Re-bind" : "Bind"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
