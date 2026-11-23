import { useState } from "react";
import { Archive, ChevronDown, ChevronRight, Hash, Plus } from "lucide-react";
import type { TopicDTO } from "../types/api";

interface TopicSidebarProps {
  topics: TopicDTO[];
  tree: TopicDTO[];
  selectedTopicSlug: string | null;
  onSelectTopic: (slug: string | null) => void;
  onCreateTopic: (payload: {
    slug: string;
    pref_label: string;
    broader_topic_id?: string | null;
  }) => Promise<void>;
  onArchiveTopic: (topicId: string) => Promise<void>;
}

export function TopicSidebar({
  topics,
  tree,
  selectedTopicSlug,
  onSelectTopic,
  onCreateTopic,
  onArchiveTopic,
}: TopicSidebarProps) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({
    "topic:financial": true,
    "topic:non-financial": true,
    "topic:leverage-covenants": true,
    "topic:governance-controls": true,
  });
  const [adding, setAdding] = useState(false);
  const [label, setLabel] = useState("");
  const [parentId, setParentId] = useState<string>("topic:financial");
  const [submitting, setSubmitting] = useState(false);

  const toggleExpand = (id: string) =>
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }));

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = label.trim();
    if (!trimmed) return;
    const slug = trimmed
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "");
    if (!slug) return;
    setSubmitting(true);
    try {
      await onCreateTopic({
        slug,
        pref_label: trimmed,
        broader_topic_id: parentId || null,
      });
      setLabel("");
      setAdding(false);
    } finally {
      setSubmitting(false);
    }
  };

  const renderNode = (node: TopicDTO, depth: number) => {
    const children = (node.children || []).filter(
      (c) => c.status !== "archived" && c.status !== "merged",
    );
    const hasChildren = children.length > 0;
    const isExpanded = expanded[node.topic_id] ?? depth === 0;
    const active = selectedTopicSlug === node.slug;
    const count =
      node.item_count ??
      (node.entity_count ?? 0) + (node.claim_count ?? 0);

    return (
      <div key={node.topic_id}>
        <div
          className={`group flex items-center justify-between gap-1 rounded-md py-1 pr-1.5 text-xs transition-colors ${
            active
              ? "bg-[#18181B] text-[#FAFAFA]"
              : "text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
          }`}
          style={{ paddingLeft: `${depth * 12 + 6}px` }}
        >
          <div className="flex min-w-0 flex-1 items-center gap-1">
            {hasChildren ? (
              <button
                type="button"
                aria-label={`Toggle ${node.pref_label}`}
                onClick={() => toggleExpand(node.topic_id)}
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-[#94949E] hover:text-[#FAFAFA]"
              >
                {isExpanded ? (
                  <ChevronDown className="h-3 w-3" />
                ) : (
                  <ChevronRight className="h-3 w-3" />
                )}
              </button>
            ) : (
              <Hash className="ml-1.5 h-3 w-3 shrink-0 text-[#94949E]" />
            )}

            <button
              type="button"
              data-testid={`topic-node-${node.slug}`}
              onClick={() =>
                onSelectTopic(active ? null : node.slug)
              }
              className="min-h-6 flex-1 truncate text-left font-medium"
            >
              {node.pref_label}
            </button>
          </div>

          <div className="flex shrink-0 items-center gap-1">
            {node.topic_kind === "emergent_candidate" && (
              <button
                type="button"
                aria-label={`Archive ${node.pref_label}`}
                onClick={() => onArchiveTopic(node.topic_id)}
                className="hidden h-6 w-6 items-center justify-center rounded text-[#94949E] hover:text-[#FAFAFA] group-hover:inline-flex"
              >
                <Archive className="h-3 w-3" />
              </button>
            )}
            <span className="font-mono text-[11px] tabular-nums text-[#94949E]">
              {count}
            </span>
          </div>
        </div>

        {hasChildren && isExpanded && (
          <div>{children.map((child) => renderNode(child, depth + 1))}</div>
        )}
      </div>
    );
  };

  const roots =
    tree.length > 0
      ? tree.filter((t) => t.status !== "archived" && t.status !== "merged")
      : topics.filter(
          (t) =>
            !t.broader_topic_id &&
            t.status !== "archived" &&
            t.status !== "merged",
        );

  return (
    <section
      aria-label="Topics"
      data-testid="topic-rail"
      className="flex w-60 shrink-0 flex-col border-r border-white/[0.08] bg-[#0A0A0B]"
    >
      <div className="flex h-10 items-center justify-between border-b border-white/[0.08] px-3">
        <span className="text-xs font-medium text-[#A1A1AA]">Topics</span>
        <button
          type="button"
          data-testid="add-topic-trigger"
          onClick={() => setAdding((v) => !v)}
          className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-[#A1A1AA] hover:bg-white/[0.06] hover:text-[#FAFAFA]"
        >
          <Plus className="h-3 w-3" />
          <span>Add</span>
        </button>
      </div>

      {adding && (
        <form
          onSubmit={handleSubmit}
          data-testid="add-topic-form"
          className="space-y-1.5 border-b border-white/[0.08] bg-[#111113] p-2.5"
        >
          <input
            type="text"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Topic label"
            aria-label="New topic label"
            data-testid="add-topic-input"
            className="w-full rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA] placeholder-[#94949E]"
          />
          <select
            value={parentId}
            onChange={(e) => setParentId(e.target.value)}
            aria-label="Parent topic"
            className="w-full rounded border border-white/[0.12] bg-[#0A0A0B] px-2 py-1 text-xs text-[#FAFAFA]"
          >
            {topics
              .filter((t) => t.status === "active")
              .map((t) => (
                <option key={t.topic_id} value={t.topic_id}>
                  {t.pref_label}
                </option>
              ))}
          </select>
          <div className="flex justify-end gap-1.5">
            <button
              type="button"
              onClick={() => setAdding(false)}
              className="rounded px-2 py-1 text-[11px] text-[#A1A1AA] hover:text-[#FAFAFA]"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={submitting || !label.trim()}
              data-testid="add-topic-submit"
              className="rounded bg-[#FAFAFA] px-2.5 py-1 text-[11px] font-medium text-[#0A0A0B] hover:bg-[#E4E4E7] disabled:opacity-40"
            >
              Create
            </button>
          </div>
        </form>
      )}

      <div className="flex-1 overflow-y-auto p-2">
        <button
          type="button"
          data-testid="topic-node-all"
          onClick={() => onSelectTopic(null)}
          className={`mb-1 flex w-full items-center justify-between rounded-md px-2 py-1 text-xs font-medium transition-colors ${
            selectedTopicSlug === null
              ? "bg-[#18181B] text-[#FAFAFA]"
              : "text-[#A1A1AA] hover:bg-white/[0.04] hover:text-[#FAFAFA]"
          }`}
        >
          <span>All Topics</span>
        </button>

        <div className="space-y-0.5">{roots.map((r) => renderNode(r, 0))}</div>
      </div>
    </section>
  );
}
