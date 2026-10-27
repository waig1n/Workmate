"""Calendar semantics are independent of browser clocks and server timezone."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from workmate_product.schedules.rules import ScheduleInput, SchedulePreview, next_fire, preview


def at(value):
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def definition(**changes):
    return ScheduleInput.model_validate(dict(name='Daily report', agent_id='a', prompt='Report',
        timezone='Asia/Shanghai', rule={'kind':'daily','time':'09:00'}, **changes))


def test_daily_uses_explicit_timezone_and_strict_future_boundary():
    schedule = definition()
    assert next_fire(schedule, at('2026-10-12T00:59:59Z')) == at('2026-10-12T01:00:00Z')
    assert next_fire(schedule, at('2026-10-12T01:00:00Z')) == at('2026-10-13T01:00:00Z')


def test_weekly_and_once_preview():
    weekly = SchedulePreview(timezone='Asia/Shanghai', rule={'kind':'weekly','time':'17:00','weekday':4})
    assert preview(weekly, at('2026-10-12T00:00:00Z')) == [at(f'2026-10-{day}T09:00:00Z') for day in (16,23,30)]
    once = SchedulePreview(timezone='Asia/Shanghai', rule={'kind':'once','run_at':'2026-10-13T09:00:00+08:00'})
    assert preview(once, at('2026-10-12T00:00:00Z')) == [at('2026-10-13T01:00:00Z')]
    assert next_fire(once, at('2026-10-13T01:00:00Z')) is None


def test_dst_gap_skips_and_fold_runs_only_first_occurrence():
    gap = SchedulePreview(timezone='America/New_York', rule={'kind':'daily','time':'02:30'})
    assert next_fire(gap, at('2026-03-08T05:00:00Z')) == at('2026-03-09T06:30:00Z')
    fold = SchedulePreview(timezone='America/New_York', rule={'kind':'daily','time':'01:30'})
    assert next_fire(fold, at('2026-11-01T04:00:00Z')) == at('2026-11-01T05:30:00Z')
    assert next_fire(fold, at('2026-11-01T05:30:00Z')) == at('2026-11-02T06:30:00Z')


@pytest.mark.parametrize('change', [
    {'timezone':'Not/A_Zone'}, {'timezone':'../UTC'}, {'timezone':''},
    {'rule':{'kind':'cron','time':'09:00'}}, {'rule':{'kind':'daily','time':'25:00'}},
    {'rule':{'kind':'daily','time':'9:00'}}, {'rule':{'kind':'weekly','time':'09:00','weekday':7}},
    {'rule':{'kind':'weekly','time':'09:00','weekday':True}},
    {'rule':{'kind':'once','run_at':'2026-10-12T09:00:00'}},
    {'rule':{'kind':'daily','time':'09:00','run_at':'2026-10-12T09:00:00Z'}},
    {'owner_user_id':'admin'}, {'approval_profile':'full_access'}, {'name':'  '}, {'prompt':' '},
])
def test_invalid_definition_and_injected_authority_are_rejected(change):
    values = dict(name='Report', agent_id='a', prompt='Report', timezone='Asia/Shanghai',
        rule={'kind':'daily','time':'09:00'})
    with pytest.raises(ValidationError):
        ScheduleInput.model_validate({**values, **change})

def test_notification_defaults_and_validation():
    assert definition().notification is None
    assert definition(notification={'binding_id':'binding'}).notification.model_dump() == {
        'binding_id':'binding','on_success':True,'on_failure':True}
    for value in ({'binding_id':''}, {'binding_id':'b','on_success':False,'on_failure':False},
                  {'binding_id':'b','channel_id':'forged'}, {'binding_id':'b','on_success':'false'}):
        with pytest.raises(ValidationError):
            definition(notification=value)
