"use client";

import { useEffect, useState } from "react";
import { api, type SpeakerProfile } from "@/lib/api";
import { confirmDialog } from "@/lib/confirm";
import { toast } from "sonner";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Loader2, Pencil, Trash2, Users, Check, X } from "lucide-react";

// Known Speakers UI in Settings.
//
// Lists every persistent SpeakerProfile created across all sessions.
// Each row offers rename, delete, and (when 2+ are checked) merge.
// The merge action exists for the unavoidable case where the user
// labeled the same person two different ways before fingerprint-matching
// kicked in — e.g. "John Smith" in their first session and "John S."
// in the second. Merging gives a single profile that aggregates both
// confirmation counts and centroid samples.
//
// Privacy: nothing here ever leaves the user's machine. The whole
// store is one JSON file in $USER_DATA/speaker_profiles.json.

export function KnownSpeakersSection() {
  const [profiles, setProfiles] = useState<SpeakerProfile[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [merging, setMerging] = useState(false);
  const [mergeName, setMergeName] = useState("");

  const load = async () => {
    try {
      const data = await api.listSpeakerProfiles();
      setProfiles(
        // Most-recently-updated first; mirrors how the user last
        // touched a profile.
        [...data].sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      );
    } catch (e) {
      toast.error(`Could not load known speakers: ${e instanceof Error ? e.message : e}`);
      setProfiles([]);
    }
  };

  useEffect(() => { load(); }, []);

  const toggle = (profile_id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(profile_id)) next.delete(profile_id);
      else next.add(profile_id);
      return next;
    });
  };

  const doDelete = async (profile_id: string, name: string) => {
    if (!(await confirmDialog(
      `Delete the speaker profile for "${name}"? Sessions where they appear keep their existing labels — only the cross-session fingerprint goes away.`,
      { title: "Delete speaker" }
    ))) {
      return;
    }
    try {
      await api.deleteSpeakerProfile(profile_id);
      toast.success(`Deleted "${name}"`);
      setSelected((prev) => {
        const next = new Set(prev);
        next.delete(profile_id);
        return next;
      });
      await load();
    } catch (e) {
      toast.error(`Delete failed: ${e instanceof Error ? e.message : e}`);
    }
  };

  const doMerge = async () => {
    const ids = Array.from(selected);
    if (ids.length < 2) return;
    const name = mergeName.trim();
    if (!name) {
      toast.error("Pick a name for the merged profile.");
      return;
    }
    setMerging(true);
    try {
      await api.mergeSpeakerProfiles(ids, name);
      toast.success(`Merged ${ids.length} profiles into "${name}"`);
      setSelected(new Set());
      setMergeName("");
      await load();
    } catch (e) {
      toast.error(`Merge failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setMerging(false);
    }
  };

  if (profiles === null) {
    return (
      <Card>
        <CardHeader><CardTitle className="text-base">Known Speakers</CardTitle></CardHeader>
        <CardContent><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Users className="h-4 w-4 text-primary" />
          Known Speakers
          <span className="text-xs font-normal text-muted-foreground">
            ({profiles.length})
          </span>
        </CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          Voice fingerprints saved across all your meetings. New sessions
          auto-tag known speakers when their voice matches an entry here.
          Profiles are local to this machine — nothing here ever leaves
          your device.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {profiles.length === 0 ? (
          <p className="text-sm text-muted-foreground text-center py-4">
            No saved speakers yet. Rename a speaker in any processed
            session and they&apos;ll appear here.
          </p>
        ) : (
          /* Scroll cap so the speaker list doesn't keep growing the
             Settings page as the user accumulates known speakers across
             meetings — the rest of the cards beneath would otherwise
             get pushed further and further off-screen. */
          <div className="max-h-[24rem] overflow-y-auto pr-1 space-y-2">
            {profiles.map((p) => (
              <ProfileRow
                key={p.profile_id}
                profile={p}
                selected={selected.has(p.profile_id)}
                onToggle={() => toggle(p.profile_id)}
                onDelete={() => doDelete(p.profile_id, p.display_name)}
                onRenamed={load}
              />
            ))}
          </div>
        )}

        {selected.size >= 2 && (
          <div className="rounded-lg border-2 border-primary bg-primary/5 p-3 space-y-2">
            <div className="text-sm font-medium">
              Merge {selected.size} profiles into one
            </div>
            <p className="text-xs text-muted-foreground">
              Used when the same person was labeled two different ways
              before fingerprint matching kicked in. The merged profile
              keeps all confirmations from both, and the centroid is a
              weighted mean by confirmation count.
            </p>
            <div className="flex items-center gap-2">
              <Input
                value={mergeName}
                onChange={(e) => setMergeName(e.target.value)}
                placeholder="Name for the merged profile"
                className="h-8"
                disabled={merging}
              />
              <Button size="sm" onClick={doMerge} disabled={merging || !mergeName.trim()}>
                {merging ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : null}
                Merge
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => { setSelected(new Set()); setMergeName(""); }}
                disabled={merging}
              >
                Cancel
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ProfileRow({
  profile, selected, onToggle, onDelete, onRenamed,
}: {
  profile: SpeakerProfile;
  selected: boolean;
  onToggle: () => void;
  onDelete: () => void;
  onRenamed: () => void | Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(profile.display_name);
  const [saving, setSaving] = useState(false);

  useEffect(() => { setValue(profile.display_name); }, [profile.display_name]);

  const save = async () => {
    const next = value.trim();
    if (!next || next === profile.display_name) {
      setEditing(false);
      setValue(profile.display_name);
      return;
    }
    setSaving(true);
    try {
      await api.renameSpeakerProfile(profile.profile_id, next);
      toast.success(`Renamed "${profile.display_name}" to "${next}"`);
      setEditing(false);
      await onRenamed();
    } catch (e) {
      toast.error(`Rename failed: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  // Unix epoch fallback to avoid Invalid Date bombs from a malformed
  // updated_at string in the persisted JSON.
  const updated = (() => {
    const d = new Date(profile.updated_at);
    return isNaN(d.getTime()) ? null : d;
  })();
  const updatedHint = updated
    ? `Updated ${updated.toLocaleDateString()}`
    : "";

  return (
    <div className="flex items-center gap-3 rounded-lg border p-3">
      <input
        type="checkbox"
        checked={selected}
        onChange={onToggle}
        className="h-4 w-4 shrink-0"
        aria-label={`Select ${profile.display_name}`}
      />
      <div className="flex h-9 w-9 items-center justify-center rounded-full bg-accent text-accent-foreground shrink-0">
        <Users className="h-4 w-4" />
      </div>
      <div className="flex-1 min-w-0">
        {editing ? (
          <div className="flex items-center gap-2">
            <Input
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") save();
                if (e.key === "Escape") { setEditing(false); setValue(profile.display_name); }
              }}
              disabled={saving}
              className="h-8"
            />
            <Button size="sm" onClick={save} disabled={saving} className="h-8">
              {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => { setEditing(false); setValue(profile.display_name); }}
              disabled={saving}
              className="h-8"
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          </div>
        ) : (
          <button
            onClick={() => setEditing(true)}
            className="group flex items-center gap-2 text-left w-full min-w-0"
          >
            <span className="text-sm font-medium truncate">{profile.display_name}</span>
            <Pencil className="h-3 w-3 text-muted-foreground opacity-0 group-hover:opacity-100 shrink-0" />
          </button>
        )}
        <div className="text-xs text-muted-foreground">
          {profile.session_count} session{profile.session_count === 1 ? "" : "s"}
          {profile.confirmation_count > 0
            ? ` · ${profile.confirmation_count} confirmation${profile.confirmation_count === 1 ? "" : "s"}`
            : null}
          {updatedHint ? ` · ${updatedHint}` : null}
        </div>
      </div>
      <Button
        size="sm"
        variant="ghost"
        onClick={onDelete}
        className="h-8 w-8 p-0"
        title="Delete this profile"
      >
        <Trash2 className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}
