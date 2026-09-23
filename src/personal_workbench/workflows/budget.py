"""Provider-independent preflight estimates, settled against reported usage.

These are buffered estimates, not a tokenizer or a strict billing guarantee.
Completed calls are charged at full reported usage (including cached input).
"""
import json
import math


def text_tokens(text):
    # UTF-8 bytes overcharge Chinese heavily. Count Unicode text instead and
    # retain headroom for mixed prose, code, punctuation and protocol overhead.
    ascii_chars = sum(ord(c) < 128 for c in text)
    return math.ceil((ascii_chars / 3 + (len(text)-ascii_chars) * 1.5) * 1.15)


def message_tokens(messages):
    return sum(text_tokens(json.dumps({'role':m.type,'content':m.content,
        'tool_calls':getattr(m,'tool_calls',[]),'tool_call_id':getattr(m,'tool_call_id',None),
        'name':m.name},ensure_ascii=False,default=str)) + 12 for m in messages)


def estimate_tokens(messages, tools_json, max_output):
    fallback = message_tokens(messages) + text_tokens(tools_json) + 256
    # Every previous model response retains the provider's input count. Within
    # a stage the prompt/tools are frozen; use the latest measured prefix and
    # estimate only the newly appended assistant/tool messages. This also works
    # after checkpoint recovery, without a process-local calibration cache.
    for i in range(len(messages)-1,-1,-1):
        m=messages[i];usage=getattr(m,'usage_metadata',None) or {}
        measured=usage.get('input_tokens')
        if m.type=='ai' and isinstance(measured,int) and not isinstance(measured,bool) and measured>0:
            fallback = math.ceil(measured * 1.15) + message_tokens(messages[i:]) + 256
            break
    return fallback + max_output
