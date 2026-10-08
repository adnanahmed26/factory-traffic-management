from dataclasses import dataclass

from django.db import transaction

from traffic.domain.scheduler import (
    SchedulingDecision,
    TrafficScheduler,
)
from traffic.domain.state_machine import (
    InvalidTransitionError,
    TrafficStateMachine,
)
from traffic.models import (
    AuditEvent,
    Junction,
    JunctionMode,
    TrafficPhase,
    TransitionStage,
)


class AutomaticControlError(Exception):
    pass


class AutomaticControlJunctionNotFoundError(
    AutomaticControlError
):
    pass


class AutomaticControlUnavailableError(
    AutomaticControlError
):
    pass


@dataclass
class AutomaticControlResult:
    junction_id: str
    action: str
    current_phase: str
    selected_phase: str | None
    transition_stage: str
    north_south_score: float
    east_west_score: float
    reason: str


class AutomaticControlService:
    @staticmethod
    @transaction.atomic
    def evaluate(
        junction_id: str,
    ) -> AutomaticControlResult:
        try:
            junction = (
                Junction.objects
                .select_for_update()
                .get(junction_id=junction_id)
            )

        except Junction.DoesNotExist as exc:
            raise (
                AutomaticControlJunctionNotFoundError(
                    f"Unknown junction: {junction_id}"
                )
            ) from exc

        # --------------------------------------------------
        # Automatic scheduler must not interfere with
        # MANUAL, EMERGENCY, or DEGRADED modes.
        # --------------------------------------------------

        if junction.mode != JunctionMode.AUTOMATIC:
            raise AutomaticControlUnavailableError(
                f"Automatic scheduling is unavailable "
                f"while junction mode is "
                f"{junction.mode}."
            )

        # --------------------------------------------------
        # Do not start another decision while a safe
        # transition is already running.
        # --------------------------------------------------

        if (
            junction.transition_stage
            != TransitionStage.STABLE
        ):
            return AutomaticControlResult(
                junction_id=junction.junction_id,
                action="TRANSITION_IN_PROGRESS",
                current_phase=junction.current_phase,
                selected_phase=(
                    junction.target_phase
                ),
                transition_stage=(
                    junction.transition_stage
                ),
                north_south_score=0,
                east_west_score=0,
                reason=(
                    "Existing safe transition is "
                    "still in progress."
                ),
            )

        queue_entries = list(
            junction.queue_entries.all()
        )

        decision = TrafficScheduler.choose_phase(
            queue_entries=queue_entries,
            current_phase=junction.current_phase,
            max_wait_seconds=(
                junction.max_wait_seconds
            ),
        )

        AutomaticControlService._audit_decision(
            junction,
            decision,
        )

        selected_phase = (
            decision.selected_phase
        )

        # --------------------------------------------------
        # Nothing waiting
        # --------------------------------------------------

        if selected_phase is None:
            return AutomaticControlResult(
                junction_id=junction.junction_id,
                action="NO_TRAFFIC",
                current_phase=junction.current_phase,
                selected_phase=None,
                transition_stage=(
                    junction.transition_stage
                ),
                north_south_score=(
                    decision.north_south.total_score
                ),
                east_west_score=(
                    decision.east_west.total_score
                ),
                reason=decision.reason,
            )

        # --------------------------------------------------
        # Already serving correct phase
        # --------------------------------------------------

        if (
            junction.current_phase
            == selected_phase
        ):
            return AutomaticControlResult(
                junction_id=junction.junction_id,
                action="KEEP_CURRENT_PHASE",
                current_phase=junction.current_phase,
                selected_phase=selected_phase,
                transition_stage=(
                    junction.transition_stage
                ),
                north_south_score=(
                    decision.north_south.total_score
                ),
                east_west_score=(
                    decision.east_west.total_score
                ),
                reason=decision.reason,
            )

        # --------------------------------------------------
        # Request safe phase change
        # --------------------------------------------------

        try:
            transition = (
                TrafficStateMachine.request_phase(
                    junction.junction_id,
                    selected_phase,
                )
            )

        except InvalidTransitionError as exc:
            raise AutomaticControlError(
                str(exc)
            ) from exc

        return AutomaticControlResult(
            junction_id=junction.junction_id,
            action="PHASE_CHANGE_REQUESTED",
            current_phase=(
                transition.current_phase
            ),
            selected_phase=selected_phase,
            transition_stage=(
                transition.stage
            ),
            north_south_score=(
                decision.north_south.total_score
            ),
            east_west_score=(
                decision.east_west.total_score
            ),
            reason=decision.reason,
        )

    @staticmethod
    def _audit_decision(
        junction: Junction,
        decision: SchedulingDecision,
    ) -> None:
        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "AUTOMATIC_SCHEDULING_DECISION"
            ),
            message=decision.reason,
            metadata={
                "selected_phase": (
                    decision.selected_phase
                ),
                "north_south": {
                    "queue_count": (
                        decision
                        .north_south
                        .queue_count
                    ),
                    "priority_score": (
                        decision
                        .north_south
                        .vehicle_priority_score
                    ),
                    "waiting_score": (
                        decision
                        .north_south
                        .waiting_time_score
                    ),
                    "starvation_bonus": (
                        decision
                        .north_south
                        .starvation_bonus
                    ),
                    "total_score": (
                        decision
                        .north_south
                        .total_score
                    ),
                },
                "east_west": {
                    "queue_count": (
                        decision
                        .east_west
                        .queue_count
                    ),
                    "priority_score": (
                        decision
                        .east_west
                        .vehicle_priority_score
                    ),
                    "waiting_score": (
                        decision
                        .east_west
                        .waiting_time_score
                    ),
                    "starvation_bonus": (
                        decision
                        .east_west
                        .starvation_bonus
                    ),
                    "total_score": (
                        decision
                        .east_west
                        .total_score
                    ),
                },
            },
        )