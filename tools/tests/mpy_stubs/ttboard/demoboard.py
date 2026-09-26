import machine


class _Proj:
    def enable(self):
        machine.log.append(("enable",))


class _Shuttle:
    def __getattr__(self, name):
        machine.log.append(("select", name))
        return _Proj()


class _Port:
    value = 0


class DemoBoard:
    _inst = None

    def __init__(self):
        self.shuttle = _Shuttle()
        self.ui_in = _Port()
        self.uio_oe_pico = _Port()
        self.mode = 0

    @classmethod
    def get(cls):
        if cls._inst is None:
            cls._inst = DemoBoard()
        return cls._inst

    def clock_project_PWM(self, hz):
        machine.log.append(("clock", hz))

    def reset_project(self, v):
        machine.log.append(("reset", v))
