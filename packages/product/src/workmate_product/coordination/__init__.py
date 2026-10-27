"""Lossy wakeups only; PostgreSQL remains the execution authority."""
from .signals import FALLBACK_SECONDS, Pulse, wait_for_wakeup
from .wakeups import WakeupBus
from .transactions import stage_wakeup

__all__ = ['FALLBACK_SECONDS', 'Pulse', 'WakeupBus', 'wait_for_wakeup', 'stage_wakeup']
