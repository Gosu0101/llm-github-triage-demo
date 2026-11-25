"""Build triage input bundles for one event in the demo repository.

Reads the GitHub event file, writes a collector-style JSON and lets the agent's
own ``triage_agent.feature_inputs`` turn it into ``issues.json`` / ``prs.json``.
Issue and PR text only travels through files, never through shell arguments.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from triage_agent import feature_inputs, review_bundle
from triage_agent.github import GitHubClient

PATCH_LIMIT = 12_000  # Same patch rule as the collector (collector.py).


def issue_record(issue):
    return {'number': issue['number'], 'title': issue['title'],
            'body': issue.get('body'), 'is_pull_request': False}


def collect_issue(event, repository, client, candidate=None):
    """Target issue first (triage_pipeline --smoke runs only the first case)."""
    issue = event['issue']
    records, note = [issue_record(issue)], None
    if candidate is not None:
        if candidate >= issue['number']:
            note = f'candidate #{candidate} is not older than target #{issue["number"]}; duplicate step skipped'
        else:
            found = client.get(f'/repos/{repository}/issues/{candidate}').data
            if 'pull_request' in found:
                raise ValueError(f'#{candidate} is a pull request, not an issue')
            records.append(issue_record(found))
    return {'metadata': {'repository': repository}, 'issues': records}, note


def pr_files(client, repository, number):
    files, page = [], 1
    while True:
        batch = client.get(f'/repos/{repository}/pulls/{number}/files',
                           params={'per_page': 100, 'page': page}).data
        for item in batch:
            patch = item.get('patch')
            files.append({'filename': item['filename'],
                          'patch': patch[:PATCH_LIMIT] if isinstance(patch, str) else None,
                          'patch_truncated': isinstance(patch, str) and len(patch) > PATCH_LIMIT})
        if len(batch) < 100:
            return files
        page += 1


def collect_pr(event, repository, client):
    pr = event['pull_request']
    record = {'number': pr['number'], 'title': pr['title'], 'body': pr.get('body'),
              'is_pull_request': True, 'files': pr_files(client, repository, pr['number'])}
    return {'metadata': {'repository': repository}, 'pull_requests': [record]}


def write_bundles(collection, output):
    """Save the collection and the agent-built bundles; return the bundles."""
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'collection.json'
    review_bundle.write_json(source, collection)
    _, bundles = feature_inputs.prepare(source)
    for name, bundle in bundles.items():
        review_bundle.write_json(output / name, bundle)
    return bundles


def step_outputs(kind, bundles):
    if kind == 'issue':
        cases = bundles['issues.json']['cases']
        outputs = {'case_id': cases[0]['case_id'], 'target_item_id': cases[0]['item_id'],
                   'has_candidate': 'true' if len(cases) > 1 else 'false'}
        if len(cases) > 1:
            outputs['candidate_item_id'] = cases[1]['item_id']
        return outputs
    case = bundles['prs.json']['cases'][0]
    return {'case_id': case['case_id'], 'target_item_id': case['item_id']}


def parse_candidate(text):
    text = (text or '').strip().lstrip('#')
    if not text:
        return None
    if not text.isdigit() or int(text) < 1:
        raise ValueError('DUPLICATE_CANDIDATE must be an issue number or empty')
    return int(text)


def main(argv=None, client=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('issue', 'pr'))
    parser.add_argument('--event', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate', default='', help='Older issue number to compare, or empty')
    args = parser.parse_args(argv)

    event = json.loads(args.event.read_text(encoding='utf-8'))
    client = client or GitHubClient(os.environ.get('GITHUB_TOKEN', ''))
    if args.kind == 'issue':
        collection, note = collect_issue(event, args.repository, client, parse_candidate(args.candidate))
    else:
        collection, note = collect_pr(event, args.repository, client), None
    outputs = step_outputs(args.kind, write_bundles(collection, args.output))

    if note:
        print(f'::notice::{note}')
    print(json.dumps(outputs, ensure_ascii=False))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a', encoding='utf-8') as handle:
            handle.writelines(f'{key}={value}\n' for key, value in outputs.items())
    return 0


if __name__ == '__main__':
    sys.exit(main())
