"""Minimal stateless Responses loop; all model-directed execution stays in MCP."""
import asyncio
import json
from pathlib import Path
import sys

import httpx
from openai import AsyncOpenAI
from verifiers.v1.harnesses.null.program import connect_mcp, call_mcp


async def main(path):
    payload = Path(path)
    config = json.loads(payload.read_text())
    payload.unlink()
    client = AsyncOpenAI(base_url=config['endpoint'], api_key=config['secret'],
                         timeout=httpx.Timeout(None, connect=5.0))
    try:
        tools, dispatch, servers = await connect_mcp({'mcpServers': {
            name: {'url': url, 'timeout': config['tool_timeout']} for name, url in config['mcp_urls'].items()}})
        tools = [{'type': 'function', **tool['function']} for tool in tools]
        messages = [{'role': 'user', 'content': config['prompt']}]
        while True:
            kwargs = {'instructions': config['system']} if config.get('system') else {}
            response = await client.responses.create(model=config['model'], input=messages,
                                                      tools=tools, store=False, **kwargs)
            messages.extend(item.model_dump(exclude_none=True) for item in response.output)
            calls = [item for item in response.output if item.type == 'function_call']
            if not calls:
                break
            for call in calls:
                try:
                    arguments = json.loads(call.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError('Tool arguments must be an object')
                    if call.name not in dispatch:
                        raise ValueError('Unknown tool')
                except (ValueError, TypeError) as error:
                    content = f'error: {error}'
                else:
                    content = await call_mcp(servers, dispatch, call.name, arguments)
                messages.append({'type': 'function_call_output', 'call_id': call.call_id,
                                 'output': content if isinstance(content, str) else json.dumps(content)})
    finally:
        await client.close()


if __name__ == '__main__':
    asyncio.run(main(sys.argv[1]))
