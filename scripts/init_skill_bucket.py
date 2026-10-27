#!/usr/bin/env python3
"""Initialize only Compose's local Skill bucket; external buckets are provisioned separately."""
import sys
from urllib.parse import urlsplit
import init_knowledge_bucket as bucket


def main():
    try:
        settings = bucket.Settings.from_environment()
    except bucket.InitializationError:
        print('ERROR Skill bucket initialization configuration is invalid', file=sys.stderr)
        return 1
    if urlsplit(settings.endpoint).hostname != 'rustfs':
        print('External Skill bucket must be provisioned privately; no bucket policy modified')
        return 0
    return bucket.main()


if __name__ == '__main__':
    sys.exit(main())
