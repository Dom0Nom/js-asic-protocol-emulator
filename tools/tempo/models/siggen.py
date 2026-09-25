"""Signal sources for logic-analyser and trigger tests."""


class Square:
    """Square wave on `net`: `high` cycles high, `low` cycles low, from `start`."""

    def __init__(self, net, high, low, start=0, level=0):
        self.net, self.high, self.low, self.start = net, high, low, start
        self.level = level
        self.edges = []  # (cycle, new level)
        self.next = start

    def step(self, cycle, nets):
        if cycle >= self.next:
            self.level ^= 1
            self.edges.append((cycle, self.level))
            self.next = cycle + (self.high if self.level else self.low)
        return {self.net: self.level}


class Pulses:
    """Level changes at given absolute cycles (idle `level`)."""

    def __init__(self, net, at, level=0):
        self.net = net
        self.at = sorted(at)
        self.level = level
        self.edges = []

    def step(self, cycle, nets):
        while self.at and cycle >= self.at[0]:
            self.at.pop(0)
            self.level ^= 1
            self.edges.append((cycle, self.level))
        return {self.net: self.level}
