from traffic.models import (
    Direction,
    SignalColor,
    TrafficPhase,
)


class UnsafeSignalStateError(Exception):
    pass


class SafetyRules:
    PHASE_DIRECTIONS = {
        TrafficPhase.NORTH_SOUTH: {
            Direction.NORTH,
            Direction.SOUTH,
        },
        TrafficPhase.EAST_WEST: {
            Direction.EAST,
            Direction.WEST,
        },
        TrafficPhase.ALL_RED: set(),
    }

    @classmethod
    def directions_for_phase(cls, phase):
        return cls.PHASE_DIRECTIONS.get(phase, set())

    @classmethod
    def desired_states_for_phase(cls, phase):
        states = {
            Direction.NORTH: SignalColor.RED,
            Direction.SOUTH: SignalColor.RED,
            Direction.EAST: SignalColor.RED,
            Direction.WEST: SignalColor.RED,
        }

        for direction in cls.directions_for_phase(phase):
            states[direction] = SignalColor.GREEN

        return states

    @classmethod
    def yellow_states_for_phase(cls, phase):
        states = {
            Direction.NORTH: SignalColor.RED,
            Direction.SOUTH: SignalColor.RED,
            Direction.EAST: SignalColor.RED,
            Direction.WEST: SignalColor.RED,
        }

        for direction in cls.directions_for_phase(phase):
            states[direction] = SignalColor.YELLOW

        return states

    @classmethod
    def validate_no_conflicting_green(cls, states):
        ns_green = (
            states.get(Direction.NORTH) == SignalColor.GREEN
            or states.get(Direction.SOUTH) == SignalColor.GREEN
        )

        ew_green = (
            states.get(Direction.EAST) == SignalColor.GREEN
            or states.get(Direction.WEST) == SignalColor.GREEN
        )

        if ns_green and ew_green:
            raise UnsafeSignalStateError(
                "Conflicting phases cannot be GREEN simultaneously."
            )

        return True