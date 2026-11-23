import { useEffect, useRef, useState } from "react";
import {
  Bot,
  Check,
  ChevronDown,
  Clock,
  Search,
  SlidersHorizontal,
  Upload,
} from "lucide-react";
import type {
  AdapterConfigDTO,
  AuthContextState,
  HealthDTO,
  PersonaDTO,
  PrincipalMeDTO,
  TemporalSnapshot,
  ViewKey,
} from "../types/api";
import {
  TEMPORAL_SNAPSHOTS,
  formatWallSideLabel,
  getSearchShortcutLabel,
  shortPersonaRole,
} from "../utils/platform";

interface HeaderProps {
  activeView: ViewKey;
  onSelectView: (view: ViewKey) => void;
  onOpenCommandPalette: () => void;
  snapshot: TemporalSnapshot;
  onSelectSnapshot: (snapshot: TemporalSnapshot) => void;
  auth: AuthContextState;
  me: PrincipalMeDTO | null;
  personas: PersonaDTO[];
  onChangeAuth: (next: AuthContextState) => void;
  onOpenUpload: () => void;
  agentOpen: boolean;
  onToggleAgent: () => void;
  health: HealthDTO | null;
  adapters: AdapterConfigDTO | null;
  onUpdateAdapters: (
    patch: Partial<Pick<AdapterConfigDTO, "store" | "retrieval" | "llm_mode">>,
  ) => Promise<void>;
  isRevoked: boolean;
}

const NAV_ITEMS: Array<{ key: ViewKey; label: string }> = [
  { key: "home", label: "Home" },
  { key: "table", label: "Table" },
  { key: "graph", label: "Graph" },
  { key: "memory", label: "My Context" },
];

