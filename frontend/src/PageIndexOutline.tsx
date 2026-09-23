import { t } from './i18n';
import { useState } from 'react';
import { api } from './api';

type Outline = { title: string; pages: number; total: number; next_offset: number | null; nodes: { title: string; page: number; summary: string; depth: number }[] };
export function PageIndexOutline({ kid, docs, ready }: { kid: string; docs: { id: string; name: string }[]; ready: boolean }) {
  const [sid, setSid] = useState('');
  const [outline, setOutline] = useState<Outline | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function load(source: string, offset = 0) {
    setBusy(true); setError('');
    try { setOutline(await api<Outline>(`/knowledge/bases/${kid}/outline?source_id=${encodeURIComponent(source)}&offset=${offset}`)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <details className="retrieval-test"><summary>{t("预览 PageIndex 文档树")}</summary><p className="field-hint">{t("目录和摘要用于定位；对话会读取相关页面，并引用原文作答。")}</p><select aria-label={t("预览文档")} disabled={!ready || busy} value={sid} onChange={e => { setSid(e.target.value); setOutline(null); if (e.target.value) load(e.target.value); }}><option value="">{t("选择一份已索引的 PDF")}</option>{docs.map(d => <option value={d.id} key={d.id}>{d.name}</option>)}</select>{!ready && <p className="field-hint">{t("构建索引后可查看文档树。")}</p>}{busy && <p>{t("加载中…")}</p>}{error && <p role="alert">{error}</p>}{outline && <><p className="field-hint">{outline.title} · {outline.pages} {t("页 ·")}{outline.total} {t("个目录节点")}</p>{outline.nodes.map((node, i) => <article className="retrieval-hit" key={i} style={{ marginLeft: Math.min(node.depth, 4) * 10 }}><b>{node.title} {t("· 第")}{node.page} {t("页起")}</b><p>{node.summary}</p></article>)}<div className="button-row"><button className="secondary" disabled={busy} onClick={() => load(sid)}>{t("回到开头")}</button>{outline.next_offset !== null && <button className="secondary" disabled={busy} onClick={() => load(sid, outline.next_offset!)}>{t("下一组目录")}</button>}</div></>}</details>;
}
