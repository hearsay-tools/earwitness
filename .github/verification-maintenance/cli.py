#!/usr/bin/env python3
"""Project verification discovery, installation and scheduled maintenance."""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from discovery import discover, setup_gap


def load(path):
    return json.loads(Path(path).read_text())


def save(path, data):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2) + '\n')


def api(method, path, body=None):
    token = os.environ.get('GH_TOKEN')
    if not token:
        raise ValueError('GH_TOKEN is required for the GitHub preflight or publisher')
    base = os.environ.get('GITHUB_API_URL', 'https://api.github.com')
    request = urllib.request.Request(base + '/' + path.lstrip('/'),
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'}, method=method)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def prepare(root, config):
    base = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    result = {'base_sha': base, 'provider': config['provider'], 'ready': False,
              'outcome': 'blocked', 'summary': ''}
    found = discover(root, config['target'])
    if found['status'] != 'present':
        result['summary'] = found.get('reason', 'Verification skill is missing or ambiguous')
        return result
    # Publisher rejects aliases, so record the canonical path during installation.
    if found['paths'] != [config['target']]:
        result['summary'] = 'Configure the canonical project skill path before scheduling maintenance'
        return result
    from publisher import branch_name
    repository = config['repository']
    owner = repository.split('/')[0]
    query = urllib.parse.urlencode({'state': 'open', 'head': f'{owner}:{branch_name(config["target"])}', 'per_page': 100})
    try:
        prs = api('GET', f'/repos/{repository}/pulls?{query}')
        if not isinstance(prs, list):
            raise TypeError('GitHub returned an invalid pull request list')
    except (ValueError, TypeError, OSError) as error:
        result['summary'] = f'Cannot check existing maintenance PRs: {error}'
        return result
    if prs:
        result['summary'] = f'Existing maintenance PR: {prs[0]["html_url"]}'
        return result
    result.update(ready=True, summary='Target resolved and no existing maintenance PR found')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('discover', 'install', 'prepare', 'bootstrap', 'prompt', 'bundle', 'publish'):
        command = sub.add_parser(name)
        command.add_argument('--root', type=Path, default=Path.cwd())
        if name != 'discover':
            command.add_argument('--config', type=Path, required=True)
        if name in ('prepare', 'prompt', 'bundle'):
            command.add_argument('--output', type=Path, required=True)
        if name == 'discover':
            command.add_argument('--target')
            command.add_argument('--issues', type=Path, help='JSON list, or paginated list of lists of existing issues')
        if name == 'install':
            command.add_argument('--templates', type=Path, required=True)
            command.add_argument('--maintenance-skill', type=Path)
            command.add_argument('--write', action='store_true')
            command.add_argument('--replace', action='store_true')
        if name == 'bundle':
            command.add_argument('--report', type=Path, required=True)
            command.add_argument('--base-sha', required=True)
        if name == 'publish':
            command.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.command == 'discover':
        result = discover(root, args.target)
        if args.issues:
            issues = load(args.issues)
            if issues and isinstance(issues[0], list):
                issues = [issue for page in issues for issue in page]
            result['gap'] = setup_gap(result, issues)
    else:
        config = load(args.config)
        if args.command == 'install':
            from installer import install
            result = install(root, config, args.templates, write=args.write, replace=args.replace,
                             maintenance_skill=args.maintenance_skill)
        elif args.command == 'prepare':
            result = prepare(root, config)
            save(args.output, result)
            if not result['ready']:
                save(root / '.verification-report.json', {
                    'outcome': 'blocked', 'summary': result['summary'], 'coverage': []})
            if os.environ.get('GITHUB_OUTPUT'):
                with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                    output.write(f'ready={str(result["ready"]).lower()}\nbase_sha={result["base_sha"]}\nprovider={result["provider"]}\n')
        elif args.command == 'bootstrap':
            commands = config['bootstrap']
            if not isinstance(commands, list) or not all(isinstance(c, str) for c in commands):
                raise ValueError('bootstrap must be a list of project commands')
            for command in commands:
                subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', command], cwd=root, check=True)
            result = {'outcome': 'prepared'}
        elif args.command == 'prompt':
            template = args.config.parent / 'prompt.md'
            text = template.read_text().replace('%%TARGET%%', config['target'])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text)
            result = {'prompt': str(args.output)}
        elif args.command == 'bundle':
            from bundle import collect
            result = collect(root, config, load(args.report), args.base_sha)
            save(args.output, result)
        else:
            from publisher import publish
            if os.environ.get('GITHUB_RUN_ID') and os.environ.get('GITHUB_REPOSITORY'):
                server = os.environ.get('GITHUB_SERVER_URL', 'https://github.com')
                config['evidence_url'] = f'{server}/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'
            result = publish(root, load(args.bundle), config, api)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, KeyError, TypeError) as error:
        print(f'verification-maintenance: {error}', file=sys.stderr)
        sys.exit(1)
