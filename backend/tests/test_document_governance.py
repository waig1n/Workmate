"""Validate the public bilingual documentation contract without external services."""
from pathlib import Path
import re
import tomllib
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).parents[2]


def public_documents():
    return [ROOT / name for name in ('README.md', 'README.zh-CN.md', 'AGENTS.md', 'CLAUDE.md', 'scripts/reliability/load_support/README.md', 'runtime/tests/fixtures/core_state_v1/README.md', 'frontend/public/README.md')] + list((ROOT / 'docs').rglob('*.md')) + list((ROOT / 'deploy').glob('*.md'))


def test_language_trees_match_and_have_language_navigation():
    english = {p.name for p in (ROOT / 'docs/en').glob('*.md')}
    chinese = {p.name for p in (ROOT / 'docs/zh-CN').glob('*.md')}
    assert {'README.md', 'getting-started.md', 'configuration.md', 'architecture.md', 'development.md', 'limitations.md'} <= english
    assert english == chinese
    for language, counterpart in (('en', 'zh-CN'), ('zh-CN', 'en')):
        for name in english:
            assert f'../{counterpart}/{name}' in (ROOT / 'docs' / language / name).read_text()


def test_public_document_relative_links_resolve():
    for document in public_documents():
        assert document.is_file(), str(document)
        text = re.sub(r'```.*?```', '', document.read_text(), flags=re.S)
        for link in re.findall(r'\]\(([^\s)]+)\)', text):
            target = urlsplit(link)
            if target.scheme or not target.path:
                continue
            resolved = (document.parent / unquote(target.path)).resolve()
            assert resolved.is_relative_to(ROOT), f'{document}: external local path {link}'
            assert resolved.exists(), f'{document}: broken link {link}'


def test_public_docs_do_not_contain_private_environment_or_process_records():
    forbidden = re.compile(r'/home/[A-Za-z0-9_.-]+/|本轮|用户确认已完成|求职训练|Q[1-4]验收|客户源码保护')
    for document in public_documents():
        assert not forbidden.search(document.read_text()), str(document)
    assert not (ROOT / 'docs/interview-handbook').exists()


def test_documented_agentscope_version_matches_dependency():
    project = tomllib.loads((ROOT / 'runtime/pyproject.toml').read_text())
    dependency = next(d for d in project['project']['dependencies'] if d.startswith('agentscope['))
    version = dependency.split('==')[1]
    for language in ('en', 'zh-CN'):
        assert version in (ROOT / 'docs' / language / 'architecture.md').read_text()


def test_release_configuration_entry_exists():
    # Existing release tooling includes this stable entry in its asset list.
    content = (ROOT / 'docs/configuration.md').read_text()
    assert 'en/configuration.md' in content
    assert 'zh-CN/configuration.md' in content


def test_paired_guides_keep_commands_and_identifiers_aligned():
    for english in (ROOT / 'docs/en').glob('*.md'):
        chinese = ROOT / 'docs/zh-CN' / english.name
        assert re.findall(r"```[^\n]*\n(.*?)```", english.read_text(), re.S) == re.findall(
            r"```[^\n]*\n(.*?)```", chinese.read_text(), re.S
        ), english.name


def test_configuration_reference_covers_current_yaml_and_environment_template():
    import yaml

    def leaf_paths(value, prefix=''):
        if isinstance(value, dict) and value:
            return set().union(*(leaf_paths(v, f'{prefix}.{k}' if prefix else k) for k, v in value.items()))
        if isinstance(value, list) and value:
            return set().union(*(leaf_paths(v, f'{prefix}[{i}]') for i, v in enumerate(value)))
        return {prefix}

    expected = leaf_paths(yaml.safe_load((ROOT / 'config/workmate.yaml').read_text()))
    environment = set(re.findall(r'^([A-Z][A-Z0-9_]*)=', (ROOT / '.env.example').read_text(), re.M))
    for language in ('en', 'zh-CN'):
        reference = (ROOT / 'docs' / language / 'configuration-reference.md').read_text()
        assert set(re.findall(r'^\| `([^`]+)` \|', reference, re.M)) == expected
        assert set(re.findall(r'^- `([A-Z][A-Z0-9_]*)`', reference, re.M)) == environment
