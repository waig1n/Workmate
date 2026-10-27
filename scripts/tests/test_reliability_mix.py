from collections import Counter
from types import SimpleNamespace
from scripts.reliability.load_support.mix import case_for, fixture_kind


def test_fixed_twenty_task_ratio():
    assert Counter(case_for(i) for i in range(20))=={'text':8,'file':5,'skill':2,'mcp':1,'rag':2,'approval':2}
    assert case_for(20)==case_for(0)


def test_fixture_case_comes_from_driver_input():
    messages=[SimpleNamespace(content=[SimpleNamespace(text='fixture-case:mcp')])]
    assert fixture_kind(messages,'text')=='mcp'
    assert fixture_kind([],'text')=='text'
