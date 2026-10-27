#!/usr/bin/env python3
"""Initialize local private buckets without starting application roles."""
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import yaml
import init_knowledge_bucket as bucket


def knowledge_enabled(path):
    config = yaml.safe_load(Path(path).read_text())
    enabled = config.get('product', {}).get('knowledge', {}).get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('knowledge.enabled must be boolean')
    return enabled


def initialize_storage(environment, enabled, artifacts=False):
    settings = []
    prefixes = ['SKILL'] + (['KNOWLEDGE'] if enabled else []) + (['ARTIFACT'] if artifacts else [])
    for prefix in prefixes:
        values = {'KNOWLEDGE_S3_' + key: environment.get(prefix + '_S3_' + key, '')
                  for key in ('ENDPOINT', 'BUCKET', 'ACCESS_KEY', 'SECRET_KEY')}
        settings.append((prefix, bucket.Settings.from_environment(values)))
    locations = [(item.endpoint.rstrip('/'), item.bucket) for _, item in settings]
    if len(set(locations)) != len(locations):
        raise bucket.InitializationError('private_bucket_overlap')
    for prefix, setting in settings:
        if urlsplit(setting.endpoint).hostname != 'rustfs':
            print(f'{prefix}: external bucket must be provisioned privately; no changes made')
            continue
        client = bucket.make_client(setting)
        try:
            bucket.initialize(client, setting.bucket)
        finally:
            client.close()
        print(f'{prefix}: local private bucket initialized')


def main():
    try:
        enabled = knowledge_enabled(os.environ.get('WORKMATE_CONFIG_FILE', '/app/config/workmate.yaml'))
        config = yaml.safe_load(Path(os.environ.get('WORKMATE_CONFIG_FILE', '/app/config/workmate.yaml')).read_text())
        backend = os.environ.get('ARTIFACT_STORAGE_BACKEND') or config.get('product', {}).get('artifacts', {}).get('storage', {}).get('backend', 'local')
        if backend not in {'local', 's3'}:
            raise ValueError('Invalid artifact storage backend')
        tool_content = os.environ.get('ARTIFACT_STORAGE_TOOL_CONTENT_ENABLED')
        if tool_content is None:
            tool_content = config.get('product', {}).get('artifacts', {}).get('storage', {}).get('tool_content_enabled', False)
        elif tool_content.lower() in {'true', '1'}:
            tool_content = True
        elif tool_content.lower() in {'false', '0'}:
            tool_content = False
        if not isinstance(tool_content, bool):
            raise ValueError('Invalid tool content policy')
        initialize_storage(os.environ, enabled, artifacts=backend == 's3' or tool_content)
        return 0
    except bucket.InitializationError as exc:
        print('ERROR ' + str(exc), file=sys.stderr)
    except Exception:
        print('ERROR storage initialization failed; check configuration and storage availability', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
