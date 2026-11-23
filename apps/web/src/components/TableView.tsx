import { useMemo, useState } from "react";
import { AlertTriangle, ArrowDown, ArrowUp, Lock, Search } from "lucide-react";
import type {
  DomainNature,
  EntitySummaryDTO,
  EpistemicTemporal,
  IngestionChannel,
  Modality,
  TopicDTO,
} from "../types/api";
import { formatEntityType, formatWallSideLabel } from "../utils/platform";

export interface TableFilterState {
  domain_nature?: DomainNature;
  modality?: Modality;
  epistemic_temporal?: EpistemicTemporal;
  ingestion_channel?: IngestionChannel;
  topic?: string;
  conflicts_only: boolean;
  q: string;
}

interface TableViewProps {
  entities: EntitySummaryDTO[];
  total: number;
  loading: boolean;
  topics: TopicDTO[];
  filters: TableFilterState;
  onChangeFilters: (next: TableFilterState) => void;
  selectedEntityId: string | null;
  onSelectEntity: (idOrKey: string) => void;
}

type SortKey = "display_name" | "entity_type" | "edge_count" | "open_conflict_count" | "wall_side";

export function TableView({
  entities,
  total,
  loading,
  topics,
  filters,
  onChangeFilters,
  selectedEntityId,
  onSelectEntity,
}: TableViewProps) {
  const [sortKey, setSortKey] = useState<SortKey>("open_conflict_count");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "display_name" || key === "entity_type" ? "asc" : "desc");
    }
  };

  const rows = useMemo(() => {
    const list = filters.conflicts_only
      ? entities.filter(
          (e) => Boolean(e.has_conflict || (e.open_conflict_count ?? 0) > 0),
        )
      : entities;

    return [...list].sort((a, b) => {
      let cmp = 0;
      switch (sortKey) {
        case "display_name":
          cmp = a.display_name.localeCompare(b.display_name);
          break;
        case "entity_type":
          cmp = a.entity_type.localeCompare(b.entity_type);
          break;
        case "edge_count":
          cmp =
            (a.edge_count ?? (a.degree_in ?? 0) + (a.degree_out ?? 0)) -
            (b.edge_count ?? (b.degree_in ?? 0) + (b.degree_out ?? 0));
          break;
        case "open_conflict_count":
          cmp = (a.open_conflict_count ?? 0) - (b.open_conflict_count ?? 0);
          if (cmp === 0) cmp = a.display_name.localeCompare(b.display_name);
          break;
        case "wall_side":
          cmp = a.wall_side.localeCompare(b.wall_side);
          break;
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [entities, filters.conflicts_only, sortKey, sortDir]);

  const SortIndicator = ({ col }: { col: SortKey }) => {
    if (sortKey !== col) return null;
    return sortDir === "asc" ? (
      <ArrowUp className="h-3 w-3 text-[#FAFAFA]" />
    ) : (
      <ArrowDown className="h-3 w-3 text-[#FAFAFA]" />
    );
  };

  return (
    <div
      data-testid="table-view"
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
    >
      {/* Compact orthogonal facet filter strip */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-white/[0.08] bg-[#111113] px-4 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-[#94949E]" />
            <input
              type="text"
              value={filters.q}
              onChange={(e) =>
                onChangeFilters({ ...filters, q: e.target.value })
              }
              placeholder="Filter entities…"
              aria-label="Filter table entities"
              data-testid="table-filter-input"
              className="h-7 w-44 rounded-md border border-white/[0.08] bg-[#0A0A0B] pl-7 pr-2 text-xs text-[#FAFAFA] placeholder-[#94949E] focus:border-white/[0.2] focus:outline-none"
            />
          </div>

          <select
            aria-label="Domain nature"
            data-testid="table-facet-domain"
            value={filters.domain_nature || ""}
            onChange={(e) => {
              const val = (e.target.value || undefined) as
                | DomainNature
                | undefined;
              onChangeFilters({
                ...filters,
                domain_nature: val,
                epistemic_temporal:
                  val === "financial" ? filters.epistemic_temporal : undefined,
              });
            }}
            className="h-7 rounded-md border border-white/[0.08] bg-[#0A0A0B] px-2 text-xs text-[#F4F4F5]"
          >
            <option value="">Domain: All</option>
            <option value="financial">Financial</option>
            <option value="non_financial">Non-Financial</option>
          </select>

          {filters.domain_nature === "financial" && (
            <select
              aria-label="Epistemic temporal"
              data-testid="table-facet-temporal"
              value={filters.epistemic_temporal || ""}
              onChange={(e) =>
                onChangeFilters({
                  ...filters,
                  epistemic_temporal: (e.target.value || undefined) as
                    | EpistemicTemporal
                    | undefined,
                })
              }
              className="h-7 rounded-md border border-white/[0.12] bg-[#18181B] px-2 text-xs text-[#FAFAFA]"
            >
              <option value="">Horizon: All</option>
              <option value="actual">Actual</option>
              <option value="estimate">Estimate</option>
              <option value="forecast">Forecast</option>
              <option value="guidance">Guidance</option>
              <option value="pro_forma">Pro Forma</option>
            </select>
          )}

          <select
            aria-label="Modality"
            data-testid="table-facet-modality"
            value={filters.modality || ""}
            onChange={(e) =>
              onChangeFilters({
                ...filters,
                modality: (e.target.value || undefined) as Modality | undefined,
              })
            }
            className="h-7 rounded-md border border-white/[0.08] bg-[#0A0A0B] px-2 text-xs text-[#F4F4F5]"
          >
            <option value="">Modality: All</option>
            <option value="quantitative">Quantitative</option>
            <option value="qualitative">Qualitative</option>
          </select>

          <select
            aria-label="Ingestion channel"
            data-testid="table-facet-ingestion"
            value={filters.ingestion_channel || ""}
            onChange={(e) =>
              onChangeFilters({
                ...filters,
                ingestion_channel: (e.target.value || undefined) as
                  | IngestionChannel
                  | undefined,
              })
            }
            className="h-7 rounded-md border border-white/[0.08] bg-[#0A0A0B] px-2 text-xs text-[#F4F4F5]"
          >
            <option value="">Channel: All</option>
            <option value="automated_feed">Automated Feed</option>
            <option value="manual_upload">Manual Upload</option>
            <option value="human_authored">Human Authored</option>
            <option value="agent_extracted">Agent Extracted</option>
          </select>

          <select
            aria-label="Topic"
            data-testid="table-facet-topic"
            value={filters.topic || ""}
            onChange={(e) =>
              onChangeFilters({
                ...filters,
                topic: e.target.value || undefined,
              })
            }
            className="h-7 max-w-[170px] rounded-md border border-white/[0.08] bg-[#0A0A0B] px-2 text-xs text-[#F4F4F5]"
          >
            <option value="">Topic: All</option>
            {topics
              .filter((t) => t.status === "active" || t.status === "proposed")
              .map((t) => (
                <option key={t.topic_id} value={t.slug}>
                  {t.pref_label}
                </option>
              ))}
          </select>

          <button
            type="button"
            data-testid="table-conflicts-toggle"
            aria-pressed={filters.conflicts_only}
            onClick={() =>
              onChangeFilters({
                ...filters,
                conflicts_only: !filters.conflicts_only,
              })
            }
            className={`flex h-7 items-center gap-1 rounded-md border px-2 text-xs font-medium transition-colors ${
              filters.conflicts_only
                ? "border-[#F43F5E]/50 bg-[#2A1219] text-[#FB7185]"
                : "border-white/[0.08] bg-[#0A0A0B] text-[#A1A1AA] hover:text-[#FAFAFA]"
            }`}
          >
            <AlertTriangle className="h-3 w-3" />
            <span>Conflicts</span>
          </button>
        </div>

        <span className="font-mono text-xs tabular-nums text-[#94949E]">
          {rows.length} / {total}
        </span>
      </div>

      {/* Dense sortable grid */}
      <div className="flex-1 overflow-auto">
        <table
          data-testid="entities-table"
          className="w-full border-collapse text-left text-xs"
        >
          <thead className="sticky top-0 z-10 border-b border-white/[0.08] bg-[#111113] text-[#A1A1AA]">
            <tr>
              <th
                scope="col"
                aria-sort={
                  sortKey === "display_name"
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="px-4 py-2 font-medium"
              >
                <button
                  type="button"
                  onClick={() => toggleSort("display_name")}
                  className="inline-flex items-center gap-1 hover:text-[#FAFAFA]"
                >
                  Entity
                  <SortIndicator col="display_name" />
                </button>
              </th>
              <th
                scope="col"
                aria-sort={
                  sortKey === "entity_type"
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="px-3 py-2 font-medium"
              >
                <button
                  type="button"
                  onClick={() => toggleSort("entity_type")}
                  className="inline-flex items-center gap-1 hover:text-[#FAFAFA]"
                >
                  Type
                  <SortIndicator col="entity_type" />
                </button>
              </th>
              <th
                scope="col"
                aria-sort={
                  sortKey === "open_conflict_count"
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="px-3 py-2 font-medium"
              >
                <button
                  type="button"
                  onClick={() => toggleSort("open_conflict_count")}
                  className="inline-flex items-center gap-1 hover:text-[#FAFAFA]"
                >
                  Status
                  <SortIndicator col="open_conflict_count" />
                </button>
              </th>
              <th scope="col" className="px-3 py-2 font-medium">
                Topics
              </th>
              <th
                scope="col"
                aria-sort={
                  sortKey === "edge_count"
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="px-3 py-2 text-right font-medium"
              >
                <button
                  type="button"
                  onClick={() => toggleSort("edge_count")}
                  className="inline-flex items-center gap-1 hover:text-[#FAFAFA]"
                >
                  Edges
                  <SortIndicator col="edge_count" />
                </button>
              </th>
              <th
                scope="col"
                aria-sort={
                  sortKey === "wall_side"
                    ? sortDir === "asc"
                      ? "ascending"
                      : "descending"
                    : "none"
                }
                className="px-4 py-2 text-right font-medium"
              >
                <button
                  type="button"
                  onClick={() => toggleSort("wall_side")}
                  className="inline-flex items-center gap-1 hover:text-[#FAFAFA]"
                >
                  Wall
                  <SortIndicator col="wall_side" />
                </button>
              </th>
            </tr>
          </thead>

          <tbody className="divide-y divide-white/[0.06]">
            {loading ? (
              Array.from({ length: 8 }).map((_, i) => (
                <tr key={i}>
                  <td colSpan={6} className="px-4 py-3">
                    <div className="h-3 w-2/3 animate-pulse rounded bg-[#18181B]" />
                  </td>
                </tr>
              ))
            ) : rows.length === 0 ? (
              <tr>
                <td
                  colSpan={6}
                  className="px-4 py-12 text-center text-[#94949E]"
                >
                  No matching authorized entities
                </td>
              </tr>
            ) : (
              rows.map((ent) => {
                const selected =
                  selectedEntityId === ent.entity_id ||
                  selectedEntityId === ent.canonical_key;
                const hasConflict = Boolean(
                  ent.has_conflict || (ent.open_conflict_count ?? 0) > 0,
                );
                const edges =
                  ent.edge_count ?? (ent.degree_in ?? 0) + (ent.degree_out ?? 0);

                return (
                  <tr
                    key={ent.entity_id}
                    data-testid={`table-row-${ent.canonical_key}`}
                    onClick={() => onSelectEntity(ent.canonical_key || ent.entity_id)}
                    className={`cursor-pointer transition-colors ${
                      selected
                        ? "bg-[#18181B]"
                        : "hover:bg-[#111113]"
                    }`}
                  >
                    <td className="max-w-[320px] px-4 py-2.5">
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          onSelectEntity(ent.canonical_key || ent.entity_id);
                        }}
                        className="block w-full truncate text-left font-medium text-[#FAFAFA]"
                      >
                        {ent.display_name}
                      </button>
                      <div className="truncate font-mono text-[11px] text-[#94949E]">
                        {ent.canonical_key}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5">
                      <span className="rounded border border-white/[0.08] bg-[#111113] px-1.5 py-0.5 font-mono text-[11px] text-[#A1A1AA]">
                        {formatEntityType(ent.entity_type)}
                      </span>
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5">
                      {hasConflict ? (
                        <span className="inline-flex items-center gap-1 rounded bg-[#2A1219] px-1.5 py-0.5 font-mono text-[11px] tabular-nums text-[#FB7185]">
                          <span className="h-1.5 w-1.5 rounded-full bg-[#F43F5E]" />
                          {ent.open_conflict_count || 1} conflict
                        </span>
                      ) : ent.epistemic_badge === "unverified_observation" ? (
                        <span className="inline-flex items-center gap-1 text-[#FBBF24]">
                          <span className="h-1.5 w-1.5 rounded-full bg-[#F59E0B]" />
                          quarantined
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-[#34D399]">
                          <span className="h-1.5 w-1.5 rounded-full bg-[#34D399]" />
                          approved
                        </span>
                      )}
                    </td>
                    <td className="max-w-[200px] truncate px-3 py-2.5 font-mono text-[11px] text-[#94949E]">
                      {ent.topics && ent.topics.length > 0
                        ? ent.topics.slice(0, 2).map((t) => `#${t}`).join(" ")
                        : "—"}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2.5 text-right font-mono tabular-nums text-[#A1A1AA]">
                      {edges}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2.5 text-right font-mono text-[11px]">
                      {ent.wall_side === "private_side" ? (
                        <span className="inline-flex items-center gap-1 text-[#FBBF24]">
                          <Lock className="h-3 w-3" />
                          {formatWallSideLabel(ent.wall_side)}
                        </span>
                      ) : (
                        <span className="text-[#94949E]">
                          {formatWallSideLabel(ent.wall_side)}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
