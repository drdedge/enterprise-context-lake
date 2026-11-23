import type {
  AdapterConfigDTO,
  AgentChatResponseDTO,
  ApiErrorBody,
  AuthContextState,
  ClaimDTO,
  CompareMemoryResponseDTO,
  ConflictSetDTO,
  EntitiesResponseDTO,
  EntityDetailDTO,
  GraphDirection,
  GraphLens,
  GraphResponseDTO,
  HealthDTO,
  ImpactResponseDTO,
  MemoryDTO,
  MemoryNotesResponseDTO,
  PersonaDTO,
  PrincipalMeDTO,
  PromoteMemoryResponseDTO,
  SearchResponseDTO,
  StreamResponseDTO,
  TopicDTO,
  TopicsResponseDTO,
  UploadResponseDTO,
  WatchlistResponseDTO,
} from "../types/api";

export class ApiError extends Error {
  status: number;
  code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

function buildHeaders(
  auth: AuthContextState,
  extra?: Record<string, string>,
): HeadersInit {
  const headers: Record<string, string> = {
    "X-Mock-User": auth.userId,
    "X-Mock-Actor-Mode": auth.actorMode,
    "X-Mock-Channel-Mode": auth.channelMode,
    "X-Mock-Groups-Overage": auth.groupsOverage ? "true" : "false",
    ...extra,
  };
  return headers;
}

function buildQuery(
  params: Record<string, string | number | boolean | undefined | null>,
): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") {
      search.set(k, String(v));
    }
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

async function requestJson<T>(
  path: string,
  auth: AuthContextState,
  init?: RequestInit & { extraHeaders?: Record<string, string> },
): Promise<T> {
  const isFormData = init?.body instanceof FormData;
  const headers = buildHeaders(auth, {
    ...(isFormData ? {} : { "Content-Type": "application/json" }),
    ...(init?.extraHeaders || {}),
  });

  const response = await fetch(path, {
    ...init,
    headers,
  });

  if (!response.ok) {
    let code = "http_error";
    let message = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as ApiErrorBody;
      code = body.error?.code || code;
      message = body.error?.message || body.detail || message;
    } catch {
      // Ignore non-JSON error body
    }
    throw new ApiError(response.status, code, message);
  }

  return (await response.json()) as T;
}

