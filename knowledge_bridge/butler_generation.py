"""Validated request choices and token capacity shared by all model calls."""
import json


def requested_settings(thinking=False, output_limit='auto', context_tokens=8192, max_output_tokens=None, *, default_output_target=512):
    if type(thinking) is not bool:
        raise ValueError('思考选项必须是开启或关闭')
    if output_limit not in ('auto','max') and (type(output_limit) is not int or not 1 <= output_limit <= context_tokens):
        raise ValueError('生成上限必须是自动、当前上下文范围内的正整数，或最大')
    capacity=context_tokens if max_output_tokens is None else max_output_tokens
    if type(capacity) is not int or capacity<1:raise ValueError('模型输出容量必须是正整数')
    if type(default_output_target) is not int or default_output_target<1:raise ValueError('默认生成额度必须是正整数')
    return {'thinking': thinking, 'output_limit': output_limit, 'context_tokens': context_tokens, 'max_output_tokens':capacity,
        'output_limit_source':'product_default' if output_limit=='auto' else 'owner_explicit',
        'default_output_target':default_output_target if output_limit=='auto' else None}


def settings_for(job):
    value=job.get('requested')
    return (json.loads(value) if isinstance(value,str) else value) or requested_settings()


def output_target(requested):
    if requested['output_limit']=='auto':return requested.get('default_output_target') or 512
    if requested['output_limit']=='max':return requested.get('max_output_tokens',requested['context_tokens'])
    return requested['output_limit']


def input_limit(requested, *, minimum_input_tokens=None):
    # Keep normal evidence input first; "max" gets the remaining capacity afterward.
    if requested['output_limit']=='auto':
        reserve=min(output_target(requested),requested.get('max_output_tokens',requested['context_tokens']))
        available=requested['context_tokens']-(minimum_input_tokens if minimum_input_tokens is not None else requested['context_tokens']//2)
        # Preserve the current question/images. If its mandatory input cannot
        # coexist with the default target, share the remaining room with history.
        if reserve>available:reserve=max(1,available//2)
    else:reserve=512 if requested['output_limit']=='max' else min(512,requested['output_limit'])
    return requested['context_tokens']-reserve


def effective_settings(requested, input_tokens):
    available=requested['context_tokens']-input_tokens
    if available < 1:raise ValueError('问题和资料已占满上下文，请缩短问题')
    output_capacity=requested.get('max_output_tokens',requested['context_tokens'])
    target=output_target(requested)
    limit=min(target,available,output_capacity)
    return {'thinking':requested['thinking'],'max_output_tokens':limit,'input_tokens':input_tokens,
            'context_tokens':requested['context_tokens'],'available_output_tokens':available,
            'shared_thinking_answer_budget':True,'model_used':True,'output_target':target,
            'output_limit_source':requested.get('output_limit_source','owner_explicit'),
            'output_limit_reasons':(['service_limit'] if output_capacity<target else [])+(['context_limit'] if available<target else [])}
