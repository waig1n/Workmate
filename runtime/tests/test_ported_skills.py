"""Import contract and deterministic helpers for adapted Skill source packages."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
from workmate_product.application.skill_artifacts import parse_skill_archive

from workmate_product.skills.catalog import builtin_root

ROOT = builtin_root()


def load(path):
    spec = importlib.util.spec_from_file_location(path.parent.name + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('source', sorted(ROOT.glob('*/SKILL.md')), ids=lambda p: p.parent.name)
def test_each_package_roundtrips_through_upload_parser(source, tmp_path):
    packager = load(Path(__file__).resolve().parents[1] / 'scripts/package_builtin_skills.py')
    package = packager.package(source.parent, tmp_path)
    metadata = {"name": parse_skill_archive(package.read_bytes()).name}
    assert metadata['name'] == source.parent.name
    with zipfile.ZipFile(package) as archive:
        assert sum(n.endswith('/SKILL.md') for n in archive.namelist()) == 1
        assert not any('/evals/' in n for n in archive.namelist())
        for notice in source.parent.iterdir():
            if notice.name.startswith(('LICENSE', 'NOTICE')):
                entry = f'{source.parent.name}/{notice.name}'
                assert archive.read(entry) == notice.read_bytes()


def test_packager_rejects_nested_skill_and_symlink(tmp_path):
    package = load(Path(__file__).resolve().parents[1] / 'scripts/package_builtin_skills.py').package
    source = tmp_path / 'example'
    source.mkdir()
    (source / 'SKILL.md').write_text('---\nname: example\ndescription: Example\n---\n')
    nested = source / 'other'
    nested.mkdir()
    (nested / 'SKILL.md').write_text('nested')
    with pytest.raises(ValueError, match='Nested'):
        package(source, tmp_path / 'dist')
    (nested / 'SKILL.md').unlink()
    (source / 'link').symlink_to('/etc/passwd')
    with pytest.raises(ValueError, match='Symlink'):
        package(source, tmp_path / 'dist')


def test_data_csv_xlsx_and_failure_exit(tmp_path, monkeypatch):
    pytest.importorskip('duckdb')
    openpyxl = pytest.importorskip('openpyxl')
    monkeypatch.chdir(tmp_path)
    script = ROOT / 'data-analysis/scripts/analyze.py'
    csv = tmp_path / "O'Reilly.csv"
    csv.write_text('amount,label\n2,a\n3,b\n')
    result = subprocess.run([sys.executable, str(script), '--files', str(csv), '--action', 'query', '--sql', 'SELECT sum(amount) AS total FROM "O_Reilly"', '--output-file', 'output/sum.json'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'output/sum.json').read_text()) == [{'total': 5}]
    wb = openpyxl.Workbook()
    wb.active.append(['amount', 'label'])
    wb.active.append([7, 'x'])
    wb.save(tmp_path / 'book.xlsx')
    result = subprocess.run([sys.executable, str(script), '--files', 'book.xlsx', '--action', 'query', '--sql', 'SELECT sum(amount) AS total FROM "Sheet"', '--output-file', 'output/excel.json'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads((tmp_path / 'output/excel.json').read_text()) == [{'total': 7}]
    result = subprocess.run([sys.executable, str(script), '--files', str(csv), '--action', 'query', '--sql', 'SELECT nonexistent FROM "O_Reilly"'], capture_output=True)
    assert result.returncode != 0
    assert (tmp_path / 'working/.data-analysis-cache').is_dir()


def test_ppt_editable_text_and_missing_image(tmp_path):
    pptx = pytest.importorskip('pptx')
    helper = load(ROOT / 'ppt-generation/scripts/generate.py')
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'slides': [{'title': 'Title', 'key_points': ['Editable text']}]}))
    output = tmp_path / 'output/deck.pptx'
    helper.generate_ppt(str(plan), [], str(output))
    deck = pptx.Presentation(output)
    assert len(deck.slides) == 1
    assert 'Editable text' in deck.slides[0].placeholders[1].text
    with pytest.raises(FileNotFoundError):
        helper.generate_ppt(str(plan), ['missing.png'], str(tmp_path / 'missing.pptx'))
    assert not (tmp_path / 'missing.pptx').exists()


@pytest.mark.parametrize('kind', ['bar', 'column', 'line', 'area', 'pie', 'scatter'])
def test_local_chart_produces_file_without_network(kind, tmp_path):
    pytest.importorskip('matplotlib')
    helper = load(ROOT / 'chart-visualization/scripts/generate.py')
    data = [{'category': 'A', 'time': 'Jan', 'value': 2, 'x': 1, 'y': 2}, {'category': 'B', 'time': 'Feb', 'value': 4, 'x': 2, 'y': 4}]
    output = tmp_path / f'{kind}.svg'
    helper.generate({'tool': f'generate_{kind}_chart', 'args': {'data': data}}, output)
    assert '<svg' in output.read_text()
    with pytest.raises(ValueError, match='Unsupported'):
        helper.generate({'tool': 'generate_pin_map'}, output)


def test_arxiv_parser_uses_https_and_preserves_metadata(monkeypatch):
    helper = load(ROOT / 'systematic-literature-review/scripts/arxiv_search.py')
    class Response:
        text = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/1234.5678v2</id><title>Example</title><summary>Abstract</summary><author><name>Author</name></author><published>2026-01-01T00:00:00Z</published></entry></feed>'
        def raise_for_status(self):
            pass
    def get(url, **kwargs):
        assert url.startswith('https://')
        assert kwargs['params']['max_results'] == 50
        return Response()
    monkeypatch.setattr(helper.requests, 'get', get)
    papers = helper.search('test topic', max_results=100)
    assert papers[0]['title'] == 'Example'
    assert papers[0]['abstract'] == 'Abstract'
