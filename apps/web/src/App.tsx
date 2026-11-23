import { useCallback, useEffect, useState } from "react";
import { ShieldOff } from "lucide-react";
import { api, ApiError } from "./api/client";
import { CommandPalette } from "./components/CommandPalette";
import { GraphView } from "./components/GraphView";
import { Header } from "./components/Header";
import { HomeView } from "./components/HomeView";
import { InspectorDrawer } from "./components/InspectorDrawer";
import { MyContextView } from "./components/MyContextView";
import { TableView, type TableFilterState } from "./components/TableView";
import { UploadTriageDrawer } from "./components/UploadTriageDrawer";
import { ViewportAgentPanel } from "./components/ViewportAgentPanel";
import type {
  AdapterConfigDTO,
  AgentCitationDTO,
  AuthContextState,
  EntitySummaryDTO,
  GraphDirection,
  GraphLens,
  HealthDTO,
  PersonaDTO,
  PrincipalMeDTO,
  StreamItemDTO,
  TemporalSnapshot,
  TopicDTO,
  ViewKey,
  WatchlistAlertDTO,
} from "./types/api";
import { FALLBACK_PERSONAS, TEMPORAL_SNAPSHOTS } from "./utils/platform";

function getInitialView(): ViewKey {
  const params = new URLSearchParams(window.location.search);
  const v = params.get("view");
  if (v === "home" || v === "table" || v === "graph" || v === "memory") {
    return v;
  }
  return "home";
}

function getInitialLens(): GraphLens {
  const params = new URLSearchParams(window.location.search);
  const l = params.get("lens");
  if (
    l === "topic" ||
    l === "app" ||
    l === "skill" ||
    l === "project" ||
    l === "issuer" ||
    l === "fund_hierarchy"
  ) {
    return l;
  }
  return "fund_hierarchy";
}

function getInitialUser(): string {
  const params = new URLSearchParams(window.location.search);
  return params.get("user") || "user:fund-steward";
}

