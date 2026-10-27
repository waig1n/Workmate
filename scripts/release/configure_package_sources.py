"""Set Debian and Python mirrors while building Workmate Python images."""
from __future__ import annotations

import argparse
import configparser
from pathlib import Path
import re

PIP_INDEX = 'https://pypi.tuna.tsinghua.edu.cn/simple'


def configure(root=Path('/'), pip_index=PIP_INDEX):
    source = root / 'etc/apt/sources.list.d/debian.sources'
    if not source.is_file():
        raise ValueError('Workmate Python base image must provide Debian debian.sources')
    # Preserve Suites, Components and Signed-By; rewrite archive addresses only.
    content = source.read_text(encoding='utf-8')
    content = re.sub(r'^URIs: https?://deb.debian.org/debian-security[ \t]*$',
                     'URIs: https://mirrors.tuna.tsinghua.edu.cn/debian-security', content, flags=re.MULTILINE)
    content = re.sub(r'^URIs: https?://deb.debian.org/debian[ \t]*$',
                     'URIs: https://mirrors.tuna.tsinghua.edu.cn/debian', content, flags=re.MULTILINE)
    source.write_text(content, encoding='utf-8')
    for relative in ('etc/pip.conf', 'usr/local/pip.conf'):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.RawConfigParser()
        if path.exists():
            config.read(path)
        if not config.has_section('global'):
            config.add_section('global')
        config.set('global', 'index-url', pip_index)
        config.remove_option('global', 'extra-index-url')
        with path.open('w', encoding='utf-8') as stream:
            config.write(stream)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pip-index-url', default=PIP_INDEX)
    configure(pip_index=parser.parse_args().pip_index_url)
