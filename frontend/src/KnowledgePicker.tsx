import { usePickerState, type PickerPresentation } from './usePickerState';
import { t } from './i18n';
import { useEffect, useRef } from 'react';
import { BookOpen, Check, ChevronDown, X } from 'lucide-react';
import { EngineLogo } from './EngineLogo';
import type { KnowledgeBase } from './KnowledgeCenter';

/** 提问框工具栏：显示当前知识库，向上展开选择面板。 */
export function KnowledgePicker({ bases, value, busy, locked, onChange, onManage, embedded=false, onDismiss }: PickerPresentation & {
  bases: KnowledgeBase[];
  value: string[];
  busy: boolean;
  locked: boolean;
  onChange: (ids: string[]) => void;
  onManage: () => void;
}) {
  const [open, setOpen] = usePickerState(embedded,onDismiss);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const selected = value.length === 1 ? bases.find(kb => kb.id === value[0]) : undefined;
  const title = value.length > 1 ? t("已选 {0} 个知识库", value.length) : value.length === 1 ? selected?.name || t("已删除或归档知识库") : t("知识库");
  const unavailable = value.filter(id => !bases.some(kb => kb.id === id));

  useEffect(() => {
    if (!open || embedded) return;
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
  }, [open, embedded]);
  useEffect(() => { if (busy) setOpen(false); }, [busy]);

  const toggle = (id: string) => onChange(value.includes(id) ? value.filter(v => v !== id) : [...value, id]);
  return <div className={'composer-knowledge '+(embedded?'picker-embedded':'')} ref={root} onBlur={event => {
    if (!embedded && !event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
  }}>
    {!embedded&&<button ref={trigger} type="button" className={'knowledge-chip ' + (value.length ? 'has-selection' : '')}
      aria-label={t("选择知识库：") + (value.length ? title : t("未选择"))} aria-haspopup="dialog"
      aria-expanded={open} disabled={busy} title={value.length ? value.map(id => bases.find(kb => kb.id === id)?.name || t("已删除或归档知识库")).join('、') : t("选择知识库")} onClick={() => setOpen(!open)}>
      <>{selected ? <EngineLogo engine={selected.engine} size={18}/> : <BookOpen size={16}/>}</><span>{title}</span><ChevronDown size={13}/>
    </button>}
    {open && <div className="knowledge-popover" role={embedded?'region':'dialog'} aria-label={t("选择知识库")}>
      <header><b>{t("选择知识库")}</b><small>{t("可多选 · 不选时使用普通对话")}</small></header>
      {locked && <p className="knowledge-scope-hint">{t("请先完成或继续当前任务，再调整知识库。")}</p>}
      <div className="knowledge-options">
        {bases.map(kb => <button key={kb.id} type="button" role="checkbox" aria-checked={value.includes(kb.id)}
          disabled={locked || (!value.includes(kb.id) && (kb.status !== 'ready' || value.length >= 50))} onClick={() => toggle(kb.id)}>
          <EngineLogo engine={kb.engine} size={20}/><span><b>{kb.name}</b><small>{kb.status === 'ready' ? (kb.engine === 'pageindex' ? 'PageIndex OSS · ' : 'LlamaIndex · ') + kb.documents + t(" 份文档") : t("待完成索引")}</small></span><span className="kb-selection-box" aria-hidden="true">{value.includes(kb.id) && <Check size={13}/>}</span>
        </button>)}
        {unavailable.map(id => <button key={id} type="button" role="checkbox" aria-checked="true" disabled={locked} onClick={() => toggle(id)}><BookOpen size={20}/><span><b>{t("已删除或归档知识库")}</b><small>{t("点击取消选择")}</small></span><X size={15}/></button>)}
        {!bases.length && <p className="knowledge-scope-hint">{t("还没有知识库，先到知识仓库创建。")}</p>}
      </div>
      <footer><button type="button" disabled={locked || !value.length} onClick={() => onChange([])}><X size={14}/> {t("清空选择")}</button>
        <button type="button" onClick={() => { setOpen(false); onManage(); }}>{t("管理知识库")}</button>
      </footer>
    </div>}
  </div>;
}
