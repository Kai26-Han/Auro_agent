import { usePickerState, type PickerPresentation } from './usePickerState';
import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, ChevronDown, Plug, Search, X } from 'lucide-react';
import { t } from './i18n';
import type { ConnectorSummary } from './ConnectorCatalog';
import { connectorSelections, missingConnectorTools, toggleConnectorSelection, MAX_CONNECTOR_TOOLS } from './connectorSelection';
import './tools.css';

type Props = PickerPresentation & {
  connectors: ConnectorSummary[];
  value: string[];
  allowed?: string[];
  locked: boolean;
  onChange: (ids: string[]) => void;
  onManage: () => void;
  onRefresh: () => void;
};

export function ConnectorPicker({ connectors, value, allowed, locked, onChange, onManage, onRefresh, embedded=false, onDismiss }: Props) {
  const [open, setOpen] = usePickerState(embedded, onDismiss);
  const [query, setQuery] = useState('');
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const rows = connectorSelections(connectors, value, allowed);
  const selected = rows.filter(row => row.selectedIds.length);
  const missing = missingConnectorTools(connectors, value);
  const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const filtered = rows.filter(row => words.every(word => row.connector.name.toLocaleLowerCase().includes(word)));
  const title = selected.length === 1 ? selected[0].connector.name : selected.length ? t('已选 {0} 个连接器', selected.length) : missing.length ? t('失效的选择') : t('连接器');
  const refresh = useRef(onRefresh);
  refresh.current = onRefresh;

  useEffect(() => { if (embedded) refresh.current(); }, [embedded]);
  useEffect(() => { if (locked) setOpen(false); }, [locked, setOpen]);
  useEffect(() => {
    if (!open || embedded) return;
    const outside = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', key);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', key); };
  }, [open, embedded, setOpen]);

  function manage() { setOpen(false); onManage(); }

  return <div className={'composer-knowledge connector-picker ' + (embedded ? 'picker-embedded' : '')} ref={root} onBlur={e => {
    if (!embedded && !e.currentTarget.contains(e.relatedTarget as Node)) setOpen(false);
  }}>
    {!embedded && <button ref={trigger} type="button" className={'knowledge-chip ' + (value.length ? 'has-selection' : '')} aria-label={t('选择连接器')} aria-haspopup="dialog" aria-expanded={open} disabled={locked} onClick={() => { if (!open) onRefresh(); setOpen(!open); }}><Plug size={15}/><span>{title}</span><ChevronDown size={13}/></button>}
    {open && <div className="knowledge-popover" role={embedded ? 'region' : 'dialog'} aria-label={t('选择连接器')}>
      <header><b>{t('选择连接器')}</b><small>{t('按连接器启用当前可用的服务能力')}</small></header>
      <label className="connector-picker-search"><Search size={15}/><input type="search" aria-label={t('搜索连接器')} placeholder={t('搜索连接器')} maxLength={200} value={query} onChange={e => setQuery(e.target.value)}/></label>
      <div className="connector-choice-list">
        {missing.length > 0 && <div className="connector-choice stale"><Plug size={18}/><div className="connector-choice-copy"><b>{t('失效的选择')}</b><small>{t('部分服务能力已失效，请清除后重新选择。')}</small></div><button type="button" className="connector-choice-link" disabled={locked} onClick={() => onChange(value.filter(id => !missing.includes(id)))}>{t('清除')}</button></div>}
        {filtered.map(row => {
          const { connector, selectedIds, eligibleIds } = row;
          const checked = selectedIds.length > 0;
          const disconnected = connector.status !== 'connected';
          const overLimit = new Set([...value, ...eligibleIds]).size > MAX_CONNECTOR_TOOLS;
          const partial = checked && (selectedIds.length !== eligibleIds.length || selectedIds.some(id => !eligibleIds.includes(id)));
          const status = disconnected ? t('未连接') : !eligibleIds.length ? t('暂无可用能力') : partial ? t('已选 {0} / {1} 项能力', selectedIds.length, eligibleIds.length) : t('{0} 项服务能力可用', eligibleIds.length);
          const blocked = !checked && (disconnected || !eligibleIds.length || overLimit);
          return <div key={connector.id} className={'connector-choice ' + (checked ? 'is-selected' : '')}>
            <span className="connector-choice-icon"><Plug size={18}/></span>
            <div className="connector-choice-copy"><b title={connector.name}>{connector.name}</b><small>{status}</small>{!checked && !disconnected && eligibleIds.length > 0 && overLimit && <small className="connector-choice-limit">{t('服务能力合计超过 {0} 项，请减少选择。', MAX_CONNECTOR_TOOLS)}</small>}</div>
            {blocked && (disconnected || !eligibleIds.length) ? <button type="button" className="connector-choice-link" disabled={locked} aria-label={t('管理连接器：{0}', connector.name)} onClick={manage}>{t(disconnected ? '去连接' : '去配置')}<ArrowUpRight size={12}/></button> : <button type="button" role="switch" aria-checked={checked} aria-label={t('在对话中使用 {0}', connector.name)} title={partial ? t('保留当前能力范围；关闭后重新开启可使用全部服务能力。') : undefined} disabled={locked || blocked} className="connector-choice-switch" onClick={() => onChange(toggleConnectorSelection(row, value))}><span/></button>}
          </div>;
        })}
        {!filtered.length && <p className="knowledge-scope-hint">{query ? t('没有找到匹配的连接器') : t('还没有配置连接器')}</p>}
      </div>
      <footer><button type="button" disabled={locked || !value.length} onClick={() => onChange([])}><X size={14}/>{t('清空选择')}</button><button type="button" onClick={manage}><ArrowUpRight size={14}/>{t('管理连接器')}</button></footer>
    </div>}
  </div>;
}
