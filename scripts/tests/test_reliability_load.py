"""Load acceptance must fail closed on incomplete/duplicated delivery."""
import copy
import pytest
from scripts.reliability.load_results import validate_report


def report():
    return {'expected_runs': 1, 'runs': [{'expected_tasks': 1, 'correct': True,
        'budget_pass': True, 'tasks': [{'id': 't', 'terminal': True, 'effect_count': 1}],
        'unfinished_commands': [], 'command_outbox': [{'status': 'settled', 'count': 1}],
        'business_outbox': [{'status': 'published', 'count': 1}],
        'pel': [{'group': {'pending': 0, 'lag': 0}}]}]}


def test_accepts_complete_report():
    assert validate_report(report()) == []


@pytest.mark.parametrize('mutation', [
    lambda r: r['runs'][0]['tasks'][0].update(terminal=False),
    lambda r: r['runs'][0]['tasks'][0].update(effect_count=2),
    lambda r: r['runs'][0]['tasks'].append(copy.deepcopy(r['runs'][0]['tasks'][0])),
    lambda r: r['runs'][0]['pel'][0]['group'].update(pending=1),
    lambda r: r['runs'][0]['pel'][0]['group'].update(lag=1),
    lambda r: r['runs'][0]['pel'][0]['group'].update(lag=None),
    lambda r: r['runs'][0]['command_outbox'][0].update(status='published'),
    lambda r: r['runs'][0]['business_outbox'][0].update(status='pending'),
    lambda r: r['runs'][0].update(budget_pass=False),
    lambda r: r.update(runs=[]),
    lambda r: r['runs'][0].update(unfinished_commands=[{'status': 'claimed'}]),
])
def test_rejects_incomplete_or_duplicate_acceptance(mutation):
    value = report()
    mutation(value)
    assert validate_report(value)


@pytest.mark.parametrize('value', [None, {}, [], {'runs': []}])
def test_rejects_missing_report(value):
    assert validate_report(value)


def test_file_workload_requires_independent_file_evidence():
    value=report();value['workload']='file'
    assert validate_report(value)
    value['runs'][0]['tasks'][0]['file_evidence']={'write_attempt_count':1,
        'download_bytes':1024,'download_sha256':'a'*64}
    assert validate_report(value)==[]
    value['runs'][0]['tasks'][0]['file_evidence']['write_attempt_count']=2
    assert validate_report(value)
