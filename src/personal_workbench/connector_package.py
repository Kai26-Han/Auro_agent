"""Connector 安装包解析：只接受公开配置，不接受凭据或可执行载荷。"""
import io
import hashlib
import json
import re
import stat
import zipfile
from urllib.parse import urlsplit


UPLOAD_LIMIT = 2 * 1024 * 1024


def _https_url(value, label):
    parsed = urlsplit(value or '')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError(f'{label}必须是 HTTPS 地址。')
    return value


def parse_connector_package(filename, data):
    if len(data) > UPLOAD_LIMIT:
        raise ValueError('连接器安装包最多 2 MiB。')
    if filename.lower().endswith('.json'):
        raw = data
    elif filename.lower().endswith('.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 30 or sum(item.file_size for item in entries) > 4 * 1024 * 1024:
                    raise ValueError('连接器安装包解压后最多 4 MiB、30 个文件。')
                for item in entries:
                    mode = item.external_attr >> 16
                    if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
                        raise ValueError('连接器安装包不支持链接、特殊文件或加密文件。')
                manifests = [item for item in entries if item.filename.rstrip('/').split('/')[-1] == 'connector.json' and not item.is_dir()]
                if len(manifests) != 1:
                    raise ValueError('安装包必须包含一个 connector.json。')
                raw = archive.read(manifests[0])
        except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError):
            raise ValueError('无法读取此连接器 ZIP 安装包。') from None
    else:
        raise ValueError('请选择 connector.json 或 ZIP 安装包。')
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('connector.json 必须是 UTF-8 JSON。') from None
    allowed = {'schema_version','id','name','version','description','transport','http','stdio','oauth','dependencies'}
    if not isinstance(manifest, dict) or set(manifest) - allowed or manifest.get('schema_version') != 1:
        raise ValueError('连接器清单字段或 schema_version 无效。')
    if not isinstance(manifest.get('id'), str) or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', manifest['id']):
        raise ValueError('连接器 id 须使用小写字母、数字和短横线。')
    for key, maximum in [('name',80),('version',80),('description',1000)]:
        if not isinstance(manifest.get(key), str) or not 1 <= len(manifest[key].strip()) <= maximum:
            raise ValueError(f'连接器 {key} 无效。')
    transport = manifest.get('transport')
    if transport == 'http':
        config = manifest.get('http')
        if not isinstance(config,dict) or set(config)-{'url'} or not isinstance(config.get('url'),str):
            raise ValueError('HTTP 连接器必须声明服务地址。')
        parsed=urlsplit(config['url'])
        if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('MCP 服务地址无效。')
        if parsed.scheme=='http' and parsed.hostname not in {'localhost','127.0.0.1','::1'}:
            raise ValueError('远程 MCP 服务必须使用 HTTPS。')
    elif transport == 'stdio':
        config = manifest.get('stdio')
        if (not isinstance(config,dict) or set(config)-{'command','args'} or not isinstance(config.get('command'),str)
                or not re.fullmatch(r'[A-Za-z0-9._+-]{1,120}',config['command']) or not isinstance(config.get('args',[]),list)
                or len(config.get('args',[]))>40 or any(not isinstance(x,str) or len(x)>4000 for x in config.get('args',[]))):
            raise ValueError('stdio 连接器启动配置无效。')
    else:
        raise ValueError('连接器 transport 只支持 http 或 stdio。')
    oauth=manifest.get('oauth')
    if oauth is not None:
        if transport!='http' or not isinstance(oauth,dict) or set(oauth)-{'authorization_url','token_url','client_id','scopes','extra_authorize_params'}:
            raise ValueError('OAuth 配置无效。')
        _https_url(oauth.get('authorization_url'),'OAuth 授权地址')
        _https_url(oauth.get('token_url'),'OAuth Token 地址')
        if not isinstance(oauth.get('client_id'),str) or not 1<=len(oauth['client_id'])<=300:
            raise ValueError('OAuth client_id 无效。')
        if (not isinstance(oauth.get('scopes',[]),list) or len(oauth.get('scopes',[]))>30
                or any(not isinstance(x,str) or not 1<=len(x)<=120 for x in oauth.get('scopes',[]))):
            raise ValueError('OAuth scopes 无效。')
        extra=oauth.get('extra_authorize_params',{})
        if not isinstance(extra,dict) or len(extra)>20 or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>500 for k,v in extra.items()):
            raise ValueError('OAuth 附加参数无效。')
    deps={**{'skills':[],'executables':[],'environment':[]},**(manifest.get('dependencies') or {})}
    if set(deps)-{'skills','executables','environment'}: raise ValueError('连接器依赖声明无效。')
    if (not isinstance(deps['skills'],list) or len(deps['skills'])>20 or
            any(not isinstance(x,dict) or set(x)-{'name','min_version'} or not isinstance(x.get('name'),str) or not isinstance(x.get('min_version',''),str) for x in deps['skills'])):
        raise ValueError('连接器 Skill 依赖无效。')
    for key in ('executables','environment'):
        if not isinstance(deps[key],list) or len(deps[key])>30 or any(not isinstance(x,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.+-]{0,119}',x) for x in deps[key]):
            raise ValueError('连接器运行环境依赖无效。')
    manifest['dependencies']=deps
    manifest['oauth']=oauth
    manifest['package_digest']=hashlib.sha256(json.dumps(manifest,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return manifest
