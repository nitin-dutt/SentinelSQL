import { Fragment, useCallback, useEffect, useState } from "react";
import { AuthError, getObservabilitySummary, getTrace, getTraces } from "../api/sentinel";
import type { ObservabilitySummary, TraceDetail, TraceRow } from "../api/sentinel";
import Icon from "./Icon";

// Two series only, so identity is never colour alone: every bar row is also labelled "model" or "gateway".
// The pair was validated with the dataviz palette checker against the earlier dark surface (#141a28); the graphite surface is equally dark.
const KIND_COLOR = { model: "#3987e5", gateway: "#d95926" } as const;
const KIND_LABEL = { model: "Model time (waiting for the language model)", gateway: "Gateway time (SentinelSQL's own code)" } as const;
const MODEL_STAGES = new Set(["planner", "explore", "router", "intent", "generation", "repair"]);
const WINDOWS = [{ hours: 1, label: "Last hour" }, { hours: 24, label: "Last 24 hours" }, { hours: 168, label: "Last 7 days" }];

const ms = (v: number | null | undefined) => (v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);
const usd = (v: number | null | undefined, digits = 4) => (v == null ? "not set" : `$${v.toFixed(digits)}`);

function Tile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="card stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      <div className="stat-note">{note}</div>
    </div>
  );
}

