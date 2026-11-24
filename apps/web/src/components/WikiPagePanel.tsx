import { useEffect, useState } from "react";
import { BookOpen, ChevronDown, ChevronRight } from "lucide-react";
import { wikiApi } from "../api/client";
import type { AuthContextState, WallSideName, WikiCitationDTO, WikiPageDTO } from "../types/api";

/**
 * Entity page (ADR-009): a read view generated from the claims you can see, cut by wall side.
 * Every statement carries numbered citations to its source claims. Pages are not edited here:
 * change the claims behind them, or the page template, through change sets.
 */

interface WikiPagePanelProps {
  entityKey: string;
  auth: AuthContextState;
  dataEpoch: number;
}

const SIDE_LABEL: Record<WallSideName, string> = {
  public_side: "Public side",
  private_side: "Private side",
  above_the_wall: "Above the wall",
};

function sourceLabel(c: WikiCitationDTO): string {
  const where = c.evidence_restricted
    ? "evidence restricted"
    : c.evidence_uri
      ? `${c.evidence_uri}${c.span_locator ? ` (${c.span_locator})` : ""}`
      : "no evidence link";
  return `${c.claim_id} · ${c.source_system} · ${where}`;
}

function Cites({ cites, byN }: { cites: number[]; byN: Map<number, WikiCitationDTO> }) {
  return (
    <>
      {cites.map((n) => {
        const c = byN.get(n);
        return (
          <sup
            key={n}
            className="ml-0.5 cursor-help font-mono text-[9px] text-[#38BDF8]"
            title={c ? sourceLabel(c) : undefined}
            data-testid="wiki-cite"
          >
            [{n}]
          </sup>
        );
      })}
    </>
  );
}

function renderCited(text: string, byN: Map<number, WikiCitationDTO>) {
  const parts = text.split(/(\[\d+(?:,\s*\d+)*\])/g);
  return parts.map((part, i) => {
    const m = part.match(/^\[(\d+(?:,\s*\d+)*)\]$/);
    if (!m) return <span key={i}>{part}</span>;
    const nums = m[1].split(/,\s*/).map(Number);
    return <Cites key={i} cites={nums} byN={byN} />;
  });
}

export function WikiPagePanel({ entityKey, auth, dataEpoch }: WikiPagePanelProps) {
  const [open, setOpen] = useState(false);
  const [side, setSide] = useState<WallSideName | undefined>(undefined);
  const [page, setPage] = useState<WikiPageDTO | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    wikiApi
      .page(auth, entityKey, side)
      .then((p) => {
        if (!cancelled) setPage(p);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load the page");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, side, entityKey, auth, dataEpoch]);

  const byN = new Map((page?.citations ?? []).map((c) => [c.n, c] as const));

  return (
    <section className="space-y-2 border-b border-white/[0.08] pb-3.5" data-testid="wiki-page-panel">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-[#94949E] hover:text-[#FAFAFA]"
        aria-expanded={open}
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        <BookOpen className="h-3 w-3" />
        Page
        {page && open && (
          <span className="ml-auto font-mono text-[10px] normal-case tracking-normal text-[#94949E]">
            {SIDE_LABEL[page.side]} · {page.template_id} · {page.narrator}
          </span>
        )}
      </button>

      {open && (
        <div className="space-y-3 text-[12px] leading-relaxed text-[#D4D4D8]">
          <div className="flex gap-1.5">
            {([undefined, "public_side"] as const).map((s) => (
              <button
                key={s ?? "mine"}
                type="button"
                onClick={() => setSide(s)}
                className={`rounded border px-1.5 py-0.5 text-[10px] ${
                  side === s ? "border-[#38BDF8]/50 text-[#38BDF8]" : "border-white/10 text-[#94949E]"
                }`}
              >
                {s ? "Public side" : "My side"}
              </button>
            ))}
          </div>
          {loading && <p className="text-[#94949E]">Building page…</p>}
          {error && <p className="text-[#FB7185]">{error}</p>}
          {page &&
            !loading &&
            page.sections.map((sec) => {
              const empty = !sec.text && !sec.mermaid && sec.statements.length === 0;
              if (empty) return null;
              return (
                <div key={sec.id}>
                  <h4 className="mb-1 text-[11px] font-semibold text-[#FAFAFA]">{sec.title}</h4>
                  {sec.text && <p>{renderCited(sec.text, byN)}</p>}
                  {sec.mermaid && (
                    <pre className="overflow-x-auto rounded bg-[#0A0A0B] p-2 font-mono text-[10px] text-[#A1A1AA]">
                      {sec.mermaid}
                    </pre>
                  )}
                  {sec.statements.length > 0 && (
                    <ul className="space-y-0.5">
                      {sec.statements.map((st, i) => (
                        <li key={i} className={st.status && st.status !== "approved_fact" ? "text-[#FBBF24]" : undefined}>
                          {st.text}
                          <Cites cites={st.cites} byN={byN} />
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              );
            })}
          {page && !loading && page.citations.length > 0 && (
            <details>
              <summary className="cursor-pointer text-[11px] text-[#94949E]">Sources ({page.citations.length})</summary>
              <ol className="mt-1 space-y-0.5 font-mono text-[10px] text-[#94949E]">
                {page.citations.map((c) => (
                  <li key={c.n}>
                    [{c.n}] {sourceLabel(c)}
                  </li>
                ))}
              </ol>
            </details>
          )}
        </div>
      )}
    </section>
  );
}
