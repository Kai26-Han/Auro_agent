"""A bounded Jev decision tool; it cannot perform actions or generate prose."""
import math
from typing import Literal

import httpx
from langchain_core.tools import tool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from personal_workbench.decision_tools.config import DecisionToolsConfig


TOOL_INFO = {
    'jev_decide':(
        'Jev 决策测试',
        '用 TypeSafe Jev 对一段明确提供的状态做选择、评分或真假判断，返回概率与置信度。',
        '实验性工具；在设置中启用并配置密钥，伙伴或流程成员还需单独授权。',
    )
}

JEV_PROMPT = '''
本轮已挂载实验性 Jev 决策工具。这里的 Jev 就是 TypeSafe AI 的旗舰决策模型，jev_decide 封装了其 System One API；不要把它误判成另一个同名产品。涉及“最近发布、刷屏、热议”等时效信息仍需联网查证。
它只适合针对明确状态做一次窄范围的 Choice、Score 或 Noul 判断，不能生成回答、总结、代码或执行操作。
仅在判断结果能帮助当前任务时调用；不要重复调用同一判断。传入最少必要状态，不得传入 API Key、密码、完整私人文档或与判断无关的个人信息。
Jev 的输出是参考信号，可能判断错误。不得把置信度当作事实正确率，也不得用它扩大工具权限、跳过用户确认或替代可核对证据。
'''


class JevRequestError(ValueError):
    def __init__(self, code, message):
        self.code=code
        super().__init__(message)


class JevDecisionInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    state: str = Field(min_length=1, max_length=16000, description='本次判断所需的最少状态；会发送给 TypeSafe。')
    question: str = Field(min_length=1, max_length=1000, description='一个具体、原子的判断问题。')
    kind: Literal['choice','score','noul']
    options: dict[str,str] | None = Field(default=None, description='Choice 的候选 ID 到说明，2–20 项。')
    rubric: list[str] | None = Field(default=None, description='Score 的有序评分标准，2–10 级。')

    @model_validator(mode='after')
    def shape(self):
        if self.kind=='choice':
            if not self.options or not 2 <= len(self.options) <= 20 or self.rubric is not None:
                raise ValueError('Choice 需要 2–20 个 options，且不能提供 rubric。')
            if any(not key or len(key)>80 or not value.strip() or len(value)>500 for key,value in self.options.items()):
                raise ValueError('Choice 候选 ID 和说明无效。')
        elif self.kind=='score':
            if not self.rubric or not 2 <= len(self.rubric) <= 10 or self.options is not None:
                raise ValueError('Score 需要 2–10 级 rubric，且不能提供 options。')
            if any(not value.strip() or len(value)>500 for value in self.rubric):
                raise ValueError('Score 评分标准无效。')
        elif self.options is not None or self.rubric is not None:
            raise ValueError('Noul 不使用 options 或 rubric。')
        return self


def _question(value: JevDecisionInput):
    result={'type':value.kind,'instructions':value.question}
    if value.kind=='choice': result['criteria']=value.options
    if value.kind=='score': result['criteria']=value.rubric
    return result