/** One horizontal bar per stage: the bar is the median, the tick is the 95th percentile, both on one shared scale. */
function StageBars({ summary }: { summary: ObservabilitySummary }) {
  const max = Math.max(1, ...summary.stages.map((s) => s.p95_ms ?? s.p50_ms ?? 0));
  if (summary.stages.length === 0) return <p className="muted">No pipeline requests in this window yet</p>;
  return (
    <div>
      <div className="legend">
        {(["model", "gateway"] as const).map((k) => (
          <span key={k}>
            <span className="swatch" style={{ background: KIND_COLOR[k] }} />
            {KIND_LABEL[k]}
          </span>
        ))}
        <span>
          <span style={{ width: 2, height: 12, background: "var(--text)" }} /> 95th percentile
        </span>
      </div>
      <table className="table">
        <thead>
          <tr>
            <th>Stage</th>
            <th style={{ width: "45%" }}>Median (bar) and p95 (tick)</th>
            <th className="num">p50</th>
            <th className="num">p95</th>
            <th className="num">Requests</th>
          </tr>
        </thead>
        <tbody>
          {summary.stages.map((s) => (
            <tr key={s.stage} title={`${s.stage} (${s.kind}): median ${ms(s.p50_ms)}, p95 ${ms(s.p95_ms)}, mean ${ms(s.mean_ms)}, in ${s.n} requests (${s.share_of_requests_pct}%)`}>
              <td className="nowrap">
                {s.stage} <span className="meta">{s.kind}</span>
              </td>
              <td>
                <div className="bar-track">
                  <div style={{ position: "absolute", left: 0, top: 0, bottom: 0, width: `${(100 * (s.p50_ms ?? 0)) / max}%`, minWidth: 2, background: KIND_COLOR[s.kind], borderRadius: 2 }} />
                  {s.p95_ms != null && (
                    <div style={{ position: "absolute", top: -2, bottom: -2, left: `calc(${(100 * s.p95_ms) / max}% - 1px)`, width: 2, background: "var(--text)" }} />
                  )}
                </div>
              </td>
              <td className="num">{ms(s.p50_ms)}</td>
              <td className="num">{ms(s.p95_ms)}</td>
              <td className="num muted">{s.n}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A single request as a waterfall: each span starts where it started in the request and is as long as it took. */
function Waterfall({ trace }: { trace: TraceDetail }) {
  const total = Math.max(1, trace.total_ms);
  return (
    <div className="waterfall">
      {trace.spans.map((s, i) => {
        const kind = MODEL_STAGES.has(s.name) ? "model" : "gateway";
        return (
          <div key={i} className="waterfall-row" title={`${s.name}: started at ${ms(s.start_ms)}, took ${ms(s.ms)}`}>
            <span className={s.depth ? "waterfall-name muted" : "waterfall-name"} style={{ paddingLeft: s.depth * 12 }}>{s.name}</span>
            <div className="waterfall-track">
              <div style={{ position: "absolute", top: 0, bottom: 0, left: `${(100 * s.start_ms) / total}%`, width: `${Math.max(0.4, (100 * s.ms) / total)}%`, background: KIND_COLOR[kind], opacity: s.depth ? 0.6 : 1, borderRadius: 2 }} />
            </div>
            <span className={s.error ? "meta waterfall-ms text-bad" : "meta waterfall-ms"}>{ms(s.ms)}</span>
          </div>
        );
      })}
      {trace.calls.length > 0 && (
        <table className="table" style={{ marginTop: 12 }}>
          <thead>
            <tr>
              <th>Model call</th>
              <th>Model</th>
              <th className="num">Time</th>
              <th className="num">Tokens in / out</th>
              <th className="num">Cost</th>
            </tr>
          </thead>
          <tbody>
            {trace.calls.map((c, i) => (
              <tr key={i}>
                <td>{c.stage}{c.ok ? "" : " (failed)"}</td>
                <td className="muted">{c.model}</td>
                <td className="num">{ms(c.ms)}</td>
                <td className="num">{c.input_tokens} / {c.output_tokens}{c.estimated ? " (estimated)" : ""}</td>
                <td className="num">{usd(c.cost_usd, 6)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function ObservabilityDashboard({ onAuthError }: { onAuthError: () => void }) {
  const [hours, setHours] = useState(24);
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null);
  const [traces, setTraces] = useState<TraceRow[]>([]);
  const [open, setOpen] = useState<TraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fail = useCallback((e: unknown) => {
    if (e instanceof AuthError) onAuthError();
    else setError(e instanceof Error ? e.message : "Could not load observability data");
  }, [onAuthError]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, t] = await Promise.all([getObservabilitySummary(hours), getTraces(30)]);
      setSummary(s);
      setTraces(t);
    } catch (e) {
      fail(e);
    } finally {
      setLoading(false);
    }
  }, [hours, fail]);

  useEffect(() => { load(); }, [load]);

  async function toggle(id: string) {
    if (open?.trace_id === id) return setOpen(null);
    try {
      setOpen(await getTrace(id));
    } catch (e) {
      fail(e);
    }
  }

  const s = summary;
  return (
    <>
      <div className="page-head">
        <div>
          <h2 className="page-title">Observability</h2>
          <p className="lede">Where each request spends its time, tokens and money</p>
        </div>
        <div className="actions">
          <div className="seg" role="group" aria-label="Time window">
            {WINDOWS.map((w) => (
              <button key={w.hours} aria-pressed={hours === w.hours} onClick={() => setHours(w.hours)}>{w.label}</button>
            ))}
          </div>
          <button className="btn" onClick={load} disabled={loading}>
            <Icon name="refresh" />
            {loading ? "Loading…" : "Refresh"}
          </button>
        </div>
      </div>

      {error && (
        <div className="notice notice-bad" role="alert">
          <span className="dot dot-bad" aria-hidden="true" />
          <span>{error}</span>
        </div>
      )}
      {s && !s.enabled && (
        <div className="notice notice-warn" role="status">
          <span className="dot dot-warn" aria-hidden="true" />
          <span>Tracing is switched off (TRACING_ENABLED=false). Only earlier traces are shown.</span>
        </div>
      )}

      {s && (
        <>
          <div className="stats">
            <Tile label="Requests" value={String(s.requests)} note={`${s.cache_hits} from cache (${s.cache_hit_pct ?? 0}%)`} />
            <Tile label="Median time" value={ms(s.total_ms.p50_ms)} note="requests that reached the model" />
            <Tile label="p95 time" value={ms(s.total_ms.p95_ms)} note={`model ${ms(s.model_ms.p95_ms)}, gateway ${ms(s.gateway_ms.p95_ms)}`} />
            <Tile label="Tokens per request" value={s.tokens_per_request ? Math.round(s.tokens_per_request.input + s.tokens_per_request.output).toLocaleString() : "–"}
                  note={s.tokens_per_request ? `${Math.round(s.tokens_per_request.input).toLocaleString()} in, ${Math.round(s.tokens_per_request.output).toLocaleString()} out${s.tokens_estimated ? " (some estimated)" : ""}` : undefined} />
            <Tile label="Cost per 1,000 requests" value={s.cost_per_1000_requests_usd == null ? "not set" : `$${s.cost_per_1000_requests_usd.toFixed(2)}`}
                  note={s.cost_per_1000_requests_usd == null ? "set LLM_PRICE_INPUT_PER_MTOK and LLM_PRICE_OUTPUT_PER_MTOK" : `${s.model_calls_per_request ?? 0} model calls per request`} />
          </div>

          <section className="card">
            <h3 className="card-title">Time per pipeline stage</h3>
            <p className="muted" style={{ margin: "4px 0 16px" }}>
              Cache hits are left out here (median {ms(s.cache_hit_ms.p50_ms)} over {s.cache_hit_ms.n}), so they do not hide the real cost of a request.
            </p>
            <StageBars summary={s} />
          </section>

          {s.models.length > 0 && (
            <section className="card">
              <h3 className="card-title" style={{ marginBottom: 8 }}>Model usage</h3>
              <table className="table">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th className="num">Calls</th>
                    <th className="num">Tokens in</th>
                    <th className="num">Tokens out</th>
                    <th className="num">p50</th>
                    <th className="num">p95</th>
                    <th className="num">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {s.models.map((m) => (
                    <tr key={m.model}>
                      <td className="mono">{m.model}</td>
                      {[m.calls, m.input_tokens, m.output_tokens].map((v, i) => (
                        <td key={i} className="num">{v.toLocaleString()}</td>
                      ))}
                      <td className="num">{ms(m.p50_ms)}</td>
                      <td className="num">{ms(m.p95_ms)}</td>
                      <td className="num">{usd(m.cost_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}
        </>
      )}

      <section className="card">
        <h3 className="card-title" style={{ marginBottom: 8 }}>Recent requests</h3>
        {traces.length === 0 ? (
          <p className="muted">No requests traced yet. Ask a question in the Terminal tab.</p>
        ) : (
          <table className="table table-hover">
            <thead>
              <tr>
                <th>When</th>
                <th>Question</th>
                <th>Channel</th>
                <th>Outcome</th>
                <th className="num">Total</th>
                <th className="num">Model</th>
                <th className="num">Tokens</th>
              </tr>
            </thead>
            <tbody>
              {traces.map((t) => (
                <Fragment key={t.trace_id}>
                  <tr className="row-click" onClick={() => toggle(t.trace_id)} title="Show this request's stages">
                    <td className="meta nowrap">{new Date(t.ts * 1000).toLocaleTimeString()}</td>
                    <td className="truncate" style={{ maxWidth: 380 }}>{t.question || "–"}</td>
                    <td className="muted">{t.channel}</td>
                    <td>{t.status}</td>
                    <td className="num">{ms(t.total_ms)}</td>
                    <td className="num">{ms(t.model_ms)}</td>
                    <td className="num">{(t.input_tokens + t.output_tokens).toLocaleString()}</td>
                  </tr>
                  {open?.trace_id === t.trace_id && (
                    <tr className="row-detail">
                      <td colSpan={7}><Waterfall trace={open} /></td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
