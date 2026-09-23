import { t } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { Check, ChevronDown } from 'lucide-react';
import { ProviderLogo } from './ProviderLogo';
import type { Profile } from './SettingsPage';
export function ModelPicker({ profiles, value, defaultId, busy, onChange, onManage }: { profiles: Profile[]; value: string; defaultId: string; busy: boolean; onChange: (id: string) => void; onManage: () => void }) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const selected = profiles.find(p => p.id === value);
  useEffect(() => {
    if (!open) return;
    const outside = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const escape = (e: KeyboardEvent) => { if (e.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape); };
  }, [open]);
  useEffect(() => { if (busy) setOpen(false); }, [busy]);
  return <div ref={root} className="composer-knowledge composer-model" onBlur={e => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setOpen(false); }}><button type="button" ref={trigger} className="knowledge-chip" aria-haspopup="dialog" aria-expanded={open} aria-label={t("选择模型：") + (selected?.model || t("未选择"))} title={selected ? selected.name + ' · ' + selected.model : undefined} disabled={busy} onClick={() => setOpen(!open)}><ProviderLogo provider={selected?.provider}/><span>{selected?.model || t("选择模型")}</span><ChevronDown size={13}/></button>
    {open && <div className="knowledge-popover model-popover" role="dialog" aria-label={t("选择对话模型")}><header><b>{t("选择对话模型")}</b><small>{t("本次提问使用的模型")}</small></header><div className="knowledge-options">{profiles.map(p => <button type="button" key={p.id} aria-pressed={value === p.id} onClick={() => { onChange(p.id); setOpen(false); trigger.current?.focus(); }}><ProviderLogo provider={p.provider}/><span><b>{p.model}</b><small>{p.name} · {p.provider}{p.id === defaultId ? ' · ' + t("默认") : ''}</small></span>{value === p.id && <Check size={15}/>}</button>)}</div><footer><button type="button" onClick={() => { setOpen(false); onManage(); }}>{t("管理模型配置")}</button></footer></div>}
  </div>;
}
