"""Keep strategy durations separate from calendar timestamps."""
import math

class ClockDiscontinuity(Exception):
    """Stop live collection after suspend or an untrusted calendar-clock change."""

class RecordingClockGuard:
    def __init__(self, clock):
        self.clock = clock
        self.wall, self.elapsed = clock.time(), clock.monotonic()

    def check(self):
        wall, elapsed = self.clock.time(), self.clock.monotonic()
        difference = (wall-self.wall)-(elapsed-self.elapsed)
        self.wall, self.elapsed = wall, elapsed
        if abs(difference) > .1:
            raise ClockDiscontinuity(f'Wall/monotonic clock divergence: {difference:.6f}s; possible sleep or clock adjustment')

class EventClock:
    def __init__(self):
        self.mode = None
        self.last_wall = None
        self.last_strategy = None
        self.discontinuities = []

    def observe(self, row):
        wall = row.get('received_at', row.get('at'))
        elapsed = row.get('elapsed_s')
        mode = 'monotonic' if elapsed is not None else 'legacy_wall'
        strategy = elapsed if elapsed is not None else wall
        for value in (wall, strategy):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('Invalid event clock')
        if mode == 'monotonic' and strategy < 0:
            raise ValueError('Monotonic elapsed time must be nonnegative')
        if self.mode is not None and mode != self.mode:
            raise ValueError('Cannot mix monotonic and wall-only event clocks in one recording')
        if self.last_strategy is not None:
            if strategy < self.last_strategy:
                raise ValueError('Strategy event times must be chronological')
            if mode == 'monotonic':
                difference = (wall-self.last_wall)-(strategy-self.last_strategy)
                if abs(difference) > .1:
                    self.discontinuities.append({'sequence': row.get('sequence'),
                                                 'difference_seconds': round(difference, 6)})
        self.mode = mode
        self.last_wall, self.last_strategy = wall, strategy
        return wall, strategy
