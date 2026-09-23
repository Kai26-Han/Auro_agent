import { Check, ChevronDown, LoaderCircle, Sparkles } from 'lucide-react';
import { useEffect, useState } from 'react';
import { t } from './i18n';

export type ReasoningTrace = {
  run_id?: string;
  status: string;
  mode: 'thinking' | 'exploring' | 'executing';
  duration_seconds?: number;
  started_at?: string;
  steps: string[];
  metrics?: {
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
    total_tokens: number;
    model_calls: number;
    cost: number | null;
    currency: 'CNY' | 'USD';
    cost_partial: boolean;
    cost_status?: 'estimated'|'partial'|'unavailable'|'local';
    cache_write_tokens?: number;
    cost_breakdown?: {category:string;tokens:number;rate_per_million:number;amount:number}[];
    pricing?: {source_url?:string;verified_at?:string;rate_tier?:string;catalog_version?:string;rule_id?:string;reason?:string};
    duration_seconds: number;
  };
};

function elapsed(trace: ReasoningTrace, active: boolean) {
  if (!active || !trace.started_at) return trace.duration_seconds || 0;
  const start = Date.parse(trace.started_at);
  return Number.isFinite(start) ? Math.max(0, Math.round((Date.now() - start) / 1000)) : 0;
}

export function ReasoningPanel({ trace, active = false }: { trace: ReasoningTrace; active?: boolean }) {
  const [seconds, setSeconds] = useState(() => elapsed(trace, active));
  useEffect(() => {
    setSeconds(elapsed(trace, active));
    if (!active) return;
    const timer = window.setInterval(() => setSeconds(elapsed(trace, true)), 1000);
    return () => window.clearInterval(timer);
  }, [active, trace.started_at, trace.duration_seconds]);

  const labels = {
    thinking: t('Auro 思考中'),
    exploring: t('Auro 探索中'),
    executing: t('Auro 执行中'),
  };
  const steps = [...new Set(trace.steps.filter(Boolean))].slice(-5);
  const heading = active ? labels[trace.mode] : t('思考过程');
  const header = <span className="reasoning-head">
    <span className="reasoning-title">{active ? <LoaderCircle className="spin" size={15}/> : <Sparkles size={15}/>}<b>{heading}</b>{seconds > 0 && <small>· {seconds}s</small>}</span>
    {!active && <ChevronDown size={15}/>}
  </span>;
  const body = <div className="reasoning-steps">
    {steps.map((step, index) => <div className={active && index === steps.length - 1 ? 'current' : ''} key={`${step}-${index}`}>
      {active && index === steps.length - 1 ? <span className="reasoning-pulse"/> : <Check size={13}/>}<span>{t(step)}</span>
    </div>)}
  </div>;

  if (active) return <section className="reasoning-panel active" role="status">{header}{body}</section>;
  return <details className="reasoning-panel"><summary>{header}</summary>{body}</details>;
}
