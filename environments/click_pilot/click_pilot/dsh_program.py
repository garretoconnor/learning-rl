"""Headless dsh RPC driver inside the isolated harness VM (stdlib only)."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path('/tmp/pilot-cli')


async def main(config):
    profile = ROOT/'home/dsh/profiles/sdk-minimal'
    profile.mkdir(parents=True)
    (profile/'node_modules').symlink_to(ROOT/'dsh/node_modules', target_is_directory=True)
    process = await asyncio.create_subprocess_exec(str(ROOT/'node/bin/node'),
        str(ROOT/'dsh/node_modules/@deepseek-ai/dsh/lib/bin.js'), '--profile', 'sdk-minimal',
        '--patch', str(ROOT/'dsh-patch.json'), cwd=ROOT/'work',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        # Stderr inherits the worker's isolated log; no credential-bearing parent env.
        limit=4*1024*1024)
    async def rpc(id, method, params):
        process.stdin.write((json.dumps({'jsonrpc':'2.0','id':id,'method':method,'params':params})+'\n').encode())
        await process.stdin.drain()
        while line := await process.stdout.readline():
            value = json.loads(line)
            print(json.dumps(value), flush=True)
            if value.get('id') == id:
                if 'error' in value:
                    raise RuntimeError('dsh RPC failed: '+str(value['error']))
                return value.get('result')
        raise RuntimeError('dsh closed before answering '+method)
    try:
        await rpc(1,'initialize',{'provider':'pilot','model':config['model'],'cwd':str(ROOT/'work'),
                                'reasoningEffort':'low','maxTokens':4096})
        await rpc(2,'session/prompt',{'sessionId':config['trace_id'],
                                    'contentBlocks':[{'type':'text','text':config['prompt']}]})
        await rpc(3,'session/wait',{'sessionId':config['trace_id']})
        await rpc(4,'shutdown',{})
        code = await process.wait()
        if code:raise RuntimeError('dsh exited with status '+str(code))
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == '__main__':
    config=json.loads(Path(sys.argv[1]).read_text())
    asyncio.run(main(config))
