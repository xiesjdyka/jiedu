"""Model output capability and remaining context, without user length quotas."""
import json


def requested_settings(thinking=False, output_limit='auto', context_tokens=8192, max_output_tokens=None, *, default_output_target=None, output_policy=None):
    # Old clients/queued jobs may still supply length choices. They no longer
    # constrain generation, including explicit short limits and auto targets.
    if type(thinking) is not bool:
        raise ValueError('思考选项必须是开启或关闭')
    if type(context_tokens) is not int or context_tokens < 1:
        raise ValueError('模型上下文容量必须是正整数')
    if max_output_tokens is not None and (type(max_output_tokens) is not int or max_output_tokens < 1):
        raise ValueError('模型输出容量必须是正整数')
    return {'thinking': thinking, 'context_tokens': context_tokens,
            'max_output_tokens': max_output_tokens,
            'output_limit_source': output_policy or ('model_capacity' if max_output_tokens is not None else 'runtime_context')}


def settings_for(job):
    value=job.get('requested')
    requested=dict((json.loads(value) if isinstance(value,str) else value) or requested_settings())
    requested.pop('output_limit',None)
    requested.pop('default_output_target',None)
    requested['output_limit_source']='model_capacity' if requested.get('max_output_tokens') is not None else 'runtime_context'
    if requested.get('operation')=='reasoning_summary':
        # The separate, explicit synopsis task keeps its existing short format.
        requested['max_output_tokens']=384
    elif requested.get('provider_snapshot',{}).get('selected')=='external':
        from .butler_provider import model_output_capacity
        requested['max_output_tokens']=model_output_capacity(requested['provider_snapshot']['external'])
        requested['output_limit_source']='service_default' if requested['max_output_tokens'] is None else 'model_capacity'
    return requested


def output_target(requested):
    """Known single-completion capability; None means no separate runtime cap."""
    return requested.get('max_output_tokens')


def input_limit(requested, *, minimum_input_tokens=None):
    # This small input fitting margin never caps the generated answer. Reserving
    # the entire model output capability would discard normal history/evidence.
    reserve=min(512,output_target(requested) or 512)
    if minimum_input_tokens is not None:
        available=requested['context_tokens']-minimum_input_tokens
        reserve=min(reserve,max(1,available//2))
    return requested['context_tokens']-reserve


def effective_settings(requested, input_tokens):
    available=requested['context_tokens']-input_tokens
    if available < 1:raise ValueError('问题和资料已占满上下文，请缩短问题')
    capacity=output_target(requested)
    managed=capacity is None and requested.get('output_limit_source')=='service_default'
    limit=None if managed else min(capacity,available) if capacity is not None else available
    return {'thinking':requested['thinking'],'max_output_tokens':limit,'input_tokens':input_tokens,
            'context_tokens':requested['context_tokens'],'available_output_tokens':available,
            'shared_thinking_answer_budget':True,'model_used':True,'output_target':capacity,
            'output_limit_source':'service_default' if managed else 'model_capacity' if capacity is not None else 'runtime_context',
            'output_limit_reasons':['service_default'] if managed else ['context_limit'] if capacity is None or available<capacity else ['model_capacity']}
