import { t, indexProgress } from './i18n';
import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, BookOpen, Cpu, FileText, Plus, RefreshCw, Search, Trash2, Upload } from 'lucide-react';
import { api } from './api';
import type { Preferences } from './SettingsPage';
import './knowledge.css';
import { EngineLogo } from './EngineLogo';
import { PageIndexOutline } from './PageIndexOutline';
import { KnowledgeCatalog } from './KnowledgeCatalog';

type Embedding = { base_url: string; model: string; api_key_set?: boolean; api_key?: string; clear_key?: boolean };
export type KnowledgeBase = { id: string; name: string; description: string; engine: 'llamaindex' | 'pageindex'; pageindex_mode: 'flash' | 'standard'; status: string; progress: string; documents: number; embedding: Embedding; dimension: number | null };
type Form = { use_default_embedding: boolean; engine: 'llamaindex' | 'pageindex'; pageindex_mode: 'flash' | 'standard'; name: string; description: string; embedding: Embedding };
type Engine = { retrieval_profile: string; vector_index_type: string; reranker_model: string; reranker_available?: boolean; [key: string]: string | number | boolean | undefined };
type Doc = { id: string; name: string; chunks: number; warnings: string[] };
type Hit = { id: string; title: string; page: number | null; paragraph: number; text: string };
const path = '/knowledge/bases';
const blank = (): Form => ({ use_default_embedding: true, engine: 'llamaindex', pageindex_mode: 'flash', name: '', description: '', embedding: { base_url: 'http://127.0.0.1:11434/v1', model: 'qwen3-embedding:0.6b', api_key: '' } });