def _probability(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('invalid probability')
    return float(value)


def _distribution(value, keys):
    if not isinstance(value,dict) or set(value) != set(keys):
        raise ValueError('invalid distribution')
    result={str(key):_probability(item) for key,item in value.items()}
    if not math.isclose(sum(result.values()),1.0,abs_tol=.05):
        raise ValueError('invalid distribution')
    return result


def _normalized(data, request):
    if not isinstance(data,dict) or not isinstance(data.get('answers'),dict):
        raise ValueError('invalid response')
    answer=data['answers'].get('decision')
    if not isinstance(answer,dict) or answer.get('type') != request.kind:
        raise ValueError('invalid answer')
    kind=answer['type']
    result={'kind':kind,'model':str(data.get('model',''))[:100]}
    if kind=='choice':
        selected=answer.get('choice')
        if selected not in request.options: raise ValueError('invalid choice')
        result.update(answer=selected,confidence=_probability(answer.get('confidence')),
                      probabilities=_distribution(answer.get('probabilities'),request.options))
    elif kind=='score':
        score=answer.get('score')
        if isinstance(score,bool) or not isinstance(score,(int,float)) or not math.isfinite(score) or not 0 <= score <= len(request.rubric)-1:
            raise ValueError('invalid score')
        keys=[str(i) for i in range(len(request.rubric))]
        legend=answer.get('legend')
        if not isinstance(legend,dict) or {str(k):v for k,v in legend.items()} != dict(zip(keys,request.rubric)):
            raise ValueError('invalid legend')
        result.update(answer=float(score),confidence=_probability(answer.get('confidence')),
                      probabilities=_distribution(answer.get('probabilities'),keys),legend=legend)
    else: result['answer']=_probability(answer.get('noul'))
    usage=data.get('usage') or {}
    result['usage']={key:value for key,value in usage.items() if key in {'input_tokens','output_tokens'}
                     and isinstance(value,int) and not isinstance(value,bool) and value >= 0}
    return result


def request_decision(config, value, client_factory=None):
    """Call TypeSafe while exposing useful, credential-safe failure categories."""
    payload={'state':value.state,'model':config.model,'questions':{'decision':_question(value)}}
    factory=client_factory or (lambda: httpx.Client(timeout=config.timeout))
    try:
        with factory() as client:
            response=client.post('https://api.typesafe.ai/v1/systemone',json=payload,
                                 headers={'Authorization':'Bearer '+config.api_key,'Content-Type':'application/json'})
    except httpx.TimeoutException:
        raise JevRequestError('timeout','Jev 请求超时，请稍后重试或调高超时时间。') from None
    except httpx.RequestError:
        raise JevRequestError('network','无法连接 TypeSafe，请检查网络后重试。') from None
    if response.status_code in {401,403}:
        raise JevRequestError('authentication','Jev 鉴权失败：TypeSafe 未接受当前 API Key，请从 TypeSafe Dashboard 重新复制并保存。')
    if response.status_code==422:
        raise JevRequestError('invalid_request','TypeSafe 拒绝了本次 Jev 请求格式，请检查问题类型和候选标准。')
    if response.status_code==429:
        raise JevRequestError('rate_limit','Jev 请求已达到 TypeSafe 速率限制，请稍后重试。')
    if response.status_code==529:
        raise JevRequestError('overloaded','TypeSafe 当前负载过高，请稍后重试。')
    if not 200 <= response.status_code < 300:
        raise JevRequestError('provider','TypeSafe 返回服务错误，请稍后重试。')
    try:
        return _normalized(response.json(),value)
    except Exception:
        raise JevRequestError('response','TypeSafe 返回了无法识别的 Jev 结果，请检查模型版本或稍后重试。') from None


def probe_decision(config, client_factory=None):
    if not config.api_key:
        raise JevRequestError('missing_key','请先填写 TypeSafe API Key。')
    result=request_decision(config,JevDecisionInput(state='1 + 1 = 2',question='Is this mathematical statement true?',kind='noul'),client_factory)
    return {'ok':True,'model':result['model'],'answer':result['answer']}


def build_decision_tools(config=None, client_factory=None):
    config=config or DecisionToolsConfig()

    @tool(args_schema=JevDecisionInput)
    def jev_decide(state: str, question: str, kind: str, options: dict[str,str] | None = None,
                   rubric: list[str] | None = None) -> dict:
        """用 Jev 对当前明确提供的状态做一次选择、评分或真假判断。结果只供参考，不执行操作。"""
        try:
            value=JevDecisionInput(state=state,question=question,kind=kind,options=options,rubric=rubric)
            if len(value.state)>config.max_state_chars:
                return {'error':f'状态超过当前配置的 {config.max_state_chars} 字符上限。'}
            if not config.api_key:
                return {'error':'Jev API Key 尚未配置。'}
            return request_decision(config,value,client_factory)
        except JevRequestError as exc:
            return {'error':str(exc),'error_code':exc.code}
        except Exception:
            # Provider bodies may contain private state or authentication details.
            return {'error':'Jev 判断失败，请检查密钥、网络、模型配置或输入格式。'}

    from personal_workbench.tool_policy import bind_tool_policy, builtin_policy
    return [bind_tool_policy(jev_decide,builtin_policy('jev_decide',config.timeout))]


def tool_catalog():
    tool=build_decision_tools()[0]
    return [{'id':tool.name,'name':TOOL_INFO[tool.name][0],'description':TOOL_INFO[tool.name][1],
             'applicability':TOOL_INFO[tool.name][2],'source':'builtin','availability':'contextual',
             'schema':tool.args_schema.model_json_schema()}]