export function App() {
  const [activeView, setActiveView] = useState<ViewKey>(getInitialView);
  const [auth, setAuth] = useState<AuthContextState>({
    userId: getInitialUser(),
    actorMode: "obo",
    channelMode: "direct",
    groupsOverage: getInitialUser() === "user:overage-director",
  });
  const [snapshot, setSnapshot] = useState<TemporalSnapshot>(
    TEMPORAL_SNAPSHOTS[0],
  );

  // Server identity, health, adapters
  const [me, setMe] = useState<PrincipalMeDTO | null>(null);
  const [isRevoked, setIsRevoked] = useState(false);
  const [personas, setPersonas] = useState<PersonaDTO[]>(FALLBACK_PERSONAS);
  const [health, setHealth] = useState<HealthDTO | null>(null);
  const [adapters, setAdapters] = useState<AdapterConfigDTO | null>(null);
  const [dataEpoch, setDataEpoch] = useState<number>(0);

  // Domain data slices
  const [topics, setTopics] = useState<TopicDTO[]>([]);
  const [topicTree, setTopicTree] = useState<TopicDTO[]>([]);
  const [selectedTopicSlug, setSelectedTopicSlug] = useState<string | null>(
    null,
  );

  const [streamItems, setStreamItems] = useState<StreamItemDTO[]>([]);
  const [streamCounts, setStreamCounts] = useState({
    entities: 0,
    open_conflicts: 0,
    observations: 0,
  });
  const [loadingStream, setLoadingStream] = useState(false);

  const [tableFilters, setTableFilters] = useState<TableFilterState>({
    conflicts_only: false,
    q: "",
  });
  const [entities, setEntities] = useState<EntitySummaryDTO[]>([]);
  const [entitiesTotal, setEntitiesTotal] = useState(0);
  const [loadingEntities, setLoadingEntities] = useState(false);

  const [watchlistAlerts, setWatchlistAlerts] = useState<WatchlistAlertDTO[]>(
    [],
  );

  // Graph lens & focus state
  const [graphLens, setGraphLens] = useState<GraphLens>(getInitialLens);
  const [graphFocusNodeId, setGraphFocusNodeId] = useState<string | null>(null);
  const [graphFocusDirection, setGraphFocusDirection] =
    useState<GraphDirection>("both");
  const [visibleEntityIds, setVisibleEntityIds] = useState<string[]>([]);

  // Overlays & Drawers
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null);
  const [cmdkOpen, setCmdkOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);

  // Global Ctrl+K / Cmd+K shortcut
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdkOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // Load personas & health on boot
  useEffect(() => {
    api
      .getPersonas(auth)
      .then((res) => {
        if (res.personas && res.personas.length > 0) setPersonas(res.personas);
      })
      .catch(() => {
        // Keep FALLBACK_PERSONAS
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const refreshHealthAndAdapters = useCallback(async () => {
    try {
      const [h, cfg] = await Promise.all([
        api.getHealth(auth),
        api.getAdapterConfig(auth),
      ]);
      setHealth(h);
      setAdapters(cfg);
      if (typeof h.data_epoch === "number") {
        setDataEpoch(h.data_epoch);
      }
    } catch {
      // Ignore if revoked or offline
    }
  }, [auth]);

  // Load caller identity (/api/v1/me) on auth changes
  useEffect(() => {
    let cancelled = false;
    setSelectedEntityId(null);
    setGraphFocusNodeId(null);

    api
      .getMe(auth)
      .then((profile) => {
        if (cancelled) return;
        setMe(profile);
        setIsRevoked(false);
        void refreshHealthAndAdapters();
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          setIsRevoked(true);
          setMe(null);
          setStreamItems([]);
          setEntities([]);
          setTopics([]);
          setTopicTree([]);
          setWatchlistAlerts([]);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [auth, refreshHealthAndAdapters]);

  // Load SKOS topics
  const loadTopics = useCallback(async () => {
    if (isRevoked) return;
    try {
      const res = await api.getTopics(auth, {
        as_of: snapshot.asOf,
        recorded_as_of: snapshot.recordedAsOf,
      });
      setTopics(res.topics || []);
      setTopicTree(res.tree || []);
    } catch {
      setTopics([]);
      setTopicTree([]);
    }
  }, [auth, snapshot, isRevoked]);

  useEffect(() => {
    void loadTopics();
  }, [loadTopics, dataEpoch]);

  // Load Home stream
  useEffect(() => {
    if (isRevoked) return;
    let cancelled = false;
    setLoadingStream(true);

    api
      .getStream(auth, {
        topic: selectedTopicSlug || undefined,
        as_of: snapshot.asOf,
        recorded_as_of: snapshot.recordedAsOf,
        limit: 80,
      })
      .then((res) => {
        if (cancelled) return;
        setStreamItems(res.items || []);
        setStreamCounts(
          res.counts || {
            entities: res.items?.length || 0,
            open_conflicts: 0,
            observations: 0,
          },
        );
        if (typeof res.data_epoch === "number") setDataEpoch(res.data_epoch);
        if (activeView === "home") {
          setVisibleEntityIds((res.items || []).map((i) => i.entity_id));
        }
      })
      .catch(() => {
        if (!cancelled) setStreamItems([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingStream(false);
      });

    return () => {
      cancelled = true;
    };
  }, [auth, snapshot, selectedTopicSlug, dataEpoch, isRevoked, activeView]);

  // Load Entities (for Table, My Context pickers, Upload pickers)
  useEffect(() => {
    if (isRevoked) return;
    let cancelled = false;
    setLoadingEntities(true);

    api
      .getEntities(auth, {
        domain_nature: tableFilters.domain_nature,
        modality: tableFilters.modality,
        epistemic_temporal: tableFilters.epistemic_temporal,
        ingestion_channel: tableFilters.ingestion_channel,
        topic: tableFilters.topic,
        q: tableFilters.q.trim() || undefined,
        as_of: snapshot.asOf,
        recorded_as_of: snapshot.recordedAsOf,
        limit: 120,
      })
      .then((res) => {
        if (cancelled) return;
        setEntities(res.items || []);
        setEntitiesTotal(res.total ?? res.items?.length ?? 0);
        if (activeView === "table") {
          setVisibleEntityIds((res.items || []).map((e) => e.entity_id));
        }
      })
      .catch(() => {
        if (!cancelled) setEntities([]);
      })
      .finally(() => {
        if (!cancelled) setLoadingEntities(false);
      });

    return () => {
      cancelled = true;
    };
  }, [auth, snapshot, tableFilters, dataEpoch, isRevoked, activeView]);

  // Load Watchlist alerts when personal memory is enabled
  useEffect(() => {
    if (isRevoked || !me?.memory_enabled) {
      setWatchlistAlerts([]);
      return;
    }
    let cancelled = false;
    api
      .getWatchlist(auth)
      .then((res) => {
        if (!cancelled) setWatchlistAlerts(res.alerts || []);
      })
      .catch(() => {
        if (!cancelled) setWatchlistAlerts([]);
      });
    return () => {
      cancelled = true;
    };
  }, [auth, me?.memory_enabled, dataEpoch, isRevoked]);

  // Subscribe to lightweight non-sensitive SSE epoch stream
  useEffect(() => {
    if (isRevoked || typeof EventSource === "undefined") return;
    let es: EventSource | null = null;
    try {
      es = new EventSource("/api/v1/events");
      const bump = () => setDataEpoch((e) => e + 1);
      es.addEventListener("epoch_updated", bump);
      es.addEventListener("claim_written", bump);
      es.addEventListener("conflict_resolved", bump);
      es.addEventListener("snapshot_published", bump);
    } catch {
      // SSE optional in static preview
    }
    return () => {
      es?.close();
    };
  }, [isRevoked]);

  const handleUpdateAdapters = async (
    patch: Partial<Pick<AdapterConfigDTO, "store" | "retrieval" | "llm_mode">>,
  ) => {
    const updated = await api.updateAdapterConfig(auth, patch);
    setAdapters(updated);
    setDataEpoch((e) => updated.data_epoch ?? e + 1);
    void refreshHealthAndAdapters();
  };

  const handleCreateTopic = async (payload: {
    slug: string;
    pref_label: string;
    broader_topic_id?: string | null;
  }) => {
    const res = await api.createTopic(auth, {
      ...payload,
      topic_kind: "emergent_candidate",
    });
    setDataEpoch(res.data_epoch);
    await loadTopics();
  };

  const handleArchiveTopic = async (topicId: string) => {
    const res = await api.updateTopic(auth, topicId, { status: "archived" });
    setDataEpoch(res.data_epoch);
    await loadTopics();
  };

  const handleSelectEntity = useCallback((idOrKey: string) => {
    setSelectedEntityId(idOrKey);
  }, []);

  const handleFocusInGraph = useCallback(
    (entityId: string, direction: GraphDirection = "both") => {
      setActiveView("graph");
      setGraphFocusNodeId(entityId);
      setGraphFocusDirection(direction);
    },
    [],
  );

  const handleSelectTopicGlobal = useCallback((slug: string) => {
    setSelectedTopicSlug(slug);
    setTableFilters((prev) => ({ ...prev, topic: slug }));
    setActiveView("home");
  }, []);

  const handleAgentCitation = useCallback(
    (citation: AgentCitationDTO, fallbackEntityId?: string) => {
      if (citation.kind === "topic") {
        handleSelectTopicGlobal(citation.id.replace(/^topic:/, ""));
      } else if (citation.kind === "entity") {
        setSelectedEntityId(citation.id);
        if (activeView === "graph") {
          setGraphFocusNodeId(citation.id);
        }
      } else if (fallbackEntityId) {
        setSelectedEntityId(fallbackEntityId);
      }
    },
    [activeView, handleSelectTopicGlobal],
  );

  const handleMutated = useCallback(() => {
    setDataEpoch((e) => e + 1);
    void refreshHealthAndAdapters();
  }, [refreshHealthAndAdapters]);

  return (
    <div className="flex h-screen w-screen flex-col overflow-hidden bg-[#0A0A0B] text-[#F4F4F5]">
      <Header
        activeView={activeView}
        onSelectView={setActiveView}
        onOpenCommandPalette={() => setCmdkOpen(true)}
        snapshot={snapshot}
        onSelectSnapshot={setSnapshot}
        auth={auth}
        me={me}
        personas={personas}
        onChangeAuth={setAuth}
        onOpenUpload={() => setUploadOpen(true)}
        agentOpen={agentOpen}
        onToggleAgent={() => setAgentOpen((v) => !v)}
        health={health}
        adapters={adapters}
        onUpdateAdapters={handleUpdateAdapters}
        isRevoked={isRevoked}
      />

      <div className="flex min-h-0 min-w-0 flex-1 overflow-hidden">
        <main
          role="main"
          className="flex min-h-0 min-w-0 flex-1 overflow-hidden"
        >
          <h1 className="sr-only">Context Lake — {activeView}</h1>
          {isRevoked ? (
            <div
              data-testid="revoked-state"
              className="flex flex-1 items-center justify-center p-8"
            >
              <div className="flex items-center gap-3 rounded-lg border border-[#F43F5E]/35 bg-[#111113] px-5 py-4 text-sm">
                <ShieldOff className="h-5 w-5 text-[#F43F5E]" />
                <div>
                  <div className="font-medium text-[#FAFAFA]">
                    401 · Access Revoked
                  </div>
                  <div className="mt-0.5 font-mono text-xs text-[#A1A1AA]">
                    {auth.userId}
                  </div>
                </div>
              </div>
            </div>
          ) : activeView === "home" ? (
            <HomeView
              items={streamItems}
              counts={streamCounts}
              loading={loadingStream}
              topics={topics}
              topicTree={topicTree}
              selectedTopicSlug={selectedTopicSlug}
              onSelectTopic={setSelectedTopicSlug}
              onCreateTopic={handleCreateTopic}
              onArchiveTopic={handleArchiveTopic}
              selectedEntityId={selectedEntityId}
              onSelectEntity={handleSelectEntity}
              memoryEnabled={Boolean(me?.memory_enabled)}
              watchlistAlerts={watchlistAlerts}
              onOpenMemory={() => setActiveView("memory")}
            />
          ) : activeView === "table" ? (
            <TableView
              entities={entities}
              total={entitiesTotal}
              loading={loadingEntities}
              topics={topics}
              filters={tableFilters}
              onChangeFilters={setTableFilters}
              selectedEntityId={selectedEntityId}
              onSelectEntity={handleSelectEntity}
            />
          ) : activeView === "graph" ? (
            <GraphView
              auth={auth}
              snapshot={snapshot}
              topics={topics}
              adapters={adapters}
              onUpdateAdapters={handleUpdateAdapters}
              lens={graphLens}
              onChangeLens={setGraphLens}
              focusNodeId={graphFocusNodeId}
              focusDirection={graphFocusDirection}
              onChangeFocus={(nodeId, dir) => {
                setGraphFocusNodeId(nodeId);
                if (dir) setGraphFocusDirection(dir);
              }}
              selectedEntityId={selectedEntityId}
              onSelectEntity={handleSelectEntity}
              onVisibleEntityIdsChange={setVisibleEntityIds}
              dataEpoch={dataEpoch}
            />
          ) : (
            <MyContextView
              auth={auth}
              snapshot={snapshot}
              entities={entities}
              topics={topics}
              onSelectEntity={handleSelectEntity}
              onMutated={handleMutated}
            />
          )}
        </main>

        {/* Shared Right-Hand Split-Pane Inspector Drawer */}
        {!isRevoked && selectedEntityId && (
          <InspectorDrawer
            entityIdOrKey={selectedEntityId}
            auth={auth}
            me={me}
            snapshot={snapshot}
            dataEpoch={dataEpoch}
            onClose={() => setSelectedEntityId(null)}
            onSelectEntity={handleSelectEntity}
            onFocusInGraph={handleFocusInGraph}
            onSelectTopic={handleSelectTopicGlobal}
            onMutated={handleMutated}
          />
        )}

        {/* Docked Viewport Agent */}
        {!isRevoked && agentOpen && (
          <ViewportAgentPanel
            open={agentOpen}
            onClose={() => setAgentOpen(false)}
            auth={auth}
            snapshot={snapshot}
            activeLens={graphLens}
            visibleEntityIds={visibleEntityIds}
            selectedEntityId={selectedEntityId}
            onSelectCitation={handleAgentCitation}
          />
        )}
      </div>

      {/* Global Ctrl+K / Cmd+K Command Palette */}
      <CommandPalette
        open={cmdkOpen}
        onClose={() => setCmdkOpen(false)}
        auth={auth}
        snapshot={snapshot}
        onSelectEntity={handleSelectEntity}
        onSelectTopic={handleSelectTopicGlobal}
        onNavigateView={setActiveView}
        onOpenUpload={() => setUploadOpen(true)}
        onToggleAgent={() => setAgentOpen((v) => !v)}
      />

      {/* Manual Upload & Quarantine Triage Drawer */}
      <UploadTriageDrawer
        open={uploadOpen}
        onClose={() => setUploadOpen(false)}
        auth={auth}
        me={me}
        entities={entities}
        topics={topics}
        dataEpoch={dataEpoch}
        onMutated={handleMutated}
      />
    </div>
  );
}
export default App;
