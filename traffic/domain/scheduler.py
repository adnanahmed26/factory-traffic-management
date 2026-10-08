from dataclasses import dataclass
from datetime import datetime

from django.utils import timezone

from traffic.models import (
    Direction,
    TrafficPhase,
    VehicleType,
)


@dataclass
class PhaseScore:
    phase: str
    queue_count: int
    vehicle_priority_score: float
    waiting_time_score: float
    starvation_bonus: float
    total_score: float
    oldest_wait_seconds: float
    starved: bool


@dataclass
class SchedulingDecision:
    selected_phase: str | None
    north_south: PhaseScore
    east_west: PhaseScore
    reason: str


class TrafficScheduler:
    """
    Determines which traffic phase should be served next.

    This scheduler does NOT change signal states directly.
    It only chooses a phase.

    Safe signal transitions are handled separately by
    TrafficStateMachine.
    """

    # ---------------------------------------------------------
    # Scheduling configuration
    # ---------------------------------------------------------

    QUEUE_WEIGHT = 10

    VEHICLE_PRIORITY = {
        VehicleType.EMERGENCY: 100,
        VehicleType.TRUCK: 30,
        VehicleType.FORKLIFT: 20,
        VehicleType.EMPLOYEE_VEHICLE: 10,
    }

    # Adds 1 scheduling point per 10 seconds of oldest wait time.
    WAITING_TIME_DIVISOR = 10

    # Added when a phase exceeds max_wait_seconds.
    STARVATION_BONUS = 100

    # Prevent switching when another phase only has a tiny advantage.
    SWITCH_THRESHOLD = 15

    PHASE_DIRECTIONS = {
        TrafficPhase.NORTH_SOUTH: {
            Direction.NORTH,
            Direction.SOUTH,
        },
        TrafficPhase.EAST_WEST: {
            Direction.EAST,
            Direction.WEST,
        },
    }

    # ---------------------------------------------------------
    # Phase scoring
    # ---------------------------------------------------------

    @classmethod
    def calculate_phase_score(
        cls,
        *,
        phase: str,
        queue_entries,
        max_wait_seconds: int,
        now: datetime | None = None,
    ) -> PhaseScore:
        if now is None:
            now = timezone.now()

        directions = cls.PHASE_DIRECTIONS[phase]

        entries = [
            entry
            for entry in queue_entries
            if entry.direction in directions
        ]

        queue_count = len(entries)

        # No vehicles waiting for this phase.
        if queue_count == 0:
            return PhaseScore(
                phase=phase,
                queue_count=0,
                vehicle_priority_score=0,
                waiting_time_score=0,
                starvation_bonus=0,
                total_score=0,
                oldest_wait_seconds=0,
                starved=False,
            )

        # -----------------------------------------------------
        # Vehicle-priority score
        # -----------------------------------------------------

        vehicle_priority_score = sum(
            cls.VEHICLE_PRIORITY.get(
                entry.vehicle_type,
                0,
            )
            for entry in entries
        )

        # -----------------------------------------------------
        # Waiting-time score
        # -----------------------------------------------------

        wait_times = []

        for entry in entries:
            wait_seconds = (
                now - entry.arrived_at
            ).total_seconds()

            # Protect against malformed/future timestamps.
            wait_seconds = max(
                0,
                wait_seconds,
            )

            wait_times.append(wait_seconds)

        oldest_wait_seconds = max(wait_times)

        waiting_time_score = (
            oldest_wait_seconds
            / cls.WAITING_TIME_DIVISOR
        )

        # -----------------------------------------------------
        # Starvation detection
        # -----------------------------------------------------

        starved = (
            oldest_wait_seconds
            >= max_wait_seconds
        )

        starvation_bonus = (
            cls.STARVATION_BONUS
            if starved
            else 0
        )

        # -----------------------------------------------------
        # Final weighted score
        # -----------------------------------------------------

        total_score = (
            queue_count * cls.QUEUE_WEIGHT
            + vehicle_priority_score
            + waiting_time_score
            + starvation_bonus
        )

        return PhaseScore(
            phase=phase,
            queue_count=queue_count,
            vehicle_priority_score=(
                vehicle_priority_score
            ),
            waiting_time_score=round(
                waiting_time_score,
                2,
            ),
            starvation_bonus=(
                starvation_bonus
            ),
            total_score=round(
                total_score,
                2,
            ),
            oldest_wait_seconds=round(
                oldest_wait_seconds,
                2,
            ),
            starved=starved,
        )

    # ---------------------------------------------------------
    # Scheduling decision
    # ---------------------------------------------------------

    @classmethod
    def choose_phase(
        cls,
        *,
        queue_entries,
        current_phase: str,
        max_wait_seconds: int,
        now: datetime | None = None,
    ) -> SchedulingDecision:
        if now is None:
            now = timezone.now()

        entries = list(queue_entries)

        north_south = cls.calculate_phase_score(
            phase=TrafficPhase.NORTH_SOUTH,
            queue_entries=entries,
            max_wait_seconds=max_wait_seconds,
            now=now,
        )

        east_west = cls.calculate_phase_score(
            phase=TrafficPhase.EAST_WEST,
            queue_entries=entries,
            max_wait_seconds=max_wait_seconds,
            now=now,
        )

        # -----------------------------------------------------
        # 1. No waiting traffic
        # -----------------------------------------------------

        if (
            north_south.queue_count == 0
            and east_west.queue_count == 0
        ):
            return SchedulingDecision(
                selected_phase=None,
                north_south=north_south,
                east_west=east_west,
                reason="No waiting traffic.",
            )

        # -----------------------------------------------------
        # 2. Starvation protection
        # -----------------------------------------------------

        # Only NORTH/SOUTH is starved.
        if (
            north_south.starved
            and not east_west.starved
        ):
            return SchedulingDecision(
                selected_phase=TrafficPhase.NORTH_SOUTH,
                north_south=north_south,
                east_west=east_west,
                reason=(
                    "NORTH_SOUTH selected due to "
                    "starvation protection."
                ),
            )

        # Only EAST/WEST is starved.
        if (
            east_west.starved
            and not north_south.starved
        ):
            return SchedulingDecision(
                selected_phase=TrafficPhase.EAST_WEST,
                north_south=north_south,
                east_west=east_west,
                reason=(
                    "EAST_WEST selected due to "
                    "starvation protection."
                ),
            )

        # Both phases are starved.
        #
        # When both have exceeded the starvation threshold,
        # serve the phase containing the vehicle that has
        # waited the longest.
        if (
            north_south.starved
            and east_west.starved
        ):
            if (
                north_south.oldest_wait_seconds
                > east_west.oldest_wait_seconds
            ):
                return SchedulingDecision(
                    selected_phase=(
                        TrafficPhase.NORTH_SOUTH
                    ),
                    north_south=north_south,
                    east_west=east_west,
                    reason=(
                        "Both phases are starved; "
                        "NORTH_SOUTH selected because "
                        "it contains the longest-waiting "
                        "vehicle."
                    ),
                )

            if (
                east_west.oldest_wait_seconds
                > north_south.oldest_wait_seconds
            ):
                return SchedulingDecision(
                    selected_phase=(
                        TrafficPhase.EAST_WEST
                    ),
                    north_south=north_south,
                    east_west=east_west,
                    reason=(
                        "Both phases are starved; "
                        "EAST_WEST selected because "
                        "it contains the longest-waiting "
                        "vehicle."
                    ),
                )

            # Exact starvation-age tie:
            # keep the current phase when possible.
            if current_phase in {
                TrafficPhase.NORTH_SOUTH,
                TrafficPhase.EAST_WEST,
            }:
                selected_phase = current_phase
            else:
                selected_phase = (
                    TrafficPhase.NORTH_SOUTH
                )

            return SchedulingDecision(
                selected_phase=selected_phase,
                north_south=north_south,
                east_west=east_west,
                reason=(
                    "Both phases are equally starved; "
                    "current phase retained where possible."
                ),
            )

        # -----------------------------------------------------
        # 3. Normal weighted scheduling
        # -----------------------------------------------------

        if (
            north_south.total_score
            > east_west.total_score
        ):
            candidate = TrafficPhase.NORTH_SOUTH
            candidate_score = (
                north_south.total_score
            )

        elif (
            east_west.total_score
            > north_south.total_score
        ):
            candidate = TrafficPhase.EAST_WEST
            candidate_score = (
                east_west.total_score
            )

        else:
            # Exact score tie:
            # keep current phase to avoid unnecessary switching.
            if current_phase in {
                TrafficPhase.NORTH_SOUTH,
                TrafficPhase.EAST_WEST,
            }:
                candidate = current_phase

            else:
                candidate = (
                    TrafficPhase.NORTH_SOUTH
                )

            return SchedulingDecision(
                selected_phase=candidate,
                north_south=north_south,
                east_west=east_west,
                reason=(
                    "Scores tied; current phase retained "
                    "where possible."
                ),
            )

        # -----------------------------------------------------
        # 4. Avoid unnecessary switching
        # -----------------------------------------------------

        if current_phase in {
            TrafficPhase.NORTH_SOUTH,
            TrafficPhase.EAST_WEST,
        }:
            if (
                current_phase
                == TrafficPhase.NORTH_SOUTH
            ):
                current_score = (
                    north_south.total_score
                )
                current_queue = (
                    north_south.queue_count
                )

            else:
                current_score = (
                    east_west.total_score
                )
                current_queue = (
                    east_west.queue_count
                )

            candidate_details = (
                north_south
                if candidate
                == TrafficPhase.NORTH_SOUTH
                else east_west
            )

            score_advantage = (
                candidate_score
                - current_score
            )

            # Keep serving the current phase if:
            # - it still has traffic,
            # - the candidate is not starved,
            # - the score advantage is small.
            if (
                candidate != current_phase
                and current_queue > 0
                and not candidate_details.starved
                and score_advantage
                < cls.SWITCH_THRESHOLD
            ):
                return SchedulingDecision(
                    selected_phase=current_phase,
                    north_south=north_south,
                    east_west=east_west,
                    reason=(
                        "Current phase retained to avoid "
                        "an unnecessary switch."
                    ),
                )

        # -----------------------------------------------------
        # 5. Highest valid candidate wins
        # -----------------------------------------------------

        return SchedulingDecision(
            selected_phase=candidate,
            north_south=north_south,
            east_west=east_west,
            reason=(
                f"{candidate} selected with "
                f"the highest scheduling score."
            ),
        )