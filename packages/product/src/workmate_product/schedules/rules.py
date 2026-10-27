"""Bounded daily/weekly calendar calculation with explicit timezone semantics."""
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class OnceRule(StrictModel):
    kind: Literal['once']
    run_at: AwareDatetime


class DailyRule(StrictModel):
    kind: Literal['daily']
    time: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')


class WeeklyRule(StrictModel):
    kind: Literal['weekly']
    time: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    weekday: int = Field(ge=0, le=6, strict=True)


ScheduleRule = Annotated[OnceRule | DailyRule | WeeklyRule, Field(discriminator='kind')]


class SchedulePreview(StrictModel):
    timezone: str = Field(min_length=1, max_length=64)
    rule: ScheduleRule

    @field_validator('timezone')
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError('请选择有效的 IANA 时区') from error
        return value


class ScheduleNotification(StrictModel):
    binding_id: str = Field(min_length=1, max_length=36)
    on_success: bool = Field(default=True, strict=True)
    on_failure: bool = Field(default=True, strict=True)

    @model_validator(mode='after')
    def at_least_one_event(self):
        if not self.on_success and not self.on_failure:
            raise ValueError('请至少选择一种通知事件')
        return self


class ScheduleInput(SchedulePreview):
    notification: ScheduleNotification | None = None
    name: str = Field(min_length=1, max_length=80)
    agent_id: str = Field(min_length=1, max_length=36)
    prompt: str = Field(min_length=1, max_length=16000)
    model_profile: str | None = Field(default=None, min_length=1, max_length=64)


class ScheduleUpdate(ScheduleInput):
    version: int = Field(ge=1, strict=True)


class ScheduleVersion(StrictModel):
    version: int = Field(ge=1, strict=True)


def next_fire(schedule: SchedulePreview, after: datetime) -> datetime | None:
    """Strictly future UTC instant; nonexistent wall times skip, folds choose first."""
    if after.tzinfo is None:
        raise ValueError('Calendar calculation requires an aware timestamp')
    rule = schedule.rule
    if rule.kind == 'once':
        value = rule.run_at.astimezone(timezone.utc)
        return value if value > after else None
    zone = ZoneInfo(schedule.timezone)
    start = after.astimezone(zone).date()
    wall_time = time.fromisoformat(rule.time)
    # Daily or weekly rules always have another occurrence within two weeks,
    # including a missing DST day or a historic whole-day timezone jump.
    for offset in range(15):
        day = start + timedelta(days=offset)
        if rule.kind == 'weekly' and day.weekday() != rule.weekday:
            continue
        local = datetime.combine(day, wall_time, tzinfo=zone).replace(fold=0)
        utc = local.astimezone(timezone.utc)
        roundtrip = utc.astimezone(zone)
        if roundtrip.replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if utc > after:
            return utc
    raise ValueError('No valid scheduled time within two weeks')


def preview(schedule: SchedulePreview, after: datetime, count: int = 3) -> list[datetime]:
    result = []
    for _ in range(min(count, 10)):
        value = next_fire(schedule, after)
        if value is None:
            break
        result.append(value)
        after = value
    return result


def missed_slots(schedule: SchedulePreview, first: datetime, through: datetime) -> int:
    """Count nominal calendar slots for a summarized outage, without replaying them.

    The count is explicitly a calendar-slot count; DST nonexistent slots may be
    included in a long outage summary. Actual runs always use next_fire().
    """
    if schedule.rule.kind == 'once':
        return 1
    zone = ZoneInfo(schedule.timezone)
    start, end = first.astimezone(zone), through.astimezone(zone)
    days = (end.date() - start.date()).days
    if end.time().replace(tzinfo=None) < time.fromisoformat(schedule.rule.time):
        days -= 1
    return max(1, days // (7 if schedule.rule.kind == 'weekly' else 1) + 1)
