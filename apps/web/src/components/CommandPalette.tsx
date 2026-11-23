import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  Compass,
  FileText,
  Hash,
  Search,
  Sparkles,
  X,
} from "lucide-react";
import { api } from "../api/client";
import type {
  AuthContextState,
  SearchHitDTO,
  TemporalSnapshot,
  ViewKey,
} from "../types/api";
import { formatEntityType } from "../utils/platform";

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  auth: AuthContextState;
  snapshot: TemporalSnapshot;
  onSelectEntity: (idOrKey: string) => void;
  onSelectTopic: (slug: string) => void;
  onNavigateView: (view: ViewKey) => void;
  onOpenUpload: () => void;
  onToggleAgent: () => void;
}

interface PaletteItem {
  id: string;
  kind: "view" | "action" | "hit";
  label: string;
  subtitle?: string;
  badge?: string;
  hasConflict?: boolean;
  hit?: SearchHitDTO;
  run: () => void;
}

export function CommandPalette({
  open,
  onClose,
  auth,
  snapshot,
  onSelectEntity,
  onSelectTopic,
  onNavigateView,
  onOpenUpload,
  onToggleAgent,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHitDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    setQuery("");
    setHits([]);
    setActiveIndex(0);
    const id = window.setTimeout(() => inputRef.current?.focus(), 10);
    return () => window.clearTimeout(id);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const trimmed = query.trim();
    if (!trimmed) {
      setHits([]);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    const timer = window.setTimeout(async () => {
      try {
        const res = await api.search(auth, {
          q: trimmed,
          as_of: snapshot.asOf,
          recorded_as_of: snapshot.recordedAsOf,
          limit: 20,
        });
        if (!cancelled) {
          setHits(res.hits || []);
          setActiveIndex(0);
        }
      } catch {
        if (!cancelled) setHits([]);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }, 110);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [open, query, auth, snapshot]);

  const quickCommands = useMemo<PaletteItem[]>(() => {
    const base: PaletteItem[] = [
      {
        id: "cmd-home",
        kind: "view",
        label: "Home",
        subtitle: "Stream",
        badge: "View",
        run: () => {
          onNavigateView("home");
          onClose();
        },
      },
      {
        id: "cmd-table",
        kind: "view",
        label: "Table",
        subtitle: "Dense Inspector",
        badge: "View",
        run: () => {
          onNavigateView("table");
          onClose();
        },
      },
      {
        id: "cmd-graph",
        kind: "view",
        label: "Graph",
        subtitle: "Diagnostic Topology",
        badge: "View",
        run: () => {
          onNavigateView("graph");
          onClose();
        },
      },
      {
        id: "cmd-memory",
        kind: "view",
        label: "My Context",
        subtitle: "Personal Memory & Watchlist",
        badge: "View",
        run: () => {
          onNavigateView("memory");
          onClose();
        },
      },
      {
        id: "cmd-upload",
        kind: "action",
        label: "Manual Upload & Triage",
        subtitle: "Quarantined Inbox",
        badge: "Action",
        run: () => {
          onOpenUpload();
          onClose();
        },
      },
      {
        id: "cmd-agent",
        kind: "action",
        label: "Viewport Agent",
        subtitle: "Grounded Q&A",
        badge: "Action",
        run: () => {
          onToggleAgent();
          onClose();
        },
      },
    ];

    const q = query.trim().toLowerCase();
    if (!q) return base;
    return base.filter(
      (c) =>
        c.label.toLowerCase().includes(q) ||
        (c.subtitle && c.subtitle.toLowerCase().includes(q)),
    );
  }, [query, onNavigateView, onOpenUpload, onToggleAgent, onClose]);

  const items = useMemo<PaletteItem[]>(() => {
    const hitItems: PaletteItem[] = hits.map((hit) => ({
      id: `hit-${hit.kind}-${hit.id}`,
      kind: "hit",
      label: hit.title,
      subtitle: hit.canonical_key || hit.subtitle,
      badge: hit.entity_type ? formatEntityType(hit.entity_type) : hit.kind,
      hasConflict: hit.has_conflict,
      hit,
      run: () => {
        if (hit.kind === "topic") {
          onSelectTopic(hit.canonical_key || hit.id.replace(/^topic:/, ""));
        } else if (hit.kind === "memory") {
          onNavigateView("memory");
        } else {
          onSelectEntity(hit.canonical_key || hit.id);
        }
        onClose();
      },
    }));
    return [...hitItems, ...quickCommands];
  }, [hits, quickCommands, onSelectEntity, onSelectTopic, onNavigateView, onClose]);

  useEffect(() => {
    if (activeIndex >= items.length) {
      setActiveIndex(Math.max(0, items.length - 1));
    }
  }, [items.length, activeIndex]);

  if (!open) return null;

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((prev) => (items.length === 0 ? 0 : (prev + 1) % items.length));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((prev) =>
        items.length === 0 ? 0 : (prev - 1 + items.length) % items.length,
      );
      return;
    }
    if (e.key === "Enter" && items[activeIndex]) {
      e.preventDefault();
      items[activeIndex].run();
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/65 px-4 pt-[12vh]"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search Context Lake"
        data-testid="cmdk-dialog"
        onKeyDown={handleKeyDown}
        className="w-full max-w-xl overflow-hidden rounded-xl border border-white/[0.1] bg-[#111113] shadow-2xl"
      >
        <div className="flex items-center gap-2.5 border-b border-white/[0.08] px-3.5 py-2.5">
          <Search className="h-4 w-4 shrink-0 text-[#94949E]" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search entities, aliases, claims, topics, skills…"
            aria-label="Search query"
            data-testid="cmdk-input"
            className="w-full bg-transparent text-sm text-[#FAFAFA] placeholder-[#94949E] focus:outline-none"
          />
          {loading && (
            <span className="shrink-0 font-mono text-[11px] text-[#94949E]">
              …
            </span>
          )}
          <button
            type="button"
            aria-label="Close search"
            onClick={onClose}
            className="rounded p-1 text-[#94949E] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div
          role="listbox"
          aria-label="Search results"
          data-testid="cmdk-results"
          className="max-h-[56vh] overflow-y-auto p-1.5"
        >
          {items.length === 0 ? (
            <div className="px-3 py-8 text-center text-xs text-[#94949E]">
              No matching authorized records
            </div>
          ) : (
            items.map((item, idx) => {
              const selected = idx === activeIndex;
              return (
                <div
                  key={item.id}
                  role="option"
                  tabIndex={-1}
                  aria-selected={selected}
                  data-testid={`cmdk-item-${idx}`}
                  onMouseEnter={() => setActiveIndex(idx)}
                  onClick={item.run}
                  className={`flex w-full cursor-pointer items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-[13px] transition-colors ${
                    selected
                      ? "bg-[#18181B] text-[#FAFAFA]"
                      : "text-[#F4F4F5] hover:bg-white/[0.04]"
                  }`}
                >
                  <span className="flex min-w-0 items-center gap-2.5">
                    {item.kind === "view" ? (
                      <Compass className="h-3.5 w-3.5 shrink-0 text-[#94949E]" />
                    ) : item.kind === "action" ? (
                      <Sparkles className="h-3.5 w-3.5 shrink-0 text-[#94949E]" />
                    ) : item.hit?.kind === "topic" ? (
                      <Hash className="h-3.5 w-3.5 shrink-0 text-[#38BDF8]" />
                    ) : item.hasConflict ? (
                      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-[#F43F5E]" />
                    ) : (
                      <FileText className="h-3.5 w-3.5 shrink-0 text-[#94949E]" />
                    )}
                    <span className="truncate font-medium">{item.label}</span>
                    {item.subtitle && (
                      <span className="truncate font-mono text-[11px] text-[#94949E]">
                        {item.subtitle}
                      </span>
                    )}
                  </span>

                  <span className="flex shrink-0 items-center gap-1.5">
                    {item.hasConflict && (
                      <span className="rounded bg-[#2A1219] px-1.5 py-0.5 text-[10px] font-medium text-[#FB7185]">
                        conflict
                      </span>
                    )}
                    {item.badge && (
                      <span className="rounded border border-white/[0.08] bg-[#0A0A0B] px-1.5 py-0.5 font-mono text-[10px] text-[#A1A1AA]">
                        {item.badge}
                      </span>
                    )}
                    {selected && (
                      <ArrowRight className="h-3 w-3 text-[#A1A1AA]" />
                    )}
                  </span>
                </div>
              );
            })
          )}
        </div>

        <div className="flex items-center justify-between border-t border-white/[0.08] bg-[#0A0A0B] px-3.5 py-1.5 font-mono text-[11px] text-[#94949E]">
          <span>↑↓ Navigate · ↵ Open · Esc Close</span>
          <span className="tabular-nums">{hits.length} hits</span>
        </div>
      </div>
    </div>
  );
}
