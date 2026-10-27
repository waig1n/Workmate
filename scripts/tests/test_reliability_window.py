import pytest
from scripts.reliability.load_support.window import Window


def test_window_never_counts_restarts_as_continuous():
    w=Window(10,2,now=100)
    assert w.admit(100)
    assert not w.admit(110)
    assert not w.result(109)['passed']
    assert w.result(110)['passed']


def test_circuit_breaker_is_sticky():
    w=Window(10,2,now=0)
    w.fail('side effect')
    assert not w.admit(1)
    assert not w.result(11)['passed']


@pytest.mark.parametrize('duration,rate',[(0,1),(-1,1),(259201,1),(1,0),(1,101)])
def test_invalid_window(duration,rate):
    with pytest.raises(ValueError):Window(duration,rate)


def test_arrival_schedule_never_catches_up_bursts():
    w=Window(10,2,now=0)
    assert w.delay(0)==0
    w.sent(0)
    assert w.delay(.1)==pytest.approx(.4)
    w.sent(3)
    assert w.delay(3)==.5


def test_six_hour_peak_lasts_ten_minutes():
    w=Window(86400,1,now=0,burst_rate=2)
    w.sent(21599);assert w.delay(21599)==1
    w.sent(21600);assert w.delay(21600)==.5
    w.sent(22200);assert w.delay(22200)==1
