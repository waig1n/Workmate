import pytest
from scripts.reliability.load import parse_args,plan

BASE=['--gateway-image','g','--runtime-image','r']


@pytest.mark.parametrize('options',[
    ['--duration-seconds','259201'],['--arrival-rate','nan'],
    ['--cooldown-seconds','1800'],['--burst-rate','2'],['--reuse-conversations'],
    ['--remote-worker','operator@example.test','--workload','mixed'],
    ['--remote-worker','operator@example.test','--capacity-per-worker','4'],
])
def test_unsafe_or_unsupported_modes_fail_before_execution(options):
    with pytest.raises(SystemExit):parse_args(BASE+options)


def test_same_slot_comparison_preserves_total_capacity():
    split=plan(parse_args(BASE))
    single=plan(parse_args(BASE+['--workers','1','--capacity-per-worker','4']))
    assert split['workers']*split['capacity_per_worker']==single['workers']*single['capacity_per_worker']==4


def test_continuous_plan_freezes_reuse_peak_and_cooldown():
    p=plan(parse_args(BASE+['--duration-seconds','86400','--arrival-rate','0.055',
        '--burst-rate','0.08','--reuse-conversations','--cooldown-seconds','1800']))
    assert p['duration_seconds']==86400 and p['cooldown_seconds']==1800
    assert p['conversation_turn_limit']==2 and p['reuse_conversations']
