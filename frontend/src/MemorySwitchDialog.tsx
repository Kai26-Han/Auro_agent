import { useEffect, useRef, useState } from 'react';
import { t } from './i18n';

export function MemorySwitchDialog({ engine, spaceName, newChat, locked, error, onCancel, onConfirm }: {
  engine: string;
  spaceName: string;
  newChat: boolean;
  locked: boolean;
  error: string;
  onCancel: () => void;
  onConfirm: (freshName?: string) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [choice, setChoice] = useState<'reuse' | 'fresh' | null>(null);
  const [name, setName] = useState(() => `${engine} · ${new Date().toLocaleString()}`);
  useEffect(() => {
    const node = dialog.current;
    node?.showModal();
    return () => node?.close();
  }, []);
  return <dialog ref={dialog} className="modal small-modal memory-switch-dialog"
    aria-labelledby="memory-switch-title" aria-describedby="memory-switch-description"
    onCancel={e => { e.preventDefault(); if (!locked) onCancel(); }}>
    <h2 id="memory-switch-title">{t('切换到 {0}？', engine)}</h2>
    <p id="memory-switch-description">{t('目标引擎中保存的记忆可能已过时。请选择继续沿用，或从新的记忆空间开始整理。')}</p>
    <fieldset className="memory-switch-options" disabled={locked}>
      <legend>{t('如何使用目标引擎的记忆')}</legend>
      <label className={choice === 'reuse' ? 'selected' : ''}>
        <input type="radio" name="memory-switch-choice" value="reuse" checked={choice === 'reuse'} onChange={() => setChoice('reuse')}/>
        <span><b>{t('沿用已有记忆')}</b><small>{t('继续使用“{0}”中保存的记忆，不重新整理旧内容。', spaceName)}</small></span>
      </label>
      <label className={choice === 'fresh' ? 'selected' : ''}>
        <input type="radio" name="memory-switch-choice" value="fresh" checked={choice === 'fresh'} onChange={() => setChoice('fresh')}/>
        <span><b>{t('重新开始整理')}</b><small>{t('保留旧记忆，新建独立空间和对话，从后续对话积累新记忆。')}</small></span>
      </label>
    </fieldset>
    {choice === 'fresh' && <label className="memory-switch-name">{t('新记忆空间名称')}<input value={name} disabled={locked} maxLength={80} onChange={e => setName(e.target.value)}/></label>}
    <p className="field-hint">{t('两套引擎的原有记忆和聊天均保留。不会自动迁移记忆或重新处理历史聊天，是否自动学习仍以你的设置为准。')}</p>
    {choice === 'reuse' && <p>{t(newChat ? '后续提问会使用当前引擎和空间，原会话与草稿保留。' : '本次切换会激活当前会话所属的记忆引擎，继续原会话。')}</p>}
    {error && <p role="alert" className="knowledge-error">{error}</p>}
    <div className="button-row">
      <button type="button" className="secondary" autoFocus disabled={locked} onClick={onCancel}>{t('取消')}</button>
      <button type="button" className="primary" disabled={locked || !choice || (choice === 'fresh' && !name.trim())} onClick={() => onConfirm(choice === 'fresh' ? name.trim() : undefined)}>{t(locked ? '正在切换…' : '确认切换')}</button>
    </div>
  </dialog>;
}
