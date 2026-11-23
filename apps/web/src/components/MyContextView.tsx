import { useEffect, useMemo, useState } from "react";
import {
  ArrowUpRight,
  Bell,
  GitCompare,
  Lock,
  Plus,
  Save,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { api, ApiError } from "../api/client";
import { formatReceiptShort } from "../utils/platform";
import type {
  AuthContextState,
  CompareMemoryResponseDTO,
  EntitySummaryDTO,
  MemoryDTO,
  TemporalSnapshot,
  TopicDTO,
  WatchlistResponseDTO,
} from "../types/api";

interface MyContextViewProps {
  auth: AuthContextState;
  snapshot: TemporalSnapshot;
  entities: EntitySummaryDTO[];
  topics: TopicDTO[];
  onSelectEntity: (idOrKey: string) => void;
  onMutated: () => void;
}

function extractWikilinks(content: string): string[] {
  const matches = content.match(/\[\[([^\]]+)\]\]/g) || [];
  return Array.from(
    new Set(matches.map((m) => m.slice(2, -2).trim()).filter(Boolean)),
  );
}

export function MyContextView({
  auth,
  snapshot,
  entities,
  topics,
  onSelectEntity,
  onMutated,
}: MyContextViewProps) {
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [disabledReason, setDisabledReason] = useState<string | null>(null);
  const [notes, setNotes] = useState<MemoryDTO[]>([]);
  const [watchlist, setWatchlist] = useState<WatchlistResponseDTO | null>(null);
  const [loading, setLoading] = useState(false);

  // Selected note & editor state
  const [scopeFilter, setScopeFilter] = useState<"all" | "user" | "user_x_skill">(
    "all",
  );
  const [activeNoteId, setActiveNoteId] = useState<string | "new" | null>(null);
  const [noteKey, setNoteKey] = useState("");
  const [noteScope, setNoteScope] = useState<"user" | "user_x_skill">("user");
  const [noteSkillId, setNoteSkillId] = useState("");
  const [noteContent, setNoteContent] = useState("");
  const [noteTags, setNoteTags] = useState("");
  const [saving, setSaving] = useState(false);

  // Compare & Promote state
  const [compareResult, setCompareResult] =
    useState<CompareMemoryResponseDTO | null>(null);
  const [comparing, setComparing] = useState(false);
  const [showPromoteForm, setShowPromoteForm] = useState(false);
  const [promoteSubject, setPromoteSubject] = useState("");
  const [promotePredicate, setPromotePredicate] = useState("analyst_note");
  const [statusBanner, setStatusBanner] = useState<{
    type: "ok" | "err";
    text: string;
  } | null>(null);

  // Watchlist quick-add picker
  const [watchEntityToAdd, setWatchEntityToAdd] = useState("");

  const loadData = async () => {
    setLoading(true);
    try {
      const [notesRes, wlRes] = await Promise.all([
        api.getMemoryNotes(auth),
        api.getWatchlist(auth),
      ]);
      setMemoryEnabled(notesRes.memory_enabled && wlRes.memory_enabled);
      setDisabledReason(
        notesRes.disabled_reason || wlRes.disabled_reason || null,
      );
      setNotes(notesRes.items || []);
      setWatchlist(wlRes);

      if (notesRes.items.length > 0 && activeNoteId === null) {
        selectNote(notesRes.items[0]);
      } else if (notesRes.items.length === 0) {
        setActiveNoteId("new");
      }
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 403) {
        setMemoryEnabled(false);
        setDisabledReason("Personal context disabled in shared channel");
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setActiveNoteId(null);
    setCompareResult(null);
    setStatusBanner(null);
    void loadData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [auth.userId, auth.channelMode, auth.actorMode]);

  const selectNote = (n: MemoryDTO) => {
    setActiveNoteId(n.memory_id);
    setNoteKey(n.key);
    setNoteScope(n.scope_type);
    setNoteSkillId(n.skill_id || "");
    setNoteContent(n.content);
    setNoteTags((n.tags || []).join(", "));
    setCompareResult(null);
    setShowPromoteForm(false);
    setStatusBanner(null);
  };

  const startNewNote = () => {
    setActiveNoteId("new");
    setNoteKey("");
    setNoteScope("user");
    setNoteSkillId("");
    setNoteContent("");
    setNoteTags("");
    setCompareResult(null);
    setShowPromoteForm(false);
    setStatusBanner(null);
  };

  const filteredNotes = useMemo(() => {
    if (scopeFilter === "all") return notes;
    return notes.filter((n) => n.scope_type === scopeFilter);
  }, [notes, scopeFilter]);

  const skills = useMemo(
    () => entities.filter((e) => e.entity_type === "skill"),
    [entities],
  );

  const entityMap = useMemo(
    () => new Map(entities.map((e) => [e.entity_id, e])),
    [entities],
  );

  const topicMap = useMemo(
    () => new Map(topics.map((t) => [t.topic_id, t])),
    [topics],
  );

  const wikilinks = useMemo(() => extractWikilinks(noteContent), [noteContent]);

  const handleSave = async () => {
    if (!noteKey.trim() || !noteContent.trim()) return;
    setSaving(true);
    setStatusBanner(null);
    const parsedTags = noteTags
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    const linkedEntityIds = wikilinks
      .map(
        (wl) =>
          entities.find(
            (e) =>
              e.canonical_key === wl ||
              e.entity_id === wl ||
              e.display_name.toLowerCase() === wl.toLowerCase(),
          )?.entity_id,
      )
      .filter((id): id is string => Boolean(id));

    try {
      if (activeNoteId && activeNoteId !== "new") {
        const res = await api.updateMemoryNote(auth, activeNoteId, {
          scope_type: noteScope,
          skill_id: noteScope === "user_x_skill" ? noteSkillId || null : null,
          key: noteKey.trim(),
          content: noteContent,
          tags: parsedTags,
          linked_entity_ids: linkedEntityIds,
        });
        setNotes((prev) =>
          prev.map((n) => (n.memory_id === activeNoteId ? res.memory : n)),
        );
        setStatusBanner({ type: "ok", text: "Note saved." });
      } else {
        const res = await api.createMemoryNote(auth, {
          scope_type: noteScope,
          skill_id: noteScope === "user_x_skill" ? noteSkillId || null : null,
          key: noteKey.trim(),
          content: noteContent,
          tags: parsedTags,
          linked_entity_ids: linkedEntityIds,
        });
        setNotes((prev) => [res.memory, ...prev]);
        selectNote(res.memory);
        setStatusBanner({ type: "ok", text: "Note created." });
      }
    } catch (err: unknown) {
      setStatusBanner({
        type: "err",
        text: err instanceof Error ? err.message : "Failed to save note.",
      });
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!activeNoteId || activeNoteId === "new") return;
    try {
      await api.deleteMemoryNote(auth, activeNoteId);
      const remaining = notes.filter((n) => n.memory_id !== activeNoteId);
      setNotes(remaining);
      if (remaining.length > 0) selectNote(remaining[0]);
      else startNewNote();
      setStatusBanner({ type: "ok", text: "Note deleted." });
    } catch (err: unknown) {
      setStatusBanner({
        type: "err",
        text: err instanceof Error ? err.message : "Delete failed.",
      });
    }
  };

  const handleCompare = async () => {
    if (!activeNoteId || activeNoteId === "new") return;
    setComparing(true);
    setStatusBanner(null);
    try {
      const res = await api.compareMemoryNote(auth, activeNoteId, snapshot.asOf);
      setCompareResult(res);
    } catch (err: unknown) {
      setStatusBanner({
        type: "err",
        text: err instanceof Error ? err.message : "Compare failed.",
      });
    } finally {
      setComparing(false);
    }
  };

  const handlePromote = async () => {
    if (!activeNoteId || activeNoteId === "new" || !promoteSubject) return;
    setStatusBanner(null);
    try {
      const res = await api.promoteMemoryNote(auth, activeNoteId, {
        subject_canonical_key: promoteSubject,
        predicate: promotePredicate || "analyst_note",
        rationale: "Promoted from personal memory for steward review",
      });
      setShowPromoteForm(false);
      setStatusBanner({
        type: "ok",
        text: `Promoted to quarantine (${res.epistemic_status}).`,
      });
      onMutated();
    } catch (err: unknown) {
      setStatusBanner({
        type: "err",
        text: err instanceof Error ? err.message : "Promotion failed.",
      });
    }
  };

  const handleAddWatchEntity = async () => {
    if (!watchlist || !watchEntityToAdd) return;
    if (watchlist.entity_ids.includes(watchEntityToAdd)) return;
    const updated = await api.updateWatchlist(auth, {
      entity_ids: [...watchlist.entity_ids, watchEntityToAdd],
      topic_ids: watchlist.topic_ids,
      source_ids: watchlist.source_ids,
    });
    setWatchlist(updated);
    setWatchEntityToAdd("");
  };

  const handleRemoveWatchEntity = async (entityId: string) => {
    if (!watchlist) return;
    const updated = await api.updateWatchlist(auth, {
      entity_ids: watchlist.entity_ids.filter((id) => id !== entityId),
      topic_ids: watchlist.topic_ids,
      source_ids: watchlist.source_ids,
    });
    setWatchlist(updated);
  };

  if (!memoryEnabled) {
    return (
      <div
        data-testid="memory-disabled-state"
        className="flex flex-1 items-center justify-center bg-[#0A0A0B] p-8"
      >
        <div className="flex items-center gap-2.5 rounded-lg border border-white/[0.08] bg-[#111113] px-4 py-3 text-sm text-[#A1A1AA]">
          <Lock className="h-4 w-4 text-[#FBBF24]" />
          <span>
            {disabledReason || "Personal context disabled in shared channel"}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div
      data-testid="my-context-view"
      className="flex min-h-0 min-w-0 flex-1 overflow-hidden bg-[#0A0A0B]"
    >
      {/* Left Pane: Watchlist Alerts + Personal Notes List */}
      <div className="flex w-80 shrink-0 flex-col border-r border-white/[0.08] bg-[#0A0A0B]">
        {/* Watchlist & Alerts */}
        <section
          aria-label="Watchlist and alerts"
          data-testid="watchlist-section"
          className="border-b border-white/[0.08] p-3"
        >
          <div className="mb-2 flex items-center justify-between">
            <span className="flex items-center gap-1.5 text-xs font-medium text-[#FAFAFA]">
              <Bell className="h-3.5 w-3.5 text-[#FBBF24]" />
              Watchlist & Alerts
            </span>
            <span className="font-mono text-[11px] tabular-nums text-[#94949E]">
              {watchlist?.alerts?.length ?? 0}
            </span>
          </div>

          {/* Live Enterprise Conflict / Change Alerts */}
          <div data-testid="watchlist-alerts" className="space-y-1.5">
            {watchlist?.alerts && watchlist.alerts.length > 0 ? (
              watchlist.alerts.slice(0, 4).map((alert) => (
                <button
                  key={alert.alert_id}
                  type="button"
                  onClick={() => {
                    const target =
                      alert.entity_canonical_key ||
                      alert.canonical_key ||
                      alert.target_id;
                    if (target) onSelectEntity(target);
                  }}
                  className="flex w-full items-start gap-2 rounded-md border border-white/[0.08] bg-[#111113] p-2 text-left text-xs hover:border-white/[0.18]"
                >
                  <span
                    className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                      alert.severity === "conflict"
                        ? "bg-[#F43F5E]"
                        : "bg-[#F59E0B]"
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium text-[#FAFAFA]">
                      {alert.title}
                    </div>
                    {alert.summary && (
                      <div className="mt-0.5 line-clamp-1 text-[11px] text-[#94949E]">
                        {alert.summary}
                      </div>
                    )}
                  </div>
                </button>
              ))
            ) : (
              <div className="rounded border border-white/[0.06] bg-[#111113] px-2.5 py-2 text-[11px] text-[#94949E]">
                No active watchlist alerts
              </div>
            )}
          </div>

          {/* Watched Entities & Topics chips */}
          {watchlist && (
            <div className="mt-2.5 space-y-1.5">
              <div className="flex flex-wrap gap-1">
                {watchlist.entity_ids.map((eid) => {
                  const ent = entityMap.get(eid);
                  return (
                    <span
                      key={eid}
                      className="inline-flex items-center gap-1 rounded border border-white/[0.08] bg-[#18181B] pl-2 pr-0.5 text-[11px] text-[#F4F4F5]"
                    >
                      <button
                        type="button"
                        onClick={() => onSelectEntity(ent?.canonical_key || eid)}
                        className="inline-flex min-h-6 max-w-[130px] items-center truncate hover:underline"
                      >
                        <span className="truncate">
                          {ent?.display_name || eid}
                        </span>
                      </button>
                      <button
                        type="button"
                        aria-label={`Remove ${ent?.display_name || eid}`}
                        onClick={() => void handleRemoveWatchEntity(eid)}
                        className="inline-flex min-h-6 min-w-6 items-center justify-center rounded text-[#94949E] hover:bg-white/[0.08] hover:text-[#FAFAFA]"
                      >
                        <X className="h-3 w-3" />
                      </button>
                    </span>
                  );
                })}
                {watchlist.topic_ids.map((tid) => {
                  const t = topicMap.get(tid);
                  return (
                    <span
                      key={tid}
                      className="rounded border border-white/[0.08] bg-[#111113] px-1.5 py-0.5 font-mono text-[10px] text-[#38BDF8]"
                    >
                      #{t?.slug || tid.replace(/^topic:/, "")}
                    </span>
                  );
                })}
              </div>

              <div className="flex gap-1">
                <select
                  aria-label="Add entity to watchlist"
                  value={watchEntityToAdd}
                  onChange={(e) => setWatchEntityToAdd(e.target.value)}
                  className="min-w-0 flex-1 rounded border border-white/[0.08] bg-[#111113] px-2 py-1 text-[11px] text-[#F4F4F5]"
                >
                  <option value="">+ Watch entity…</option>
                  {entities
                    .filter((e) => !watchlist.entity_ids.includes(e.entity_id))
                    .map((e) => (
                      <option key={e.entity_id} value={e.entity_id}>
                        {e.display_name}
                      </option>
                    ))}
                </select>
                <button
                  type="button"
                  disabled={!watchEntityToAdd}
                  onClick={() => void handleAddWatchEntity()}
                  className="rounded border border-white/[0.12] bg-[#18181B] px-2 py-1 text-[11px] font-medium text-[#FAFAFA] hover:bg-[#202024] disabled:opacity-40"
                >
                  Add
                </button>
              </div>
            </div>
          )}
        </section>

        {/* Notes list header */}
        <div className="flex items-center justify-between border-b border-white/[0.08] px-3 py-2">
          <div className="flex items-center gap-1">
            {(["all", "user", "user_x_skill"] as const).map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setScopeFilter(s)}
                className={`min-h-6 rounded px-2 py-0.5 text-[11px] font-medium ${
                  scopeFilter === s
                    ? "bg-[#18181B] text-[#FAFAFA]"
                    : "text-[#94949E] hover:text-[#FAFAFA]"
                }`}
              >
                {s === "all" ? "All" : s === "user" ? "User" : "Skill"}
              </button>
            ))}
          </div>
          <button
            type="button"
            data-testid="new-note-btn"
            onClick={startNewNote}
            className="flex min-h-6 items-center gap-1 rounded bg-[#FAFAFA] px-2.5 py-0.5 text-[11px] font-medium text-[#0A0A0B] hover:bg-[#E4E4E7]"
          >
            <Plus className="h-3 w-3" />
            New
          </button>
        </div>

        {/* Notes list */}
        <div
          data-testid="memory-notes-list"
          className="flex-1 divide-y divide-white/[0.06] overflow-y-auto"
        >
          {loading ? (
            <div className="p-3 text-xs text-[#94949E]">Loading…</div>
          ) : filteredNotes.length === 0 ? (
            <div className="p-4 text-center text-xs text-[#94949E]">
              No personal notes yet
            </div>
          ) : (
            filteredNotes.map((note) => {
              const selected = activeNoteId === note.memory_id;
              return (
                <button
                  key={note.memory_id}
                  type="button"
                  data-testid={`memory-note-item-${note.key}`}
                  onClick={() => selectNote(note)}
                  className={`block w-full px-3 py-2.5 text-left transition-colors ${
                    selected ? "bg-[#18181B]" : "hover:bg-[#111113]"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-xs font-medium text-[#FAFAFA]">
                      {note.key}
                    </span>
                    <span className="shrink-0 rounded border border-white/[0.08] bg-[#0A0A0B] px-1 py-0.5 font-mono text-[10px] text-[#94949E]">
                      {note.scope_type}
                    </span>
                  </div>
                  <div className="mt-1 line-clamp-2 text-[11px] text-[#A1A1AA]">
                    {note.content}
                  </div>
                </button>
              );
            })
          )}
        </div>
      </div>

      {/* Right Pane: Apple Notes-style Editor + Compare vs Firm + Promote */}
      <div
        data-testid="memory-editor"
        className="flex min-w-0 flex-1 flex-col overflow-hidden bg-[#0A0A0B]"
      >
        {/* Editor toolbar */}
        <div className="flex h-10 shrink-0 items-center justify-between gap-2 border-b border-white/[0.08] bg-[#111113] px-4">
          <div className="flex items-center gap-2">
            <select
              aria-label="Note scope"
              data-testid="note-scope-select"
              value={noteScope}
              onChange={(e) =>
                setNoteScope(e.target.value as "user" | "user_x_skill")
              }
              className="h-7 rounded border border-white/[0.08] bg-[#0A0A0B] px-2 font-mono text-xs text-[#FAFAFA]"
            >
              <option value="user">scope: user</option>
              <option value="user_x_skill">scope: user_x_skill</option>
            </select>

            {noteScope === "user_x_skill" && (
              <select
                aria-label="Target skill"
                value={noteSkillId}
                onChange={(e) => setNoteSkillId(e.target.value)}
                className="h-7 rounded border border-white/[0.08] bg-[#0A0A0B] px-2 font-mono text-xs text-[#FAFAFA]"
              >
                <option value="">Select skill…</option>
                {skills.map((s) => (
                  <option key={s.entity_id} value={s.canonical_key}>
                    {s.display_name}
                  </option>
                ))}
              </select>
            )}
          </div>

          <div className="flex items-center gap-1.5">
            {activeNoteId && activeNoteId !== "new" && (
              <>
                <button
                  type="button"
                  data-testid="compare-note-btn"
                  disabled={comparing}
                  onClick={() => void handleCompare()}
                  className="flex h-7 items-center gap-1 rounded border border-white/[0.08] bg-[#18181B] px-2.5 text-xs font-medium text-[#F4F4F5] hover:border-white/[0.2]"
                >
                  <GitCompare className="h-3.5 w-3.5 text-[#38BDF8]" />
                  <span>{comparing ? "Comparing…" : "Compare vs Firm"}</span>
                </button>
                <button
                  type="button"
                  data-testid="promote-note-btn"
                  onClick={() => {
                    setShowPromoteForm((v) => !v);
                    if (!promoteSubject && entities.length > 0) {
                      setPromoteSubject(entities[0].canonical_key);
                    }
                  }}
                  className="flex h-7 items-center gap-1 rounded border border-white/[0.08] bg-[#18181B] px-2.5 text-xs font-medium text-[#F4F4F5] hover:border-white/[0.2]"
                >
                  <Send className="h-3 w-3 text-[#FBBF24]" />
                  <span>Promote</span>
                </button>
                <button
                  type="button"
                  aria-label="Delete note"
                  onClick={() => void handleDelete()}
                  className="flex h-7 w-7 items-center justify-center rounded border border-white/[0.08] text-[#94949E] hover:border-[#F43F5E]/40 hover:text-[#FB7185]"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </>
            )}

            <button
              type="button"
              data-testid="save-note-btn"
              disabled={saving || !noteKey.trim() || !noteContent.trim()}
              onClick={() => void handleSave()}
              className="flex h-7 items-center gap-1 rounded bg-[#FAFAFA] px-3 text-xs font-medium text-[#0A0A0B] hover:bg-[#E4E4E7] disabled:opacity-40"
            >
              <Save className="h-3 w-3" />
              <span>{saving ? "Saving…" : "Save"}</span>
            </button>
          </div>
        </div>

        {/* Editor body */}
        <div className="flex-1 space-y-4 overflow-y-auto p-6">
          {statusBanner && (
            <div
              role="status"
              data-testid="memory-status-banner"
              className={`rounded-md border px-3 py-2 text-xs ${
                statusBanner.type === "ok"
                  ? "border-[#34D399]/30 bg-[#0A221A] text-[#34D399]"
                  : "border-[#F43F5E]/40 bg-[#2A1219] text-[#FB7185]"
              }`}
            >
              {statusBanner.text}
            </div>
          )}

          {showPromoteForm && (
            <div
              data-testid="promote-note-panel"
              className="flex flex-wrap items-center gap-2 rounded-lg border border-[#F59E0B]/30 bg-[#111113] p-3 text-xs"
            >
              <select
                aria-label="Target entity for promotion"
                value={promoteSubject}
                onChange={(e) => setPromoteSubject(e.target.value)}
                className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-xs text-[#FAFAFA]"
              >
                {entities.map((e) => (
                  <option key={e.entity_id} value={e.canonical_key}>
                    {e.display_name} ({e.canonical_key})
                  </option>
                ))}
              </select>
              <input
                type="text"
                value={promotePredicate}
                onChange={(e) => setPromotePredicate(e.target.value)}
                placeholder="predicate"
                aria-label="Claim predicate"
                className="w-36 rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-xs text-[#FAFAFA]"
              />
              <button
                type="button"
                data-testid="promote-confirm-btn"
                onClick={() => void handlePromote()}
                className="rounded bg-[#FAFAFA] px-3 py-1 font-medium text-[#0A0A0B]"
              >
                Send to Steward Inbox
              </button>
            </div>
          )}

          <div className="max-w-3xl space-y-3">
            <input
              type="text"
              value={noteKey}
              onChange={(e) => setNoteKey(e.target.value)}
              placeholder="note-key"
              aria-label="Note key"
              data-testid="note-key-input"
              className="w-full border-b border-white/[0.08] bg-transparent pb-2 font-mono text-lg font-medium text-[#FAFAFA] placeholder-[#94949E] focus:border-white/[0.25] focus:outline-none"
            />

            <input
              type="text"
              value={noteTags}
              onChange={(e) => setNoteTags(e.target.value)}
              placeholder="tags (comma separated)"
              aria-label="Note tags"
              className="w-full bg-transparent font-mono text-xs text-[#A1A1AA] placeholder-[#94949E] focus:outline-none"
            />

            <textarea
              rows={10}
              value={noteContent}
              onChange={(e) => setNoteContent(e.target.value)}
              placeholder="Write personal context… use [[veh:alpha-credit-offshore-lp]] to link entities"
              aria-label="Note content"
              data-testid="note-content-input"
              className="w-full resize-y rounded-lg border border-white/[0.08] bg-[#111113] p-4 text-sm leading-relaxed text-[#FAFAFA] placeholder-[#94949E] focus:border-white/[0.2] focus:outline-none"
            />

            {/* Parsed [[wikilinks]] chips */}
            {wikilinks.length > 0 && (
              <div className="flex flex-wrap items-center gap-1.5">
                {wikilinks.map((wl) => (
                  <button
                    key={wl}
                    type="button"
                    onClick={() => onSelectEntity(wl)}
                    className="inline-flex items-center gap-1 rounded border border-white/[0.1] bg-[#18181B] px-2 py-0.5 font-mono text-[11px] text-[#38BDF8] hover:border-[#38BDF8]/50"
                  >
                    <span>[[{wl}]]</span>
                    <ArrowUpRight className="h-3 w-3" />
                  </button>
                ))}
              </div>
            )}

            {/* Compare vs Firm diff card */}
            {compareResult && (
              <section
                data-testid="compare-result-card"
                aria-label="Compare against firm claims"
                className="rounded-lg border border-white/[0.1] bg-[#111113] p-4 text-xs"
              >
                <div className="flex items-center justify-between border-b border-white/[0.08] pb-2">
                  <span className="font-medium text-[#FAFAFA]">
                    Firm Comparison
                  </span>
                  <span className="font-mono text-[11px] text-[#94949E]">
                    {formatReceiptShort(compareResult.receipt_id)}
                  </span>
                </div>
                <p className="mt-2.5 leading-relaxed text-[#F4F4F5]">
                  {compareResult.divergence_summary}
                </p>

                {compareResult.matching_firm_claims.length > 0 && (
                  <div className="mt-3 space-y-1.5">
                    {compareResult.matching_firm_claims.map((c) => (
                      <div
                        key={c.claim_id}
                        className="flex items-center justify-between gap-2 rounded border border-white/[0.06] bg-[#0A0A0B] px-2.5 py-1.5 font-mono text-[11px]"
                      >
                        <span className="text-[#A1A1AA]">{c.predicate}</span>
                        <span className="text-[#FAFAFA]">{c.object_value}</span>
                        <span className="text-[#34D399]">
                          {c.epistemic_status}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
