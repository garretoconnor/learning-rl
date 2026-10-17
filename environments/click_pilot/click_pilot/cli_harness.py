"""Pinned CLI adapters. Only the Prime-backed MCP command reaches the model."""
import json
from pathlib import Path
import sys

CODEX_VERSION = '0.160.0'
DSH_VERSION = '0.2.1-rc.2'


def codex_config(config, cwd, endpoint):
    home = cwd/'codex-home'
    home.mkdir()
    lines = [f'model = {json.dumps(config["model"])}', 'model_provider = "pilot"',
             'model_context_window = 1048576',
             f'model_auto_compact_token_limit = {config["compact_tokens"]}',
             'model_reasoning_effort = "low"', 'web_search = "disabled"',
             'approval_policy = "never"', 'sandbox_mode = "read-only"',
             'tool_output_token_limit = 12000',
             '[features]']
    for feature in ['shell_tool', 'shell_snapshot', 'shell_snapshot_v2', 'view_image',
                    'apps', 'plugins', 'multi_agent', 'multi_agent_v2', 'code_mode',
                    'code_mode_host', 'browser_use', 'computer_use', 'image_generation',
                    'hooks', 'memories', 'skill_mcp_dependency_install', 'standalone_web_search',
                    'enable_request_compression']:
        lines.append(feature+' = false')
    lines += ['[model_providers.pilot]', 'name = "Pilot Responses gateway"',
              f'base_url = {json.dumps(endpoint)}', 'wire_api = "responses"',
              'env_key = "HARNESS_GATEWAY_KEY"', 'supports_websockets = false',
              ]
    for url in config['mcp_urls'].values():
        lines += ['[mcp_servers.workspace]', f'url = {json.dumps(url)}', 'required = true',
                  'startup_timeout_sec = 60', 'tool_timeout_sec = 600', 'enabled_tools = ["command"]',
                  '[mcp_servers.workspace.tools.command]', 'approval_mode = "approve"']
    (home/'config.toml').write_text('\n'.join(lines)+'\n')
    return home


def dsh_patch(config, cwd, endpoint):
    # This is a VM path. Resolving it on macOS rewrites /tmp to /private/tmp.
    cache = Path(config['dsh_bin']).parents[3]
    # All shell, file, directory-changing and discovery tools are disabled.
    disabled = ['llm-deepseek', 'deepseek-llm-api-extensions', 'session-log-deepseek',
                'plugin-package-inventory-deepseek', 'tool-fs', 'tool-working-directory',
                'persistent-bash', 'persistent-pwsh', 'terminal-bash', 'terminal-pwsh', 'pty']
    patch = [{'id': id, 'disabled': True} for id in disabled]
    patch.append({'id': 'compaction-basic', 'config': {
        'thresholdRatio': config['compact_tokens']/1048576, 'retainTokens': min(8000, config['compact_tokens']//4),
        'maxTokens': 4096}})
    patch.append({'insert': [{'id': 'pilot-responses', 'name': str(cache/'@deepseek-ai/dsh-llm-pi-ai/lib/index.js'),
        'config': {'providers': {'pilot': {'apiKeyEnv': 'HARNESS_GATEWAY_KEY', 'api': 'openai-responses',
            'baseURL': endpoint, 'defaultContextWindow': 1048576, 'defaultMaxTokens': 4096,
            'models': [{'id': config['model'], 'contextWindow': 1048576,
                        'reasoningEfforts': {'low': 'low'}}]}}}},
        *[{'id': 'pilot-mcp', 'name': str(cache/'@deepseek-ai/dsh-mcp-client/lib/index.js'),
           'config': {'transport': 'streamable-http', 'serverName': 'workspace', 'url': url,
                      'failOnStartupError': True, 'toolCallTimeoutMs': 600000}}
          for url in config['mcp_urls'].values()]]})
    path = cwd/'dsh-patch.json'
    path.write_text(json.dumps(patch, indent=2))
    return path



def main():
    from .remote_harness import run
    path = Path(sys.argv[1])
    config = json.loads(path.read_text())
    path.unlink()
    run(config)


if __name__ == '__main__':
    main()