export const api = {
  getHealth(auth: AuthContextState): Promise<HealthDTO> {
    return requestJson<HealthDTO>("/healthz", auth);
  },

  getMe(auth: AuthContextState): Promise<PrincipalMeDTO> {
    return requestJson<PrincipalMeDTO>("/api/v1/me", auth);
  },

  getPersonas(auth: AuthContextState): Promise<{ personas: PersonaDTO[] }> {
    return requestJson<{ personas: PersonaDTO[] }>("/api/v1/personas", auth);
  },

  getAdapterConfig(auth: AuthContextState): Promise<AdapterConfigDTO> {
    return requestJson<AdapterConfigDTO>("/api/v1/config/adapters", auth);
  },

  updateAdapterConfig(
    auth: AuthContextState,
    payload: Partial<Pick<AdapterConfigDTO, "store" | "retrieval" | "llm_mode">>,
  ): Promise<AdapterConfigDTO> {
    return requestJson<AdapterConfigDTO>("/api/v1/config/adapters", auth, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  getStream(
    auth: AuthContextState,
    params: {
      topic?: string;
      entity_type?: string;
      conflicts_only?: boolean;
      as_of?: string;
      recorded_as_of?: string;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<StreamResponseDTO> {
    return requestJson<StreamResponseDTO>(
      `/api/v1/stream${buildQuery(params)}`,
      auth,
    );
  },

  getEntities(
    auth: AuthContextState,
    params: {
      entity_type?: string;
      topic?: string;
      domain_nature?: string;
      modality?: string;
      epistemic_temporal?: string;
      ingestion_channel?: string;
      q?: string;
      as_of?: string;
      recorded_as_of?: string;
      limit?: number;
      offset?: number;
    } = {},
  ): Promise<EntitiesResponseDTO> {
    return requestJson<EntitiesResponseDTO>(
      `/api/v1/entities${buildQuery(params)}`,
      auth,
    );
  },

  getEntityDetail(
    auth: AuthContextState,
    idOrKey: string,
    params: { as_of?: string; recorded_as_of?: string } = {},
  ): Promise<EntityDetailDTO> {
    return requestJson<EntityDetailDTO>(
      `/api/v1/entities/${encodeURIComponent(idOrKey)}${buildQuery(params)}`,
      auth,
    );
  },

  search(
    auth: AuthContextState,
    params: {
      q: string;
      entity_type?: string;
      topic?: string;
      domain_nature?: string;
      modality?: string;
      epistemic_temporal?: string;
      as_of?: string;
      recorded_as_of?: string;
      limit?: number;
    },
  ): Promise<SearchResponseDTO> {
    return requestJson<SearchResponseDTO>(
      `/api/v1/search${buildQuery(params)}`,
      auth,
    );
  },

  getTopics(
    auth: AuthContextState,
    params: {
      include_archived?: boolean;
      as_of?: string;
      recorded_as_of?: string;
    } = {},
  ): Promise<TopicsResponseDTO> {
    return requestJson<TopicsResponseDTO>(
      `/api/v1/topics${buildQuery(params)}`,
      auth,
    );
  },

  createTopic(
    auth: AuthContextState,
    payload: {
      slug: string;
      pref_label: string;
      alt_labels?: string[];
      broader_topic_id?: string | null;
      related_topic_ids?: string[];
      topic_kind?: string;
      owner_group?: string;
    },
  ): Promise<{ topic: TopicDTO; data_epoch: number }> {
    return requestJson<{ topic: TopicDTO; data_epoch: number }>(
      "/api/v1/topics",
      auth,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  updateTopic(
    auth: AuthContextState,
    topicId: string,
    payload: {
      pref_label?: string;
      alt_labels?: string[];
      broader_topic_id?: string | null;
      merged_into_topic_id?: string | null;
      status?: "active" | "proposed" | "merged" | "archived";
    },
  ): Promise<{ topic: TopicDTO; data_epoch: number }> {
    return requestJson<{ topic: TopicDTO; data_epoch: number }>(
      `/api/v1/topics/${encodeURIComponent(topicId)}`,
      auth,
      {
        method: "PATCH",
        body: JSON.stringify(payload),
      },
    );
  },

  getGraph(
    auth: AuthContextState,
    params: {
      lens: GraphLens;
      focus_id?: string;
      hops?: number;
      direction?: GraphDirection;
      q?: string;
      topic?: string;
      as_of?: string;
      recorded_as_of?: string;
    },
  ): Promise<GraphResponseDTO> {
    return requestJson<GraphResponseDTO>(
      `/api/v1/graph${buildQuery(params)}`,
      auth,
    );
  },

  getImpact(
    auth: AuthContextState,
    nodeId: string,
    params: {
      max_depth?: number;
      as_of?: string;
      recorded_as_of?: string;
    } = {},
  ): Promise<ImpactResponseDTO> {
    return requestJson<ImpactResponseDTO>(
      `/api/v1/map/impact/${encodeURIComponent(nodeId)}${buildQuery(params)}`,
      auth,
    );
  },

  getConflicts(
    auth: AuthContextState,
    params: { status?: "open" | "resolved" | "all"; subject_id?: string } = {},
  ): Promise<{ conflicts: ConflictSetDTO[] }> {
    return requestJson<{ conflicts: ConflictSetDTO[] }>(
      `/api/v1/conflicts${buildQuery(params)}`,
      auth,
    );
  },

  getConflictBlastRadius(
    auth: AuthContextState,
    conflictSetId: string,
    params: { as_of?: string; recorded_as_of?: string } = {},
  ): Promise<ImpactResponseDTO> {
    return requestJson<ImpactResponseDTO>(
      `/api/v1/conflicts/${encodeURIComponent(conflictSetId)}/blast-radius${buildQuery(params)}`,
      auth,
    );
  },

  resolveConflict(
    auth: AuthContextState,
    conflictSetId: string,
    payload: {
      winning_claim_id?: string;
      replacement_object_value?: string;
      valid_from?: string;
      close_prior_valid_to?: string;
      rationale: string;
      expected_data_epoch?: number;
    },
  ): Promise<{
    conflict: ConflictSetDTO;
    approved_claim: ClaimDTO;
    affected_skills: unknown[];
    snapshot_scheduled: boolean;
    data_epoch: number;
  }> {
    return requestJson(
      `/api/v1/conflicts/${encodeURIComponent(conflictSetId)}/resolve`,
      auth,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  createClaim(
    auth: AuthContextState,
    payload: {
      subject_canonical_key: string;
      predicate: string;
      object_value: string;
      object_entity_id?: string | null;
      context_purpose?: string;
      scope_type: string;
      scope_id: string;
      channel?: "fact" | "directive";
      epistemic_status: string;
      kind?: string;
      is_mandatory?: boolean;
      facets?: Record<string, unknown>;
      source_system: string;
      evidence_uri?: string | null;
      evidence_acl_groups?: string[];
      valid_from?: string;
      valid_to?: string | null;
      force_supersede?: boolean;
      expected_data_epoch?: number;
    },
  ): Promise<{
    claim: ClaimDTO;
    conflict_detected: boolean;
    conflict_set_id?: string | null;
    data_epoch: number;
  }> {
    return requestJson("/api/v1/claims", auth, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  patchClaim(
    auth: AuthContextState,
    claimId: string,
    payload: {
      object_value?: string;
      epistemic_status?: string;
      valid_from?: string;
      valid_to?: string | null;
      rationale?: string;
      force_supersede?: boolean;
      expected_data_epoch?: number;
    },
  ): Promise<{
    claim: ClaimDTO;
    superseded_claim_id?: string | null;
    snapshot_scheduled?: boolean;
    data_epoch: number;
  }> {
    return requestJson(`/api/v1/claims/${encodeURIComponent(claimId)}`, auth, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },

  getMemoryNotes(
    auth: AuthContextState,
    skillId?: string,
  ): Promise<MemoryNotesResponseDTO> {
    return requestJson<MemoryNotesResponseDTO>(
      `/api/v1/memory/notes${buildQuery({ skill_id: skillId })}`,
      auth,
    );
  },

  createMemoryNote(
    auth: AuthContextState,
    payload: {
      scope_type: "user" | "user_x_skill";
      skill_id?: string | null;
      key: string;
      content: string;
      tags?: string[];
      linked_entity_ids?: string[];
      linked_topic_ids?: string[];
    },
  ): Promise<{ memory: MemoryDTO }> {
    return requestJson<{ memory: MemoryDTO }>("/api/v1/memory/notes", auth, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },

  updateMemoryNote(
    auth: AuthContextState,
    memoryId: string,
    payload: {
      scope_type?: "user" | "user_x_skill";
      skill_id?: string | null;
      key?: string;
      content?: string;
      tags?: string[];
      linked_entity_ids?: string[];
      linked_topic_ids?: string[];
    },
  ): Promise<{ memory: MemoryDTO }> {
    return requestJson<{ memory: MemoryDTO }>(
      `/api/v1/memory/notes/${encodeURIComponent(memoryId)}`,
      auth,
      {
        method: "PUT",
        body: JSON.stringify(payload),
      },
    );
  },

  deleteMemoryNote(
    auth: AuthContextState,
    memoryId: string,
  ): Promise<{ deleted: boolean }> {
    return requestJson<{ deleted: boolean }>(
      `/api/v1/memory/notes/${encodeURIComponent(memoryId)}`,
      auth,
      { method: "DELETE" },
    );
  },

  compareMemoryNote(
    auth: AuthContextState,
    memoryId: string,
    asOf?: string,
  ): Promise<CompareMemoryResponseDTO> {
    return requestJson<CompareMemoryResponseDTO>(
      `/api/v1/memory/notes/${encodeURIComponent(memoryId)}/compare`,
      auth,
      {
        method: "POST",
        body: JSON.stringify(asOf ? { as_of: asOf } : {}),
      },
    );
  },

  promoteMemoryNote(
    auth: AuthContextState,
    memoryId: string,
    payload: {
      subject_canonical_key: string;
      predicate?: string;
      rationale?: string;
    },
  ): Promise<PromoteMemoryResponseDTO> {
    return requestJson<PromoteMemoryResponseDTO>(
      `/api/v1/memory/notes/${encodeURIComponent(memoryId)}/promote`,
      auth,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  getWatchlist(auth: AuthContextState): Promise<WatchlistResponseDTO> {
    return requestJson<WatchlistResponseDTO>("/api/v1/memory/watchlist", auth);
  },

  updateWatchlist(
    auth: AuthContextState,
    payload: {
      entity_ids: string[];
      topic_ids: string[];
      source_ids: string[];
    },
  ): Promise<WatchlistResponseDTO> {
    return requestJson<WatchlistResponseDTO>("/api/v1/memory/watchlist", auth, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
  },

  uploadFile(
    auth: AuthContextState,
    payload: {
      file: File;
      subject_canonical_key?: string;
      topic_slug?: string;
      wall_side?: string;
      evidence_acl_groups?: string[];
      notes?: string;
    },
  ): Promise<UploadResponseDTO> {
    const form = new FormData();
    form.append("file", payload.file);
    if (payload.subject_canonical_key) {
      form.append("subject_canonical_key", payload.subject_canonical_key);
    }
    if (payload.topic_slug) {
      form.append("topic_slug", payload.topic_slug);
    }
    if (payload.wall_side) {
      form.append("wall_side", payload.wall_side);
    }
    if (payload.evidence_acl_groups && payload.evidence_acl_groups.length > 0) {
      form.append("evidence_acl_groups", payload.evidence_acl_groups.join(","));
    }
    if (payload.notes) {
      form.append("notes", payload.notes);
    }
    return requestJson<UploadResponseDTO>("/api/v1/uploads", auth, {
      method: "POST",
      body: form,
    });
  },

  getInbox(
    auth: AuthContextState,
  ): Promise<{ items: ClaimDTO[]; total?: number }> {
    return requestJson<{ items: ClaimDTO[]; total?: number }>(
      "/api/v1/inbox",
      auth,
    );
  },

  approveInboxClaim(
    auth: AuthContextState,
    claimId: string,
    payload: { rationale?: string; expected_data_epoch?: number } = {},
  ): Promise<{ claim: ClaimDTO; data_epoch: number }> {
    return requestJson<{ claim: ClaimDTO; data_epoch: number }>(
      `/api/v1/inbox/${encodeURIComponent(claimId)}/approve`,
      auth,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  rejectInboxClaim(
    auth: AuthContextState,
    claimId: string,
    payload: { rationale?: string; expected_data_epoch?: number } = {},
  ): Promise<{ claim: ClaimDTO; data_epoch: number }> {
    return requestJson<{ claim: ClaimDTO; data_epoch: number }>(
      `/api/v1/inbox/${encodeURIComponent(claimId)}/reject`,
      auth,
      {
        method: "POST",
        body: JSON.stringify(payload),
      },
    );
  },

  chatWithAgent(
    auth: AuthContextState,
    payload: {
      message: string;
      viewport: {
        lens?: string;
        visible_entity_ids: string[];
        selected_entity_id?: string | null;
        active_filters?: Record<string, unknown>;
        as_of?: string;
        recorded_as_of?: string;
      };
    },
  ): Promise<AgentChatResponseDTO> {
    return requestJson<AgentChatResponseDTO>("/api/v1/agent/chat", auth, {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
};
