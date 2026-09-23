import { t } from './i18n';

export class ApiError extends Error {
  constructor(message:string,public readonly status:number,public readonly detail:unknown){
    super(message);this.name='ApiError';
  }
}

function errorMessage(detail: unknown): string {
  if (typeof detail !== 'string') return t('操作未完成，请检查输入后重试。');
  const lengthError = detail.match(/^SKILL\.md 正文共 (\d+) 个字符，超过 (\d+) 字符上限。请把参考资料拆分到 references\/ 文件夹，并在正文中说明何时读取。$/);
  if (lengthError) return t('SKILL.md 正文共 {0} 个字符，超过 {1} 字符上限。请把参考资料拆分到 references/ 文件夹，并在正文中说明何时读取。', lengthError[1], lengthError[2]);
  return t(detail);
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, { ...init, headers: typeof init.body === 'string' ? { 'Content-Type': 'application/json', ...init.headers } : init.headers });
  const body = await response.text();
  let data: any = undefined;
  if (body.trim()) {
    try {
      data = JSON.parse(body);
    } catch {
      if (!response.ok) throw new ApiError(errorMessage(body),response.status,body);
      throw new Error(t('后台返回了无法识别的数据，请重试。'));
    }
  }
  if (response.status === 404 && data?.detail === 'Not Found') {
    throw new ApiError(t('当前后台服务尚未加载此功能，请重启本地工作台后刷新页面。'),response.status,data?.detail);
  }
  if (!response.ok) throw new ApiError(errorMessage(data?.detail || response.statusText),response.status,data?.detail);
  return data as T;
}
