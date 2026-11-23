import { useMemo, useState } from "react";
import { AlertTriangle, Bell, ChevronRight, Lock } from "lucide-react";
import type {
  DomainNature,
  StreamItemDTO,
  TopicDTO,
  WatchlistAlertDTO,
} from "../types/api";
import { formatEntityType, pluralize } from "../utils/platform";
import { TopicSidebar } from "./TopicSidebar";

interface HomeViewProps {
  items: StreamItemDTO[];
  counts: { entities: number; open_conflicts: number; observations: number };
  loading: boolean;
  topics: TopicDTO[];
  topicTree: TopicDTO[];
  selectedTopicSlug: string | null;
  onSelectTopic: (slug: string | null) => void;
  onCreateTopic: (payload: {
    slug: string;
    pref_label: string;
    broader_topic_id?: string | null;
  }) => Promise<void>;
  onArchiveTopic: (topicId: string) => Promise<void>;
  selectedEntityId: string | null;
  onSelectEntity: (idOrKey: string) => void;
  memoryEnabled: boolean;
  watchlistAlerts: WatchlistAlertDTO[];
  onOpenMemory: () => void;
}

type StatusFilter = "all" | "conflicts" | "approved" | "unverified";

export function HomeView({
  items,
  counts,
  loading,
  topics,
  topicTree,
  selectedTopicSlug,
  onSelectTopic,
  onCreateTopic,
  onArchiveTopic,
  selectedEntityId,
  onSelectEntity,
  memoryEnabled,
  watchlistAlerts,
  onOpenMemory,
}: HomeViewProps) {
  const [domainFilter, setDomainFilter] = useState<"all" | DomainNature>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");

  const filteredItems = useMemo(() => {
    return items.filter((item) => {
      if (domainFilter !== "all" && item.domain_nature && item.domain_nature !== domainFilter) {
        return false;
      }
      if (statusFilter === "conflicts") {
        return Boolean(item.has_conflict || (item.open_conflict_count ?? 0) > 0);
      }
      if (statusFilter === "approved") {
        return !item.has_conflict && item.epistemic_badge !== "unverified_observation";
      }
      if (statusFilter === "unverified") {
        return item.epistemic_badge === "unverified_observation";
      }
      return true;
    });
  }, [items, domainFilter, statusFilter]);

  return (
    <div data-testid="home-view" className="flex min-h-0 flex-1 overflow-hidden">
      <TopicSidebar
        topics={topics}
        tree={topicTree}
        selectedTopicSlug={selectedTopicSlug}
        onSelectTopic={onSelectTopic}
        onCreateTopic={onCreateTopic}
        onArchiveTopic={onArchiveTopic}
      />

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {/* Compact single-row filter bar */}
        <div className="flex h-10 shrink-0 items-center justify-between gap-3 border-b border-white/[0.08] bg-[#111113] px-4">
          <div className="flex items-center gap-2 overflow-x-auto">
            {/* Domain Nature segmented pills */}
            <div
              role="group"
              aria-label="Domain nature"
              className="flex items-center rounded-md border border-white/[0.08] bg-[#0A0A0B] p-0.5"
            >
              {(["all", "financial", "non_financial"] as const).map((val) => {
                const active = domainFilter === val;
                const text =
                  val === "all"
                    ? "All"
                    : val === "financial"
                      ? "Financial"
                      : "Non-Financial";
                return (
                  <button
                    key={val}
                    type="button"
                    data-testid={`home-domain-${val}`}
                    onClick={() => setDomainFilter(val)}
                    className={`min-h-6 rounded px-2 py-0.5 text-xs font-medium transition-colors ${
                      active
                        ? "bg-[#18181B] text-[#FAFAFA]"
                        : "text-[#A1A1AA] hover:text-[#FAFAFA]"
                    }`}
                  >
                    {text}
                  </button>
                );
              })}
            </div>

            {/* Epistemic / conflict status pills */}
            <div
              role="group"
              aria-label="Epistemic status"
              className="flex items-center gap-1"
            >
              {(
                [
                  { id: "all", label: "All" },
                  { id: "conflicts", label: "Conflicts" },
                  { id: "approved", label: "Approved" },
                  { id: "unverified", label: "Quarantined" },
                ] as const
              ).map((pill) => {
                const active = statusFilter === pill.id;
                return (
                  <button
                    key={pill.id}
                    type="button"
                    data-testid={`home-status-${pill.id}`}
                    onClick={() => setStatusFilter(pill.id)}
                    className={`min-h-6 rounded-md border px-2 py-0.5 text-xs font-medium transition-colors ${
                      active
                        ? "border-white/[0.2] bg-[#18181B] text-[#FAFAFA]"
                        : "border-transparent text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
                    }`}
                  >
                    {pill.label}
                  </button>
                );
              })}
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-3 font-mono text-xs tabular-nums text-[#A1A1AA]">
            <span>{pluralize(counts.entities, "entity", "entities")}</span>
            {counts.open_conflicts > 0 && (
              <span className="flex items-center gap-1 text-[#FB7185]">
                <span className="h-1.5 w-1.5 rounded-full bg-[#F43F5E]" />
                {pluralize(counts.open_conflicts, "conflict")}
              </span>
            )}
          </div>
        </div>

        {/* Scannable stream */}
        <div className="flex-1 overflow-y-auto">
          {memoryEnabled && watchlistAlerts.length > 0 && (
            <section
              aria-label="Watchlist alerts"
              data-testid="home-watchlist-strip"
              className="border-b border-white/[0.08] bg-[#111113]/60 px-4 py-2"
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex min-w-0 items-center gap-2 overflow-x-auto">
                  <Bell className="h-3.5 w-3.5 shrink-0 text-[#FBBF24]" />
                  {watchlistAlerts.slice(0, 3).map((alert) => (
                    <button
                      key={alert.alert_id}
                      type="button"
                      onClick={() => {
                        const target =
                          alert.entity_canonical_key ||
                          alert.canonical_key ||
                          alert.target_id;
                        if (target) onSelectEntity(target);
                        else onOpenMemory();
                      }}
                      className="flex shrink-0 items-center gap-1.5 rounded border border-white/[0.08] bg-[#18181B] px-2 py-0.5 text-xs text-[#F4F4F5] hover:border-white/[0.18]"
                    >
                      <span
                        className={`h-1.5 w-1.5 rounded-full ${
                          alert.severity === "conflict"
                            ? "bg-[#F43F5E]"
                            : "bg-[#F59E0B]"
                        }`}
                      />
                      <span className="max-w-[260px] truncate">{alert.title}</span>
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={onOpenMemory}
                  className="shrink-0 text-xs font-medium text-[#A1A1AA] hover:text-[#FAFAFA]"
                >
                  My Context
                </button>
              </div>
            </section>
          )}

          {loading ? (
            <div className="divide-y divide-white/[0.06]">
              {Array.from({ length: 8 }).map((_, i) => (
                <div
                  key={i}
                  className="flex h-12 items-center justify-between px-4"
                >
                  <div className="h-3 w-56 animate-pulse rounded bg-[#18181B]" />
                  <div className="h-3 w-24 animate-pulse rounded bg-[#18181B]" />
                </div>
              ))}
            </div>
          ) : filteredItems.length === 0 ? (
            <div className="px-6 py-12 text-center text-sm text-[#94949E]">
              No matching authorized items
            </div>
          ) : (
            <ul
              data-testid="stream-list"
              className="divide-y divide-white/[0.06]"
            >
              {filteredItems.map((item) => {
                const isSelected =
                  selectedEntityId === item.entity_id ||
                  selectedEntityId === item.canonical_key;
                const hasConflict = Boolean(
                  item.has_conflict || (item.open_conflict_count ?? 0) > 0,
                );
                const isQuarantined =
                  item.epistemic_badge === "unverified_observation";
                const dotColor = hasConflict
                  ? "bg-[#F43F5E]"
                  : isQuarantined
                    ? "bg-[#F59E0B]"
                    : "bg-[#34D399]";

                return (
                  <li key={item.entity_id}>
                    <button
                      type="button"
                      data-testid={`stream-row-${item.canonical_key}`}
                      data-entity-id={item.entity_id}
                      onClick={() => onSelectEntity(item.canonical_key || item.entity_id)}
                      className={`flex w-full items-center justify-between gap-4 px-4 py-2.5 text-left transition-colors ${
                        isSelected
                          ? "bg-[#18181B]"
                          : "hover:bg-[#111113]"
                      }`}
                    >
                      <div className="flex min-w-0 items-center gap-3">
                        <span
                          className={`h-2 w-2 shrink-0 rounded-full ${dotColor}`}
                          aria-hidden="true"
                        />
                        <div className="min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="truncate text-[13px] font-medium text-[#FAFAFA]">
                              {item.display_name}
                            </span>
                            <span className="shrink-0 font-mono text-[11px] text-[#94949E]">
                              {item.canonical_key}
                            </span>
                          </div>
                          {item.topics && item.topics.length > 0 && (
                            <div className="mt-0.5 flex items-center gap-1.5">
                              {item.topics.slice(0, 3).map((slug) => (
                                <span
                                  key={slug}
                                  className="font-mono text-[11px] text-[#94949E]"
                                >
                                  #{slug}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>

                      <div className="flex shrink-0 items-center gap-3">
                        {item.wall_side === "private_side" && (
                          <span className="flex items-center gap-1 rounded border border-[#F59E0B]/30 bg-[#18181B] px-1.5 py-0.5 font-mono text-[10px] text-[#FBBF24]">
                            <Lock className="h-2.5 w-2.5" />
                            MNPI
                          </span>
                        )}
                        <span className="rounded border border-white/[0.08] bg-[#111113] px-1.5 py-0.5 font-mono text-[11px] text-[#A1A1AA]">
                          {formatEntityType(item.entity_type)}
                        </span>
                        <span className="font-mono text-xs tabular-nums text-[#94949E]">
                          {pluralize(item.edge_count ?? 0, "edge")}
                          {(item.skill_count ?? 0) > 0
                            ? ` · ${pluralize(item.skill_count ?? 0, "skill")}`
                            : ""}
                        </span>
                        {hasConflict && (
                          <span className="flex items-center gap-1 rounded bg-[#2A1219] px-1.5 py-0.5 font-mono text-[11px] tabular-nums text-[#FB7185]">
                            <AlertTriangle className="h-3 w-3" />
                            {item.open_conflict_count || 1}
                          </span>
                        )}
                        <ChevronRight className="h-3.5 w-3.5 text-[#94949E]" />
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
