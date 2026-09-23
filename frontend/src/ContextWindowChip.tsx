import { useEffect, useRef, useState } from 'react';
import { Info, Settings2 } from 'lucide-react';
import { t } from './i18n';
import type { Profile } from './SettingsPage';
import './context-window.css';

export type ContextWindowSegment = {
  key: string;
  label: string;
  tokens: number;
  percent: number;
};

export type ContextWindowView = {
  schema_version: string;
  measured: boolean;
  estimated: boolean;
  model: string;
  method: string;
  updated?: string | null;
  model_context_window: number;
  working_context_window: number;
  context_window: number;
  input_capacity: number;
  input_used: number | null;
  input_remaining: number;
  output_reserve: number;
  safety_margin: number;
  compaction_trigger_percent: number;
  compaction_target_percent: number;
  usage_percent: number | null;
  degraded?: boolean;
  degraded_message?: string;
  compressed_messages: number;
  segments: ContextWindowSegment[];
};

const SEGMENT_LABELS: Record<string, string> = {
  system_workspace: '系统与工作区',
  tools_capabilities: '工具与能力',
  conversation_history: '对话历史',
  current_request: '当前请求',
  knowledge_results: '知识与检索结果',
  skills_partners: '技能与伙伴',
  memory: '长期记忆',
  other: '其他上下文',
};

export function contextWindowForProfile(profile?: Profile): ContextWindowView | null {
  if (!profile) return null;
  const window = profile.context_window || 256000;
  const modelWindow = Math.max(window,profile.model_context_window||window);
  const output = profile.max_tokens || 8192;
  const safety = Math.max(1024, Math.floor(window / 10));
  return {
    schema_version: 'context-window-v1', measured: false, estimated: false,
    model: profile.model, method: 'utf8_upper_estimate', model_context_window:modelWindow,
    working_context_window:window, context_window: window,
    input_capacity: Math.max(0, window - output - safety), input_used: null,
    input_remaining: Math.max(0, window - output - safety), output_reserve: output,
    safety_margin: safety, compaction_trigger_percent:profile.context_compaction_trigger||75,
    compaction_target_percent:profile.context_compaction_target||50,
    usage_percent: null, compressed_messages: 0, segments: [],
    degraded: false, degraded_message: '',
  };
}

function compactTokens(value: number) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 0 : 1)}m`;
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 100_000 ? 0 : 1)}k`;
  return String(value);
}

function preciseTokens(value: number) {
  return new Intl.NumberFormat(document.documentElement.lang || 'zh-CN').format(value);
}

export function ContextWindowChip({ view, onManage }: {
  view: ContextWindowView | null;
  onManage: () => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); }
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  if (!view || !view.context_window || !view.measured || view.usage_percent == null) return null;
  const percent = view.usage_percent;
  const rounded = Math.round(percent);
  const pressure = percent >= 90 ? 'critical' : percent >= 70 ? 'warning' : 'normal';
  const circumference = 2 * Math.PI * 8;
  const dash = circumference * Math.min(100, percent) / 100;
  const title = t('上下文窗口已使用 {0}%', rounded);
  return <div ref={root} className={`context-window-control ${pressure}`} onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }}>
    <button ref={trigger} className="context-window-trigger" type="button" aria-haspopup="dialog"
      aria-expanded={open} aria-label={title} title={title} onClick={() => setOpen(value => !value)}>
      <svg viewBox="0 0 20 20" aria-hidden="true">
        <circle className="context-ring-track" cx="10" cy="10" r="8"/>
        <circle className="context-ring-value" cx="10" cy="10" r="8"
          strokeDasharray={`${dash} ${circumference - dash}`}/>
      </svg>
      <span>{rounded}%</span>
    </button>
    {open && <section className="context-window-popover" role="dialog" aria-label={t('上下文窗口')}>
      <header><div><b>{t('上下文窗口')}</b><small>{view.model || t('当前模型')}</small></div><span>{compactTokens(view.context_window)}</span></header>
      {view.measured ? <>
        <div className="context-window-summary"><b>{compactTokens(view.input_used || 0)} / {compactTokens(view.input_capacity)}</b><span>{t('输入预算已使用 {0}%', rounded)}</span></div>
        <div className="context-window-bar" aria-hidden="true">{view.segments.map(segment => <i key={segment.key} className={`segment-${segment.key}`} style={{ width: `${Math.max(.8, segment.tokens * 100 / Math.max(1, view.input_capacity))}%` }}/>)}</div>
        <div className="context-window-segments">{view.segments.map(segment => <div key={segment.key}><i className={`segment-${segment.key}`}/><span>{t(SEGMENT_LABELS[segment.key] || segment.label)}</span><b>{compactTokens(segment.tokens)}</b><small>{Math.round(segment.tokens * 100 / Math.max(1, view.input_capacity))}%</small></div>)}</div>
      </> : <div className="context-window-empty"><Info size={15}/><p>{t('完成一次回答后，这里会显示实际组装的上下文占用与构成。')}</p></div>}
      <dl>
        <div><dt>{t('模型上下文上限')}</dt><dd>{preciseTokens(view.model_context_window||view.context_window)}</dd></div>
        <div><dt>{t('工作上下文预算')}</dt><dd>{preciseTokens(view.working_context_window||view.context_window)}</dd></div>
        <div><dt>{t('可用输入预算')}</dt><dd>{preciseTokens(view.input_capacity)}</dd></div>
        <div><dt>{t('预留输出')}</dt><dd>{preciseTokens(view.output_reserve)}</dd></div>
        <div><dt>{t('安全余量')}</dt><dd>{preciseTokens(view.safety_margin)}</dd></div>
        <div><dt>{t('整理水位')}</dt><dd>{view.compaction_trigger_percent||75}% → {view.compaction_target_percent||50}%</dd></div>
        {view.measured && <div><dt>{t('剩余输入预算')}</dt><dd>{preciseTokens(view.input_remaining)}</dd></div>}
      </dl>
      {view.compressed_messages > 0 && <p className="context-window-compression">{t('较早的 {0} 条消息已整理为摘要，聊天原文仍保留。', view.compressed_messages)}</p>}
      {view.degraded && <p className="context-window-degraded">{t(view.degraded_message || '会话整理服务暂时不可用，已使用已有摘要和近期对话继续。')}</p>}
      <footer><span>{t('Token 为本地保守估算，实际计费以模型服务为准。')}</span><button type="button" onClick={() => { setOpen(false); onManage(); }}><Settings2 size={13}/>{t('模型设置')}</button></footer>
    </section>}
  </div>;
}