export function KnowledgeCenter({ onClose, onChat, onChanged }: { onClose: () => void; onChat: (kb: KnowledgeBase) => void; onChanged: () => void }) {
  const labels: Record<string, string> = { empty: t("待添加资料"), stale: t("待重建"), indexing: t("构建中"), ready: t("就绪"), failed: t("需重试") };
  const [tab, setTab] = useState<'bases' | 'engine'>('bases');
  const [bases, setBases] = useState<KnowledgeBase[]>([]);
  const [id, setId] = useState<string | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [creating, setCreating] = useState(false);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [engine, setEngine] = useState<Engine | null>(null);
  const [defaultEmbedding, setDefaultEmbedding] = useState<Embedding | null>(null);
  const [engineTab, setEngineTab] = useState<'llamaindex' | 'pageindex'>('llamaindex');
  const [pageIndex, setPageIndex] = useState<{ model: string; configured: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [question, setQuestion] = useState('');
  const [hits, setHits] = useState<Hit[] | null>(null);
  const [detailTab, setDetailTab] = useState<'documents' | 'search' | 'settings'>('documents');
  const [confirm, setConfirm] = useState<{ title: string; action: () => Promise<unknown> } | null>(null);
  const main = useRef<HTMLElement>(null);
  const catalogScroll = useRef(0);
  const selectionRequest = useRef(0);
  const [loading, setLoading] = useState(true);
  const [loadingDocs, setLoadingDocs] = useState(false);
  const selected = bases.find(b => b.id === id);
  const indexing = selected?.status === 'indexing';
  async function refresh() { try { setBases(await api<KnowledgeBase[]>(path)); onChanged(); } finally { setLoading(false); } }
  async function run(action: () => Promise<unknown>, message = '') {
    setBusy(true); setError(''); setNotice('');
    try { await action(); await refresh(); if (message) setNotice(message); }
    catch (e) { setError((e as Error).message); }
    finally { await refresh().catch(() => {}); setBusy(false); }
  }
  useEffect(() => { api<Preferences>('/settings').then(p => { const model = p.profiles.find(m => m.id === p.default_embedding_id); if (model) setDefaultEmbedding(model); }).catch(e => setError(e.message)); api<{ model: string; configured: boolean }>('/knowledge/pageindex').then(setPageIndex).catch(e => setError(e.message)); refresh().catch(e => setError(e.message)); api<Engine>('/knowledge/engine').then(setEngine).catch(e => setError(e.message)); }, []);
  useEffect(() => {
    if (!bases.some(b => b.status === 'indexing')) return;
    const timer = setInterval(() => refresh().catch(e => setError(e.message)), 1600);
    return () => clearInterval(timer);
  }, [bases.some(b => b.status === 'indexing')]);
  function openCreate() {
    if (!form) catalogScroll.current = main.current?.scrollTop || 0;
    selectionRequest.current++; setLoadingDocs(false); setDocs([]);
    setId(null); setCreating(true); setForm(blank()); setDetailTab('settings'); setError(''); setNotice(''); setHits(null);
  }
  function backToCatalog() {
    selectionRequest.current++; setLoadingDocs(false);
    setId(null); setForm(null); setCreating(false); setDocs([]); setHits(null); setError('');
    requestAnimationFrame(() => {
      main.current?.querySelector<HTMLElement>('[data-catalog-heading]')?.focus({ preventScroll: true });
      main.current?.scrollTo({ top: catalogScroll.current });
    });
  }
  useEffect(() => {
    if (form) {
      main.current?.scrollTo({ top: 0 });
      main.current?.querySelector<HTMLElement>('[data-detail-heading]')?.focus({ preventScroll: true });
    }
  }, [id, creating]);
  async function select(kb: KnowledgeBase) {
    if (!form) catalogScroll.current = main.current?.scrollTop || 0;
    const request = ++selectionRequest.current;
    setDocs([]); setLoadingDocs(true);
    setId(kb.id); setCreating(false); setDetailTab('documents'); setError(''); setNotice(''); setHits(null); setQuestion('');
    setForm({ use_default_embedding: false, engine: kb.engine, pageindex_mode: kb.pageindex_mode, name: kb.name, description: kb.description, embedding: { ...kb.embedding, api_key: '' } });
    try {
      const documents = await api<Doc[]>(`${path}/${kb.id}/documents`);
      if (request === selectionRequest.current) setDocs(documents);
    } catch (e) {
      if (request === selectionRequest.current) setError((e as Error).message);
    } finally {
      if (request === selectionRequest.current) setLoadingDocs(false);
    }
  }
  function requestDelete(kb: KnowledgeBase) {
    setConfirm({ title: t("删除「") + kb.name + t("」？该库的本地文档与索引将被清理，无法恢复。历史对话和引用快照仍保留。"), action: async () => {
      await api(`${path}/${kb.id}`, { method: 'DELETE' });
      if (id === kb.id) backToCatalog();
      setNotice(t("知识库已删除，历史引用仍可查看。"));
    } });
  }
  function editEmbedding(key: keyof Embedding, value: string | boolean) {
    setForm(f => f && ({ ...f, embedding: { ...f.embedding, [key]: value } }));
  }
  async function save() {
    if (!form) return;
    const { api_key_set: _set, ...embedding } = form.embedding;
    const kb = await api<KnowledgeBase>(path + (creating ? '' : '/' + id), { method: creating ? 'POST' : 'PUT', body: JSON.stringify({ ...form, embedding }) });
    await select(kb);
  }
  async function upload(files: FileList | null) {
    if (!files?.length || !id) return;
    const problems: string[] = [];
    for (const file of Array.from(files)) {
      try {
        if (file.size > 50 * 1024 * 1024) throw new Error(t("单个文件最多 50 MiB"));
        const data = new FormData(); data.append('file', file);
        const result = await api<{ warnings: string[] }>(`${path}/${id}/documents`, { method: 'POST', body: data });
        if (result.warnings.length) problems.push(file.name + '：' + result.warnings.join('；'));
      } catch (e) { problems.push(file.name + '：' + (e as Error).message); }
    }
    setDocs(await api<Doc[]>(`${path}/${id}/documents`)); setHits(null);
    if (problems.length) throw new Error(problems.join('\n'));
  }
  function number(key: string, title: string, min: number, max: number, disabled = false) {
    return <label key={key}>{title}<input type="number" min={min} max={max} disabled={disabled} value={Number(engine?.[key] ?? 0)} onChange={e => setEngine(v => v && ({ ...v, [key]: Number(e.target.value) }))}/></label>;
  }
  return <main className="knowledge-center" ref={main}>
    <header className="knowledge-header"><div><button className="back-link" onClick={onClose}><ArrowLeft size={16}/> {t("回到对话")}</button><span className="eyebrow">YOUR KNOWLEDGE, CONNECTED</span><h1>{t("知识仓库")}</h1><p>{t("让分散的资料，成为可以随时提问的知识。")}</p></div>{tab === 'bases' && !form && <button className="primary" onClick={openCreate}><Plus size={16}/> {t("新建知识库")}</button>}</header>
    <div className="knowledge-tabs" role="tablist" aria-label={t("知识仓库")}><button role="tab" aria-selected={tab === 'bases'} onClick={() => setTab('bases')}><BookOpen size={17}/> {t("知识库")}<span>{bases.length}</span></button><button role="tab" aria-selected={tab === 'engine'} onClick={() => setTab('engine')}><Cpu size={17}/> {t("知识引擎")}<span>2</span></button></div>
    {error && <div role="alert" className="knowledge-error">{error}<button onClick={() => setError('')}>{t("关闭")}</button></div>}
    {notice && <div role="status" className="knowledge-notice">{notice}</div>}
    {tab === 'engine' ? <section className="engine-page">
      <header className="engine-group-heading"><h2>{t("本地引擎")}</h2><p>{t("选择引擎，查看与调整配置。")}</p></header>
      <div className="engine-selector" aria-label={t("选择本地知识引擎")}>{(['llamaindex', 'pageindex'] as const).map(value => <button key={value} id={'engine-' + value} type="button" aria-pressed={engineTab === value} aria-controls="engine-config" onClick={() => setEngineTab(value)}>
        <span className="engine-card-heading"><EngineLogo engine={value} size={32}/><b>{value === 'llamaindex' ? 'LlamaIndex' : 'PageIndex OSS'}</b><span className="kb-status ready">{t("已安装")}</span></span>
        <span className="engine-card-description">{value === 'llamaindex' ? t("本地分块与向量索引 · FAISS 存储 · 可选 BM25 混合检索") : t("本地文档树 · 按页阅读 · 无需 Embedding")}</span>
        <span className="engine-card-action">{engineTab === value ? t("当前选择") : t("查看配置")}<span aria-hidden="true">{engineTab === value ? '✓' : '→'}</span></span>
      </button>)}</div>
      <div id="engine-config" className="engine-config" role="region" aria-labelledby={'engine-' + engineTab}>
      {engineTab === 'pageindex' ? <><h3>{t("沿用对话模型")}</h3><p className="field-hint">{t("当前模型：")}{pageIndex?.model || t("加载中…")} · {pageIndex?.configured ? t("已配置") : t("尚未配置")}{t("。建索引时生成摘要，对话时根据目录选择页面；模型请求会发送至现有模型服务。")}</p><h3>{t("索引模式")}</h3><div className="option-cards"><label><b>{t("Flash · 默认")}</b><small>{t("从 PDF 版式提取目录结构，由模型生成摘要，适合先快速试用。")}</small></label><label><b>Standard</b><small>{t("用模型分析文档结构，构建通常更慢、模型调用更多。")}</small></label></div><p className="field-hint">{t("模式在每个知识库的设置中选择。仅支持带文本层的 PDF；每份最多 50 MiB、500 页。模型沿用设置中的默认语言模型，无需 PageIndex Cloud 密钥。")}</p></> : <>{engine && <form onSubmit={e => { e.preventDefault(); run(async () => { const { reranker_available: available, ...data } = engine; const result = await api<Engine>('/knowledge/engine', { method: 'PUT', body: JSON.stringify(data) }); setEngine({ ...result, reranker_available: available }); }, t("引擎配置已保存。分块或索引类型变化后，请重建已有知识库。")); }}>
      <h3>{t("检索与召回")}</h3><div className="option-cards">{[['hybrid', t("混合检索"), t("向量理解含义，BM25 匹配关键词，再融合排序。")], ['vector', t("向量检索"), t("按语义相似度召回资料。")]].map(([value, title, detail]) => <label key={value} className={engine.retrieval_profile === value ? 'chosen' : ''}><input type="radio" name="profile" checked={engine.retrieval_profile === value} onChange={() => setEngine({ ...engine, retrieval_profile: value })}/><b>{title}</b><small>{detail}</small></label>)}</div>
      <div className="fields three">{number('top_k', t("每次返回片段数（Top K）"), 1, 50)}{number('vector_top_k_multiplier', t("向量候选倍数"), 1, 10, engine.retrieval_profile !== 'hybrid')}{number('bm25_top_k_multiplier', t("关键词候选倍数"), 1, 10, engine.retrieval_profile !== 'hybrid')}</div>
      <h3>{t("分块设置")}</h3><p className="field-hint">{t("按 token 分块。以下配置改变后，已有知识库会提示重建。")}</p><div className="fields">{number('chunk_size', t("分块大小"), 64, 4096)}{number('chunk_overlap', t("重叠大小"), 0, 1024)}</div>
      <h3>{t("向量索引")}</h3><div className="fields"><label>{t("索引类型")}<select value={engine.vector_index_type} onChange={e => setEngine({ ...engine, vector_index_type: e.target.value })}><option value="flat">{t("Flat · 精确检索，适合个人资料库")}</option><option value="hnsw">{t("HNSW · 近似检索，适合较大资料库")}</option></select></label></div>{engine.vector_index_type === 'hnsw' && <div className="fields three">{number('hnsw_m', t("邻接数 M"), 4, 128)}{number('hnsw_ef_construction', t("构建候选数"), 8, 1000)}{number('hnsw_ef_search', t("搜索候选数"), 1, 1000)}</div>}
      <details className="advanced"><summary>{t("高级设置 · 可选重排")}</summary><p className="field-hint">{t("重排模型留空表示关闭。启用需安装可选依赖 uv sync --extra rerank，首次查询会下载模型。当前")}{engine.reranker_available ? t("已安装") : t("未安装")}。</p><div className="fields"><label>{t("重排模型")}<input disabled={!engine.reranker_available} value={engine.reranker_model} placeholder={t("例如 BAAI/bge-reranker-base")} onChange={e => setEngine({ ...engine, reranker_model: e.target.value })}/></label>{number('rerank_top_k', t("重排候选数"), 1, 100, !engine.reranker_available)}</div></details>
      <p className="field-hint">{t("当前解析本地 Markdown、TXT 和文本 PDF。图片描述和 OCR 暂未接入。")}</p><button className="primary" disabled={busy}>{t("保存引擎配置")}</button>
    </form>}</>}</div></section> : <>
      <KnowledgeCatalog bases={bases} labels={labels} hidden={!!form} loading={loading} busy={busy}
        onSelect={kb => { void select(kb); }} onCreate={openCreate} onDelete={requestDelete}
        onPageChange={() => main.current?.scrollTo({ top: 0, behavior: 'smooth' })}/>
      {form && <div className={'kb-detail-view' + (creating ? ' creating' : '')}>
        <button className="back-link kb-catalog-back" disabled={busy} onClick={backToCatalog}><ArrowLeft size={16}/> {t("返回知识库列表")}</button>
        <section className="kb-detail">
          <div className="kb-detail-heading"><div className="kb-detail-title"><EngineLogo engine={form.engine} size={38}/><div><span className="eyebrow">KNOWLEDGE BASE</span><h2 tabIndex={-1} data-detail-heading>{creating ? t("新建知识库") : selected?.name}</h2>{!creating && <p>{selected?.description || t("为一个主题积累自己的资料")}</p>}</div></div>{!creating && <button className="primary" disabled={selected?.status !== 'ready' || busy} onClick={() => selected && onChat(selected)}>{t("与知识库对话")} →</button>}</div>

          {!creating && <><div className="kb-health-grid"><div className={'kb-health-main ' + selected?.status}><div><span className={'kb-status ' + selected?.status}>{labels[selected?.status || '']}</span><strong>{selected?.status === 'ready' ? t("可以开始提问") : t("需要处理索引")}</strong></div><p>{indexProgress(selected?.progress || '')}</p></div><div className="kb-health-stat"><span>{t("文档")}</span><b>{docs.length} {t("份")}</b></div><div className="kb-health-stat"><span>{t("向量维度")}</span><b>{selected?.dimension || '—'}</b></div><div className="kb-health-stat"><span>{t("知识引擎")}</span><b>{form.engine === 'pageindex' ? 'PageIndex OSS' : 'LlamaIndex'}</b></div></div>
            <div className="kb-detail-tabs" role="tablist" aria-label={t("知识库管理")}><button role="tab" aria-selected={detailTab === 'documents'} onClick={() => setDetailTab('documents')}>{t("文档")}</button><button role="tab" aria-selected={detailTab === 'search'} onClick={() => setDetailTab('search')}>{t("检索测试")}</button><button role="tab" aria-selected={detailTab === 'settings'} onClick={() => setDetailTab('settings')}>{t("设置")}</button></div></>}

          {(creating || detailTab === 'settings') && <form onSubmit={e => { e.preventDefault(); run(save, creating ? t("知识库已创建，请添加文档并构建索引。") : t("知识库设置已保存。")); }} className="kb-form kb-settings-panel"><div className="kb-panel-heading"><div><h3>{creating ? t("建立一个新的知识库") : t("知识库设置")}</h3><p>{creating ? t("填写资料主题并选择适合的知识引擎。") : t("修改名称、说明和该知识库使用的模型配置。")}</p></div></div><div className="kb-name-field"><label>{t("知识库名称")}<input required maxLength={80} value={form.name} onChange={e => setForm({ ...form, name: e.target.value })}/></label></div><label>{t("说明")}<textarea rows={2} maxLength={1000} value={form.description} onChange={e => setForm({ ...form, description: e.target.value })} placeholder={t("这个知识库用来积累哪些资料？")}/></label>
            <fieldset className="kb-engine-field" disabled={!creating || busy}><legend>{t("知识引擎")}</legend><div className="kb-engine-options">{(['llamaindex', 'pageindex'] as const).map(value => <label key={value} className={'kb-engine-option ' + (form.engine === value ? 'chosen' : '')}><input type="radio" name="knowledge-engine" value={value} checked={form.engine === value} onChange={() => setForm({ ...form, engine: value })}/><EngineLogo engine={value} size={28}/><span><b>{value === 'llamaindex' ? 'LlamaIndex' : 'PageIndex OSS'}</b><small>{value === 'llamaindex' ? t("向量与混合检索 · PDF / MD / TXT") : t("目录树与页码阅读 · 文本 PDF")}</small></span></label>)}</div></fieldset>
            {form.engine === 'pageindex' ? <><h3>PageIndex OSS</h3><p className="field-hint">{t("沿用对话模型")}{pageIndex?.model}{t("；无需 Embedding。文档与索引保存在本机，生成摘要时会调用现有模型服务。")}</p><label>{t("索引模式")}<select value={form.pageindex_mode} disabled={busy || indexing} onChange={e => setForm({ ...form, pageindex_mode: e.target.value as Form['pageindex_mode'] })}><option value="flash">{t("Flash · 版式目录 + 模型摘要（默认）")}</option><option value="standard">{t("Standard · 模型分析文档结构")}</option></select></label><p className="field-hint">{t("更改模式后需要重建索引；Standard 通常耗时更长。")}</p></> : <><h3>{t("Embedding 模型")}</h3><label className="check-label"><input type="checkbox" checked={form.use_default_embedding} onChange={e => setForm({ ...form, use_default_embedding: e.target.checked })}/> {t("使用设置中的默认嵌入模型")}</label>{form.use_default_embedding ? <p className="field-hint">{defaultEmbedding?.model || t("加载中…")} · {defaultEmbedding?.base_url}<br/>{t("保存时复制默认配置；以后更改全局设置不会自动替换本库模型。")}</p> : <><p className="field-hint">{t("嵌入模型将资料转换为向量，对话模型负责回答。也可以单独配置本库的兼容接口。")}</p><div className="fields"><label>Base URL<input required type="url" value={form.embedding.base_url} onChange={e => editEmbedding('base_url', e.target.value)}/></label><label>{t("模型名称")}<input required value={form.embedding.model} onChange={e => editEmbedding('model', e.target.value)}/></label></div><details className="advanced"><summary>{t("兼容接口密钥（本机 Ollama 无需填写）")}</summary><label>API Key<input type="password" autoComplete="new-password" value={form.embedding.api_key || ''} placeholder={form.embedding.api_key_set ? t("已保存，留空保留现有密钥") : t("本机服务可留空")} onChange={e => editEmbedding('api_key', e.target.value)}/></label>{form.embedding.api_key_set && <label className="check-label"><input type="checkbox" checked={!!form.embedding.clear_key} onChange={e => editEmbedding('clear_key', e.target.checked)}/> {t("清除已保存的密钥")}</label>}</details></>}</>}
            <div className="button-row"><button className="primary" disabled={busy || indexing}>{creating ? t("创建知识库") : t("保存知识库设置")}</button>{!creating && form.engine === 'llamaindex' && <button type="button" className="secondary" disabled={busy || indexing} onClick={() => run(async () => { const r = await api<{ dimension: number }>(`${path}/${id}/probe`, { method: 'POST' }); setNotice(t("已保存的 Embedding 配置连接成功 · {0} 维", r.dimension)); })}>{t("测试已保存配置")}</button>}</div>
            {!creating && <div className="kb-danger-zone"><div><b>{t("删除知识库")}</b><small>{t("本地文档和索引会被清理，历史引用快照仍保留。")}</small></div><button type="button" disabled={busy || indexing} onClick={() => selected && requestDelete(selected)}><Trash2 size={14}/> {t("删除知识库")}</button></div>}
          </form>}

          {!creating && detailTab === 'documents' && <div className="kb-detail-workspace"><section className="kb-documents-panel"><div className="kb-panel-heading"><div><h3>{t("本地文档")} <span>{docs.length}</span></h3><p>{t("添加、替换或移出当前知识库的资料。")}</p></div><label className="secondary kb-upload-button"><Upload size={15}/>{busy ? t("正在处理…") : t("添加文档")}<input aria-label={t("添加知识库本地文档")} type="file" accept={form.engine === 'pageindex' ? '.pdf' : '.md,.txt,.pdf'} multiple disabled={busy || indexing} onChange={e => { const files = e.target.files; run(() => upload(files), t("文档已导入，请构建索引。")); e.target.value = ''; }}/></label></div><label className="kb-upload"><Upload size={22}/><b>{t("拖放文件到这里，或点击选择")}</b><small>{form.engine === 'pageindex' ? t("文本 PDF；同名文件更新版本；每份不超过 50 MiB") : t("Markdown、UTF-8 TXT、文本 PDF；同名文件更新版本；每份不超过 50 MiB")}</small><input aria-label={t("添加知识库本地文档")} type="file" accept={form.engine === 'pageindex' ? '.pdf' : '.md,.txt,.pdf'} multiple disabled={busy || indexing} onChange={e => { const files = e.target.files; run(() => upload(files), t("文档已导入，请构建索引。")); e.target.value = ''; }}/></label>{loadingDocs && <p className="field-hint" role="status">{t("正在加载文档…")}</p>}<div className="kb-documents">{docs.map(doc => <div key={doc.id}><FileText size={18}/><span><b>{doc.name}</b><small>{doc.warnings.join('；') || t("本地文档 · 已保存原件")}</small></span><em>{doc.warnings.length ? t("需要检查") : t("已保存")}</em><button className="icon-button" aria-label={t("移出 ") + doc.name} disabled={busy || indexing} onClick={() => setConfirm({ title: t("移出「") + doc.name + t("」？历史引用仍保留，当前索引需要重建。"), action: async () => { await api(`${path}/${id}/documents/${doc.id}`, { method: 'DELETE' }); setDocs(await api<Doc[]>(`${path}/${id}/documents`)); setHits(null); } })}><Trash2 size={16}/></button></div>)}</div>{form.engine === 'pageindex' && <PageIndexOutline key={id + (selected?.status || '')} kid={id!} docs={docs} ready={selected?.status === 'ready'}/>}</section><aside className="kb-index-panel"><div className={'kb-index-card ' + selected?.status}><div><span className={'kb-status ' + selected?.status}>{labels[selected?.status || '']}</span><b>{selected?.status === 'ready' ? t("索引已是最新") : t("索引需要处理")}</b></div><p>{indexProgress(selected?.progress || '')}</p><button className="secondary" disabled={busy || indexing || loadingDocs || !docs.length} onClick={() => run(() => api(`${path}/${id}/index`, { method: 'POST' }))}><RefreshCw size={14}/> {indexing ? t("正在构建…") : t("构建 / 重建索引")}</button></div><div className="kb-index-info"><h3>{t("索引信息")}</h3><p><span>{t("知识引擎")}</span><b>{form.engine === 'pageindex' ? 'PageIndex OSS' : 'LlamaIndex'}</b></p><p><span>{t("文档")}</span><b>{docs.length} {t("份")}</b></p>{form.engine === 'llamaindex' && <><p><span>{t("嵌入模型")}</span><b>{selected?.embedding.model}</b></p><p><span>{t("向量维度")}</span><b>{selected?.dimension || '—'}</b></p></>}</div><button className="secondary kb-test-shortcut" onClick={() => setDetailTab('search')}><Search size={14}/>{t("测试检索质量")}</button></aside></div>}

          {!creating && detailTab === 'search' && <section className="kb-retrieval-panel"><div className="kb-panel-heading"><div><h3>{t("检索测试")}</h3><p>{t("只查看知识引擎找到的原文，不调用对话模型。")}</p></div></div>{form.engine === 'pageindex' ? <PageIndexOutline key={id + (selected?.status || '') + '-search'} kid={id!} docs={docs} ready={selected?.status === 'ready'}/> : <><form onSubmit={e => { e.preventDefault(); run(async () => setHits(await api<Hit[]>(`${path}/${id}/search?q=${encodeURIComponent(question)}`))); }}><input aria-label={t("检索测试问题")} maxLength={1000} placeholder={t("输入一个与资料相关的问题…")} value={question} onChange={e => setQuestion(e.target.value)}/><button className="primary" disabled={busy || selected?.status !== 'ready' || !question.trim()}>{t("检索")}</button></form>{hits?.map(hit => <article className="retrieval-hit" key={hit.id}><b>{hit.title} · {hit.page ? t("第 {0} 页 · ", hit.page) : ''}{t("片段")}{hit.paragraph}</b><p>{hit.text}</p></article>)}{hits?.length === 0 && <p>{t("没有找到相关片段。")}</p>}</>}</section>}
        </section>
      </div>}</>}
    {confirm && <div className="modal-backdrop"><section className="modal small-modal" role="dialog" aria-label={t("确认操作")}><h2>{t("确认操作")}</h2><p>{confirm.title}</p><div className="button-row"><button className="secondary" onClick={() => setConfirm(null)}>{t("取消")}</button><button className="primary" onClick={() => { const action = confirm.action; setConfirm(null); run(action); }}>{t("确认")}</button></div></section></div>}
  </main>;
}
