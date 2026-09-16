"""Span factories. Every fixture here mirrors a shape seen in real exports —
if you change one, reproduce the case in a trace first."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def span(name, agent, op_id="op1", *, ts="2026-09-03T17:00:00.000Z",
         success=True, duration=10.0, **dims):
    d = {"gen_ai.agent.name": agent}
    for k, v in dims.items():
        d[k.replace("__", ".")] = v
    return {"timestamp": ts, "name": name, "id": name, "op_id": op_id,
            "parent": "", "duration": duration, "success": success, "d": d}


def tool_call(tool, agent, op_id="op1", *, args=None, result="",
              ts="2026-09-03T17:00:00.000Z", success=True, with_child=True):
    """A real MCP call emits BOTH spans; `with_child` reproduces that."""
    dims = {
        "gen_ai.tool.name": tool,
        "gen_ai.tool.call.arguments": json.dumps(args or {}),
        "gen_ai.tool.call.result": result,
        "gen_ai.operation.name": "execute_tool",
    }
    parent = {"timestamp": ts, "name": f"execute_tool {tool}", "id": tool,
              "op_id": op_id, "parent": "", "duration": 10.0,
              "success": success,
              "d": dict(dims, **{"gen_ai.agent.name": agent})}
    out = [parent]
    if with_child:
        out.append({"timestamp": ts, "name": f"tools/call {tool}",
                    "id": tool + "-c", "op_id": op_id, "parent": tool,
                    "duration": 9.0, "success": success,
                    "d": {"gen_ai.agent.name": agent,
                          "gen_ai.operation.name": "execute_tool"}})
    return out


def invoke(agent, op_id="op1", *, user_text="", output="[]", **dims):
    msgs = json.dumps([{"role": "user",
                        "parts": [{"type": "text", "content": user_text}]}])
    return span(f"invoke_agent {agent}", agent, op_id,
                **{"gen_ai__input__messages": msgs,
                   "gen_ai__output__messages": output, **dims})


@pytest.fixture
def repo():
    return REPO
