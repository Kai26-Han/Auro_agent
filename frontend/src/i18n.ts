import { useSyncExternalStore } from 'react';
import en from './locales/en.json';
export type Language = 'zh' | 'en';
let language: Language = localStorage.getItem('workbench-language') === 'en' ? 'en' : 'zh';
const listeners = new Set<() => void>();
export function setLanguage(value: Language) {
  language = value; localStorage.setItem('workbench-language', value);
  document.documentElement.lang = value === 'zh' ? 'zh-CN' : 'en';
  document.title = 'Auro · Agentic for everything.';
  listeners.forEach(fn => fn());
}
export function useLanguage() { return useSyncExternalStore(fn => { listeners.add(fn); return () => listeners.delete(fn); }, () => language); }
export function t(text: string, ...values: unknown[]): string {
  const translated = language === 'en' ? (en as Record<string, string>)[text] ?? text : text;
  return values.length ? translated.replace(/\{(\d+)\}/g, (_, i) => String(values[Number(i)] ?? '')) : translated;
}
setLanguage(language);

// Only translate server-generated index status, preserving document names.
export function indexProgress(text: string): string {
  const patterns: [RegExp, string][] = [
    [/^已就绪 · (\d+) 个片段$/, '已就绪 · {0} 个片段'],
    [/^已就绪 · (\d+) 份文档 · (\d+) 页$/, '已就绪 · {0} 份文档 · {1} 页'],
    [/^生成 (\d+) 个片段的向量…$/, '生成 {0} 个片段的向量…'],
    [/^分块：(.*)$/s, '分块：{0}'],
    [/^建立文档树与摘要：(.*)$/s, '建立文档树与摘要：{0}'],
    [/^索引未完成（(\w+)）。请测试 Embedding 连接、检查文档，随后重试。$/, '索引未完成（{0}）。请测试 Embedding 连接、检查文档，随后重试。'],
    [/^文档树未完成（(\w+)）。请检查对话模型连接和 PDF 文本，随后重试。$/, '文档树未完成（{0}）。请检查对话模型连接和 PDF 文本，随后重试。'],
  ];
  for (const [pattern, key] of patterns) { const match = text.match(pattern); if (match) return t(key, ...match.slice(1)); }
  return t(text);
}
