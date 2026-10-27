from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release.configure_package_sources import configure


def test_build_sources_preserve_distribution_and_signatures(tmp_path):
    path=tmp_path/'etc/apt/sources.list.d/debian.sources'
    path.parent.mkdir(parents=True)
    path.write_text('Types: deb\nURIs: http://deb.debian.org/debian\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /keyring\n\nTypes: deb\nURIs: http://deb.debian.org/debian-security\nSuites: bookworm-security\nComponents: main\nSigned-By: /keyring\n')
    configure(tmp_path)
    result=path.read_text()
    assert 'https://mirrors.tuna.tsinghua.edu.cn/debian\n' in result
    assert 'https://mirrors.tuna.tsinghua.edu.cn/debian-security\n' in result
    assert 'Suites: bookworm bookworm-updates' in result and result.count('Signed-By: /keyring')==2
    for name in ['etc/pip.conf','usr/local/pip.conf']:
        assert 'index-url = https://pypi.tuna.tsinghua.edu.cn/simple' in (tmp_path/name).read_text()


def test_missing_debian_sources_fail_before_writing(tmp_path):
    with pytest.raises(ValueError,match='debian.sources'):
        configure(tmp_path)
    assert not (tmp_path/'etc/pip.conf').exists()


def test_pip_override(tmp_path):
    source=tmp_path/'etc/apt/sources.list.d/debian.sources'
    source.parent.mkdir(parents=True)
    source.write_text('URIs: http://deb.debian.org/debian\nSuites: trixie\n')
    configure(tmp_path,'https://packages.example.org/simple')
    assert 'https://packages.example.org/simple' in (tmp_path/'usr/local/pip.conf').read_text()