export function Header({
  activeView,
  onSelectView,
  onOpenCommandPalette,
  snapshot,
  onSelectSnapshot,
  auth,
  me,
  personas,
  onChangeAuth,
  onOpenUpload,
  agentOpen,
  onToggleAgent,
  health,
  adapters,
  onUpdateAdapters,
  isRevoked,
}: HeaderProps) {
  const [openMenu, setOpenMenu] = useState<
    null | "snapshot" | "persona" | "runtime"
  >(null);
  const [customAsOf, setCustomAsOf] = useState(snapshot.asOf?.slice(0, 10) || "");
  const [customRecorded, setCustomRecorded] = useState(
    snapshot.recordedAsOf?.slice(0, 10) || "",
  );
  const [adapterBusy, setAdapterBusy] = useState(false);
  const headerRef = useRef<HTMLElement>(null);

  const shortcutLabel = getSearchShortcutLabel();

  useEffect(() => {
    const onPointerDown = (e: MouseEvent) => {
      if (headerRef.current && !headerRef.current.contains(e.target as Node)) {
        setOpenMenu(null);
      }
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpenMenu(null);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  const currentPersona =
    personas.find((p) => p.user_id === auth.userId) || personas[0];

  const wallDotClass = isRevoked
    ? "bg-[#F43F5E]"
    : me?.wall_side === "above_the_wall" ||
        currentPersona?.wall_side === "above_the_wall"
      ? "bg-[#38BDF8]"
      : me?.wall_side === "private_side" ||
          currentPersona?.wall_side === "private_side"
        ? "bg-[#F59E0B]"
        : "bg-[#34D399]";

  const applyCustomDate = () => {
    if (!customAsOf) return;
    const asOfIso = `${customAsOf}T23:59:59Z`;
    const recIso = customRecorded ? `${customRecorded}T23:59:59Z` : undefined;
    onSelectSnapshot({
      id: "custom",
      label: recIso ? `${customAsOf} (rec ${customRecorded})` : customAsOf,
      shortLabel: customAsOf,
      asOf: asOfIso,
      recordedAsOf: recIso,
    });
    setOpenMenu(null);
  };

  const handleAdapterChange = async (
    patch: Partial<Pick<AdapterConfigDTO, "store" | "retrieval" | "llm_mode">>,
  ) => {
    setAdapterBusy(true);
    try {
      await onUpdateAdapters(patch);
    } finally {
      setAdapterBusy(false);
    }
  };

  return (
    <header
      ref={headerRef}
      role="banner"
      data-testid="app-header"
      className="relative z-40 flex h-12 shrink-0 items-center justify-between gap-3 border-b border-white/[0.08] bg-[#0A0A0B] px-4"
    >
      {/* Left: Brand mark + 4 lean views */}
      <div className="flex min-w-0 items-center gap-4">
        <button
          type="button"
          onClick={() => onSelectView("home")}
          className="flex shrink-0 items-center gap-2 rounded px-1 py-1 text-left text-sm font-medium tracking-tight text-[#FAFAFA] hover:bg-white/[0.04]"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden="true"
            className="shrink-0"
          >
            <rect
              x="1.5"
              y="1.5"
              width="13"
              height="13"
              rx="3"
              stroke="#FAFAFA"
              strokeWidth="1.25"
            />
            <circle cx="5.5" cy="5.5" r="1.35" fill="#FAFAFA" />
            <circle cx="10.5" cy="10.5" r="1.35" fill="#FAFAFA" />
            <path
              d="M6.6 6.6L9.4 9.4"
              stroke="#A1A1AA"
              strokeWidth="1.25"
              strokeLinecap="round"
            />
          </svg>
          <span>Context Lake</span>
        </button>

        <nav aria-label="Primary" className="flex items-center gap-0.5">
          {NAV_ITEMS.map((item) => {
            const active = activeView === item.key;
            return (
              <button
                key={item.key}
                type="button"
                data-testid={`nav-${item.key}`}
                aria-current={active ? "page" : undefined}
                onClick={() => onSelectView(item.key)}
                className={`rounded-md px-2.5 py-1 text-[13px] font-medium transition-colors ${
                  active
                    ? "bg-[#18181B] text-[#FAFAFA]"
                    : "text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
                }`}
              >
                {item.label}
              </button>
            );
          })}
        </nav>
      </div>

      {/* Center: Universal Search trigger */}
      <button
        type="button"
        data-testid="cmdk-trigger"
        onClick={onOpenCommandPalette}
        title={`Search (${shortcutLabel})`}
        className="flex h-8 w-52 items-center justify-between gap-2 rounded-md border border-white/[0.08] bg-[#111113] px-2.5 text-[13px] text-[#A1A1AA] transition-colors hover:border-white/[0.16] hover:text-[#FAFAFA] lg:w-64"
      >
        <span className="flex items-center gap-2 truncate">
          <Search className="h-3.5 w-3.5 shrink-0 text-[#94949E]" aria-hidden="true" />
          <span className="truncate">Search…</span>
        </span>
        <kbd
          data-testid="cmdk-shortcut-badge"
          aria-hidden="true"
          className="shrink-0 rounded border border-white/[0.12] bg-[#18181B] px-1.5 py-0.5 font-mono text-[11px] leading-none text-[#A1A1AA]"
        >
          {shortcutLabel}
        </kbd>
      </button>

      {/* Right: Snapshot as_of, Persona switcher, Upload, Agent, Runtime dot */}
      <div className="flex shrink-0 items-center gap-1.5">
        {/* Snapshot as_of selector */}
        <div className="relative">
          <button
            type="button"
            data-testid="snapshot-trigger"
            aria-expanded={openMenu === "snapshot"}
            aria-haspopup="menu"
            onClick={() =>
              setOpenMenu(openMenu === "snapshot" ? null : "snapshot")
            }
            className={`flex h-8 items-center gap-1.5 rounded-md border px-2 text-xs font-medium transition-colors ${
              snapshot.id !== "latest"
                ? "border-[#F59E0B]/40 bg-[#18181B] text-[#FBBF24]"
                : "border-white/[0.08] bg-[#111113] text-[#F4F4F5] hover:border-white/[0.16]"
            }`}
          >
            <Clock className="h-3.5 w-3.5 shrink-0 text-[#A1A1AA]" />
            <span className="tabular-nums">{snapshot.shortLabel}</span>
            <ChevronDown className="h-3 w-3 text-[#94949E]" />
          </button>

          {openMenu === "snapshot" && (
            <div
              role="menu"
              data-testid="snapshot-popover"
              className="absolute right-0 top-10 z-50 w-72 rounded-lg border border-white/[0.08] bg-[#111113] p-1.5 shadow-2xl"
            >
              {TEMPORAL_SNAPSHOTS.map((preset) => {
                const selected = snapshot.id === preset.id;
                return (
                  <button
                    key={preset.id}
                    type="button"
                    role="menuitem"
                    data-testid={`snapshot-option-${preset.id}`}
                    onClick={() => {
                      onSelectSnapshot(preset);
                      setOpenMenu(null);
                    }}
                    className={`flex w-full items-center justify-between rounded-md px-2.5 py-1.5 text-left text-xs transition-colors ${
                      selected
                        ? "bg-[#18181B] text-[#FAFAFA]"
                        : "text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
                    }`}
                  >
                    <span className="tabular-nums">{preset.label}</span>
                    {selected && <Check className="h-3.5 w-3.5 text-[#FAFAFA]" />}
                  </button>
                );
              })}

              <div className="mt-1.5 border-t border-white/[0.08] pt-2 px-2 pb-1">
                <div className="grid grid-cols-2 gap-1.5">
                  <label className="flex flex-col gap-1 text-[11px] text-[#A1A1AA]">
                    <span>as_of</span>
                    <input
                      type="date"
                      value={customAsOf}
                      onChange={(e) => setCustomAsOf(e.target.value)}
                      className="rounded border border-white/[0.12] bg-[#0A0A0B] px-1.5 py-1 font-mono text-[11px] text-[#FAFAFA]"
                    />
                  </label>
                  <label className="flex flex-col gap-1 text-[11px] text-[#A1A1AA]">
                    <span>recorded_as_of</span>
                    <input
                      type="date"
                      value={customRecorded}
                      onChange={(e) => setCustomRecorded(e.target.value)}
                      className="rounded border border-white/[0.12] bg-[#0A0A0B] px-1.5 py-1 font-mono text-[11px] text-[#FAFAFA]"
                    />
                  </label>
                </div>
                <button
                  type="button"
                  onClick={applyCustomDate}
                  disabled={!customAsOf}
                  className="mt-2 w-full rounded bg-[#FAFAFA] py-1 text-xs font-medium text-[#0A0A0B] hover:bg-[#E4E4E7] disabled:opacity-40"
                >
                  Apply Date
                </button>
              </div>
            </div>
          )}
        </div>

        {/* Persona & Channel switcher */}
        <div className="relative">
          <button
            type="button"
            data-testid="persona-trigger"
            aria-expanded={openMenu === "persona"}
            aria-haspopup="menu"
            onClick={() =>
              setOpenMenu(openMenu === "persona" ? null : "persona")
            }
            className="flex h-8 items-center gap-1.5 rounded-md border border-white/[0.08] bg-[#111113] px-2 text-xs font-medium text-[#F4F4F5] transition-colors hover:border-white/[0.16]"
          >
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${wallDotClass}`}
              aria-hidden="true"
            />
            <span className="max-w-[120px] truncate">
              {currentPersona ? shortPersonaRole(currentPersona) : auth.userId}
            </span>
            {auth.channelMode === "shared" && (
              <span className="rounded bg-[#202024] px-1 py-0.5 font-mono text-[10px] text-[#FBBF24]">
                shared
              </span>
            )}
            <ChevronDown className="h-3 w-3 text-[#94949E]" />
          </button>

          {openMenu === "persona" && (
            <div
              role="menu"
              data-testid="persona-popover"
              className="absolute right-0 top-10 z-50 w-72 rounded-lg border border-white/[0.08] bg-[#111113] p-1.5 shadow-2xl"
            >
              <div className="space-y-0.5">
                {personas.map((p) => {
                  const selected = p.user_id === auth.userId;
                  const dot = p.revoked
                    ? "bg-[#F43F5E]"
                    : p.wall_side === "above_the_wall"
                      ? "bg-[#38BDF8]"
                      : p.wall_side === "private_side"
                        ? "bg-[#F59E0B]"
                        : "bg-[#34D399]";
                  return (
                    <button
                      key={p.user_id}
                      type="button"
                      role="menuitem"
                      data-testid={`persona-option-${p.user_id}`}
                      onClick={() => {
                        onChangeAuth({
                          ...auth,
                          userId: p.user_id,
                          groupsOverage: p.groups_overage,
                        });
                        setOpenMenu(null);
                      }}
                      className={`flex w-full items-center justify-between gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors ${
                        selected
                          ? "bg-[#18181B] text-[#FAFAFA]"
                          : "text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
                      }`}
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <span className={`h-2 w-2 shrink-0 rounded-full ${dot}`} />
                        <span className="truncate font-medium text-[#F4F4F5]">
                          {shortPersonaRole(p)}
                        </span>
                        <span className="truncate text-[11px] text-[#94949E]">
                          {p.display_name}
                        </span>
                      </span>
                      <span className="shrink-0 font-mono text-[10px] text-[#94949E]">
                        {p.revoked ? "401" : formatWallSideLabel(p.wall_side)}
                      </span>
                    </button>
                  );
                })}
              </div>

              <div className="mt-1.5 flex items-center justify-between gap-2 border-t border-white/[0.08] px-2 pt-2 pb-1">
                <div className="flex items-center gap-1">
                  <span className="text-[11px] text-[#94949E]">Channel</span>
                  <button
                    type="button"
                    data-testid="channel-mode-toggle"
                    onClick={() =>
                      onChangeAuth({
                        ...auth,
                        channelMode:
                          auth.channelMode === "direct" ? "shared" : "direct",
                      })
                    }
                    className="rounded border border-white/[0.12] bg-[#18181B] px-1.5 py-0.5 font-mono text-[11px] text-[#FAFAFA] hover:bg-[#202024]"
                  >
                    {auth.channelMode}
                  </button>
                </div>
                <div className="flex items-center gap-1">
                  <span className="text-[11px] text-[#94949E]">Actor</span>
                  <button
                    type="button"
                    data-testid="actor-mode-toggle"
                    onClick={() =>
                      onChangeAuth({
                        ...auth,
                        actorMode: auth.actorMode === "obo" ? "service" : "obo",
                      })
                    }
                    className="rounded border border-white/[0.12] bg-[#18181B] px-1.5 py-0.5 font-mono text-[11px] text-[#FAFAFA] hover:bg-[#202024]"
                  >
                    {auth.actorMode}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* + Upload trigger */}
        <button
          type="button"
          data-testid="upload-trigger"
          onClick={onOpenUpload}
          className="flex h-8 items-center gap-1.5 rounded-md border border-white/[0.08] bg-[#111113] px-2.5 text-xs font-medium text-[#F4F4F5] transition-colors hover:border-white/[0.16] hover:bg-[#18181B]"
        >
          <Upload className="h-3.5 w-3.5 text-[#A1A1AA]" />
          <span>Upload</span>
        </button>

        {/* Agent toggle */}
        <button
          type="button"
          data-testid="agent-trigger"
          aria-pressed={agentOpen}
          onClick={onToggleAgent}
          className={`flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-medium transition-colors ${
            agentOpen
              ? "border-[#FAFAFA] bg-[#FAFAFA] text-[#0A0A0B]"
              : "border-white/[0.08] bg-[#111113] text-[#F4F4F5] hover:border-white/[0.16] hover:bg-[#18181B]"
          }`}
        >
          <Bot className="h-3.5 w-3.5" />
          <span>Agent</span>
        </button>

        {/* Quiet Runtime / Adapter popover trigger */}
        <div className="relative">
          <button
            type="button"
            data-testid="runtime-status-trigger"
            aria-label="Runtime status and adapters"
            aria-expanded={openMenu === "runtime"}
            onClick={() =>
              setOpenMenu(openMenu === "runtime" ? null : "runtime")
            }
            className="flex h-8 w-8 items-center justify-center rounded-md border border-white/[0.08] bg-[#111113] text-[#A1A1AA] transition-colors hover:border-white/[0.16] hover:text-[#FAFAFA]"
          >
            <span className="relative flex h-2 w-2">
              <span
                className={`h-2 w-2 rounded-full ${
                  health?.status === "ok" ? "bg-[#34D399]" : "bg-[#F43F5E]"
                }`}
              />
            </span>
          </button>

          {openMenu === "runtime" && (
            <div
              role="dialog"
              aria-label="Runtime Adapters"
              data-testid="runtime-popover"
              className="absolute right-0 top-10 z-50 w-72 rounded-lg border border-white/[0.08] bg-[#111113] p-3 shadow-2xl"
            >
              <div className="mb-2.5 flex items-center justify-between border-b border-white/[0.08] pb-2">
                <span className="flex items-center gap-1.5 text-xs font-medium text-[#FAFAFA]">
                  <SlidersHorizontal className="h-3.5 w-3.5 text-[#A1A1AA]" />
                  Runtime
                </span>
                <span className="font-mono text-[11px] tabular-nums text-[#94949E]">
                  e{health?.data_epoch ?? 0} · p{health?.policy_epoch ?? 0}
                </span>
              </div>

              <div className="space-y-2.5 text-xs">
                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">Store</span>
                  <select
                    data-testid="adapter-store-select"
                    disabled={adapterBusy}
                    value={adapters?.store || "sqlite_bitemporal"}
                    onChange={(e) =>
                      handleAdapterChange({
                        store: e.target.value as AdapterConfigDTO["store"],
                      })
                    }
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-xs text-[#FAFAFA]"
                  >
                    <option value="sqlite_bitemporal">sqlite_bitemporal</option>
                    <option value="okf_markdown_vault">okf_markdown_vault</option>
                  </select>
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">Retrieval</span>
                  <select
                    data-testid="adapter-retrieval-select"
                    disabled={adapterBusy}
                    value={adapters?.retrieval || "lexical_manifest"}
                    onChange={(e) =>
                      handleAdapterChange({
                        retrieval: e.target
                          .value as AdapterConfigDTO["retrieval"],
                      })
                    }
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-xs text-[#FAFAFA]"
                  >
                    <option value="lexical_manifest">lexical_manifest</option>
                    <option value="facet_graph">facet_graph</option>
                  </select>
                </label>

                <label className="flex flex-col gap-1">
                  <span className="text-[11px] text-[#A1A1AA]">LLM Mode</span>
                  <select
                    data-testid="adapter-llm-select"
                    disabled={adapterBusy}
                    value={adapters?.llm_mode || "deterministic"}
                    onChange={(e) =>
                      handleAdapterChange({
                        llm_mode: e.target.value as AdapterConfigDTO["llm_mode"],
                      })
                    }
                    className="rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 font-mono text-xs text-[#FAFAFA]"
                  >
                    <option value="deterministic">deterministic</option>
                    <option value="auto">auto</option>
                    <option value="gemini">gemini</option>
                  </select>
                </label>
              </div>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
