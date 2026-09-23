import { useEffect, useState } from 'react';
import { ArrowRight, BookOpen, Plus, Search, Trash2 } from 'lucide-react';
import { EngineLogo } from './EngineLogo';
import type { KnowledgeBase } from './KnowledgeCenter';
import { t } from './i18n';

const PAGE_SIZE = 12;

/** Keep this mounted behind details so returning preserves search and page. */
export function KnowledgeCatalog({ bases, labels, hidden, loading, busy, onSelect, onCreate, onDelete, onPageChange }: {
  bases: KnowledgeBase[];
  labels: Record<string, string>;
  hidden: boolean;
  loading: boolean;
  busy: boolean;
  onSelect: (kb: KnowledgeBase) => void;
  onCreate: () => void;
  onDelete: (kb: KnowledgeBase) => void;
  onPageChange: () => void;
}) {
  const [query, setQuery] = useState('');
  const [engine, setEngine] = useState('all');
  const [page, setPage] = useState(1);
  const needle = query.trim().toLocaleLowerCase();
  const filtered = bases.filter(kb => (engine === 'all' || kb.engine === engine)
    && (!needle || `${kb.name} ${kb.description}`.toLocaleLowerCase().includes(needle)));
  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pageCount);
  const visible = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const documentCount = bases.reduce((sum, kb) => sum + kb.documents, 0);
  const readyCount = bases.filter(kb => kb.status === 'ready').length;
  const attentionCount = bases.filter(kb => ['failed', 'stale'].includes(kb.status)).length;
  useEffect(() => { setPage(p => Math.min(p, pageCount)); }, [pageCount]);

  function reset() { setQuery(''); setEngine('all'); setPage(1); }
  function turn(next: number) { setPage(next); onPageChange(); }

  return <section className="kb-catalog" hidden={hidden} aria-label={t('知识库列表')}>
    {!!bases.length && <div className="kb-overview" aria-label={t('知识仓库概览')}>
      <div className="kb-overview-intro"><strong>{t('所有资料都在本机整理')}</strong><p>{attentionCount ? t('{0} 个知识库可直接使用，{1} 个需要处理。', readyCount, attentionCount) : t('{0} 个知识库已准备好，可以开始提问。', readyCount)}</p></div>
      <div className="kb-overview-stat"><span>{t('知识库')}</span><b>{bases.length}<small>{t('个')}</small></b></div>
      <div className="kb-overview-stat"><span>{t('本地文档')}</span><b>{documentCount}<small>{t('份')}</small></b></div>
      <div className="kb-overview-stat"><span>{t('可直接使用')}</span><b>{readyCount}<small>{t('个')}</small></b></div>
    </div>}
    <div className="kb-catalog-heading">
      <div><h2 tabIndex={-1} data-catalog-heading>{t('我的知识库')}</h2><p>{t('按主题管理资料，选择一个知识库查看文档与索引。')}</p></div>
      <span className="kb-catalog-count">{t('共 {0} 个知识库', bases.length)}</span>
    </div>
    {bases.length > 0 && <div className="kb-catalog-toolbar">
      <div className="kb-catalog-search"><Search size={17}/><input type="search" aria-label={t('搜索知识库')} placeholder={t('搜索名称或说明…')} value={query} onChange={e => { setQuery(e.target.value); setPage(1); }}/></div>
      <select aria-label={t('按知识引擎筛选')} value={engine} onChange={e => { setEngine(e.target.value); setPage(1); }}>
        <option value="all">{t('全部知识引擎')}</option><option value="llamaindex">LlamaIndex</option><option value="pageindex">PageIndex OSS</option>
      </select>
      {(query || engine !== 'all') && <button type="button" className="kb-reset-filter" onClick={reset}>{t('重置筛选')}</button>}
    </div>}
    {loading ? <div className="kb-empty" role="status">{t('正在加载知识库…')}</div> : !bases.length ? <div className="kb-catalog-empty kb-empty">
      <BookOpen size={40}/><h3>{t('给你的资料一个家')}</h3><p>{t('新建知识库 → 添加本地文档 → 构建索引 → 开始提问')}</p>
      <button className="primary" onClick={onCreate}><Plus size={16}/>{t('创建知识库')}</button>
    </div> : !filtered.length ? <div className="kb-catalog-empty kb-empty" role="status">
      <Search size={32}/><h3>{t('没有匹配的知识库')}</h3><p>{t('试试其他关键词，或清除筛选条件。')}</p><button className="secondary" onClick={reset}>{t('重置筛选')}</button>
    </div> : <>
      <div className="kb-grid">
        {visible.map(kb => <article key={kb.id} className="kb-grid-item">
          <button className="kb-grid-card" aria-label={t('打开知识库：{0}', kb.name)} onClick={() => onSelect(kb)}>
            <span className="kb-grid-card-top"><EngineLogo engine={kb.engine} size={30}/><span className={'kb-status ' + kb.status}>{labels[kb.status] || kb.status}</span></span>
            <b className="kb-grid-card-name" title={kb.name}>{kb.name}</b>
            <span className="kb-grid-card-description" title={kb.description}>{kb.description || t('为一个主题积累自己的资料')}</span>
            <span className="kb-grid-card-meta"><span>{kb.engine === 'pageindex' ? 'PageIndex OSS' : 'LlamaIndex'}</span><span>{t('{0} 份文档', kb.documents)}</span></span>
            <span className="kb-grid-card-action">{t('查看文档与设置')}<ArrowRight size={15}/></span>
          </button>
          <button className="kb-grid-delete icon-button" aria-label={t('删除知识库：') + kb.name} title={t('删除知识库')} disabled={busy || kb.status === 'indexing'} onClick={() => onDelete(kb)}><Trash2 size={15}/></button>
        </article>)}
      </div>
      <nav className="kb-pagination" aria-label={t('知识库分页')}>
        <span role="status">{t('显示 {0}–{1} / {2} 个', (currentPage - 1) * PAGE_SIZE + 1, Math.min(currentPage * PAGE_SIZE, filtered.length), filtered.length)}</span>
        {pageCount > 1 && <div><button className="secondary" disabled={currentPage === 1} onClick={() => turn(currentPage - 1)}>{t('上一页')}</button><span>{t('第 {0} / {1} 页', currentPage, pageCount)}</span><button className="secondary" disabled={currentPage === pageCount} onClick={() => turn(currentPage + 1)}>{t('下一页')}</button></div>}
      </nav>
    </>}
  </section>;
}
