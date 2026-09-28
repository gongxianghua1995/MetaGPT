"""Bounded native tool requests shared by SWE planning and review roles."""
import asyncio
import time

from metagpt.roles.di.swe_rate_limit import wait_for_model_slot

async def request_tools(role, messages, tools, seconds, *, max_tokens, final_tool=None):
    state = role.case_state
    started = time.monotonic()
    request_id = '%s-%s' % (role.name, time.monotonic_ns())
    if state:
        state.record('role_model_request', role=role.name, request_id=request_id,
                     messages=messages, tool_names=[t['function']['name'] for t in tools],
                     seconds=seconds, max_tokens=max_tokens)
    try:
        deadline = started + seconds
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            slot_wait = await asyncio.wait_for(asyncio.to_thread(wait_for_model_slot), timeout=remaining)
            if slot_wait and state:
                state.record('model_rate_wait', role=role.name, seconds=round(slot_wait, 3))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            try:
                response = await asyncio.wait_for(role.llm._achat_completion_function(
                    messages=messages, tools=tools,
                    tool_choice={'type': 'function', 'function': {'name': final_tool}} if final_tool else 'auto',
                    max_tokens=max_tokens, timeout=remaining), timeout=remaining)
                break
            except Exception as exc:
                status = getattr(exc, 'status_code', None)
                terminal = status in (401, 402) or any(marker in str(exc) for marker in (
                    'insufficient_quota', 'TokenStatusExhausted', 'Budget has been exceeded'))
                transient = status in (429, 502, 503, 504) or type(exc).__name__ in (
                    'RateLimitError', 'BadGatewayError', 'APIConnectionError', 'APITimeoutError')
                delay = 4 * (2 ** attempt)
                if terminal or not transient or attempt == 2 or deadline-time.monotonic() <= delay+1:
                    raise
                if state:
                    state.record('role_model_retry', role=role.name, request_id=request_id,
                                 attempt=attempt+1, error=type(exc).__name__, delay_seconds=delay)
                await asyncio.sleep(delay)
        if state:
            state.record('role_model_response', role=role.name, request_id=request_id,
                         duration_seconds=round(time.monotonic()-started, 3),
                         finish_reason=response.choices[0].finish_reason,
                         message=response.choices[0].message.model_dump(),
                         usage=response.usage.model_dump() if getattr(response, 'usage', None) else {})
        return response.choices[0]
    except BaseException as exc:
        if state:
            state.record('role_model_interrupted', role=role.name, request_id=request_id,
                         duration_seconds=round(time.monotonic()-started, 3), error=type(exc).__name__)
        raise
