from dataclasses import dataclass
from typing import Optional

from django.db import transaction

from traffic.domain.state_machine import (
    InvalidTransitionError,
    TrafficStateMachine,
)
from traffic.models import (
    AuditEvent,
    ControllerStatus,
    EmergencyRequest,
    EmergencyStatus,
    Junction,
    JunctionMode,
    TrafficPhase,
    TransitionStage,
)


class EmergencyControlError(Exception):
    """
    Base exception for emergency control errors.
    """


class EmergencyJunctionNotFoundError(
    EmergencyControlError
):
    pass


class InvalidEmergencyDirectionError(
    EmergencyControlError
):
    pass


@dataclass
class EmergencyControlResult:
    junction_id: str
    action: str
    emergency_id: Optional[int]
    vehicle_id: Optional[str]
    direction: Optional[str]
    target_phase: Optional[str]
    current_phase: str
    transition_stage: str
    message: str


class EmergencyControlService:
    """
    Coordinates emergency vehicle preemption.

    This service NEVER directly changes individual signal
    colors.

    All phase changes are delegated to TrafficStateMachine,
    preserving the normal safety sequence:

        GREEN
          ->
        YELLOW
          ->
        ALL_RED
          ->
        TARGET GREEN

    Priority ownership:

        EMERGENCY > MANUAL > AUTOMATIC

    Competing emergency policy:

    - The oldest ACTIVE emergency owns the junction first.
    - Ties are broken by database id.
    - The selected emergency keeps ownership until it clears.
    - After it clears, the next-oldest ACTIVE emergency wins.

    This deterministic policy prevents the junction from
    repeatedly switching between competing emergency vehicles.
    """

    # ========================================================
    # PUBLIC API
    # ========================================================

    @staticmethod
    def process_junction(
        junction_id: str,
    ) -> EmergencyControlResult:
        """
        Evaluate emergency control for one junction.

        This method is intended to be called by the background
        traffic-controller loop.

        It is safe to call repeatedly.
        """

        try:
            junction = Junction.objects.get(
                junction_id=junction_id
            )

        except Junction.DoesNotExist as exc:
            raise EmergencyJunctionNotFoundError(
                f"Unknown junction: {junction_id}"
            ) from exc

        # ----------------------------------------------------
        # Controller failure always wins over preemption.
        #
        # We must never attempt a new phase transition when the
        # physical controller is known to be unavailable.
        # ----------------------------------------------------

        if junction.controller_status in {
            ControllerStatus.OFFLINE,
            ControllerStatus.DEGRADED,
        }:
            return EmergencyControlService._result(
                junction=junction,
                emergency=None,
                action="BLOCKED_CONTROLLER",
                target_phase=None,
                message=(
                    "Emergency preemption blocked because "
                    f"controller status is "
                    f"{junction.controller_status}."
                ),
            )

        # ----------------------------------------------------
        # Select active emergency.
        # ----------------------------------------------------

        emergency = (
            EmergencyControlService
            ._select_active_emergency(
                junction=junction
            )
        )

        # ----------------------------------------------------
        # No active emergency.
        #
        # SensorEventService is responsible for restoring
        # MANUAL/AUTOMATIC when the final emergency clears.
        # ----------------------------------------------------

        if emergency is None:
            return EmergencyControlService._result(
                junction=junction,
                emergency=None,
                action="NO_ACTIVE_EMERGENCY",
                target_phase=None,
                message=(
                    "No active emergency request exists."
                ),
            )

        # ----------------------------------------------------
        # Defensive ownership correction.
        #
        # Normally SensorEventService already sets EMERGENCY,
        # but persistence/restart scenarios may leave an active
        # request while the mode is stale.
        # ----------------------------------------------------

        if junction.mode != JunctionMode.EMERGENCY:
            EmergencyControlService._ensure_emergency_mode(
                junction=junction,
                emergency=emergency,
            )

            junction.refresh_from_db()

        target_phase = (
            EmergencyControlService
            .phase_for_direction(
                emergency.direction
            )
        )

        # ----------------------------------------------------
        # An existing safe transition is already underway.
        #
        # IMPORTANT:
        #
        # Do not overwrite target_phase in the middle of an
        # existing transition.
        #
        # TrafficStateMachine owns that transition and must
        # complete it safely first.
        #
        # Once it becomes STABLE, the next controller cycle
        # reevaluates this emergency and requests its phase.
        # ----------------------------------------------------

        if (
            junction.transition_stage
            != TransitionStage.STABLE
        ):
            return EmergencyControlService._result(
                junction=junction,
                emergency=emergency,
                action="WAITING_FOR_TRANSITION",
                target_phase=target_phase,
                message=(
                    "Emergency is active, but a safe signal "
                    "transition is already in progress. "
                    "The current transition will complete "
                    "before emergency preemption continues."
                ),
            )

        # ----------------------------------------------------
        # Emergency phase already active.
        #
        # Hold this phase. Automatic scheduling/manual requests
        # must not take ownership while mode=EMERGENCY.
        # ----------------------------------------------------

        if junction.current_phase == target_phase:
            return EmergencyControlService._result(
                junction=junction,
                emergency=emergency,
                action="HOLDING_EMERGENCY_PHASE",
                target_phase=target_phase,
                message=(
                    f"Emergency {emergency.vehicle_id} is "
                    f"being served by {target_phase}."
                ),
            )

        # ----------------------------------------------------
        # Request safe preemption through the existing traffic
        # state machine.
        #
        # This is the critical safety boundary:
        #
        # EmergencyControlService DOES NOT set GREEN.
        # ----------------------------------------------------

        try:
            transition = (
                TrafficStateMachine.request_phase(
                    junction_id,
                    target_phase,
                )
            )

        except InvalidTransitionError as exc:
            # This can happen if another controller worker
            # started a transition between our read above and
            # request_phase().
            #
            # Treat it as a safe retry condition rather than
            # bypassing the state machine.
            junction.refresh_from_db()

            return EmergencyControlService._result(
                junction=junction,
                emergency=emergency,
                action="WAITING_FOR_TRANSITION",
                target_phase=target_phase,
                message=(
                    "Emergency preemption will retry after "
                    f"the current transition: {exc}"
                ),
            )

        junction.refresh_from_db()

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "EMERGENCY_PREEMPTION_REQUESTED"
            ),
            direction=emergency.direction,
            vehicle_id=emergency.vehicle_id,
            message=(
                f"Emergency vehicle "
                f"{emergency.vehicle_id} from "
                f"{emergency.direction} requested "
                f"traffic phase {target_phase}."
            ),
            metadata={
                "emergency_request_id": (
                    emergency.id
                ),
                "vehicle_id": (
                    emergency.vehicle_id
                ),
                "direction": (
                    emergency.direction
                ),
                "target_phase": (
                    target_phase
                ),
                "transition_stage": (
                    transition.stage
                ),
                "current_phase": (
                    transition.current_phase
                ),
            },
        )

        return EmergencyControlService._result(
            junction=junction,
            emergency=emergency,
            action="PREEMPTION_REQUESTED",
            target_phase=target_phase,
            message=(
                f"Safe emergency preemption requested "
                f"for {emergency.direction}; "
                f"target phase is {target_phase}."
            ),
        )

    # ========================================================
    # EMERGENCY SELECTION
    # ========================================================

    @staticmethod
    def _select_active_emergency(
        *,
        junction: Junction,
    ) -> Optional[EmergencyRequest]:
        """
        Deterministic competing-emergency policy.

        Oldest active emergency wins first.

        detected_at is the primary ordering key and id breaks
        ties deterministically.
        """

        return (
            EmergencyRequest.objects
            .filter(
                junction=junction,
                status=EmergencyStatus.ACTIVE,
            )
            .order_by(
                "detected_at",
                "id",
            )
            .first()
        )

    # ========================================================
    # DIRECTION -> PHASE
    # ========================================================

    @staticmethod
    def phase_for_direction(
        direction: str,
    ) -> str:
        """
        Convert an emergency approach direction into the phase
        that can serve that direction.
        """

        if direction in {
            "NORTH",
            "SOUTH",
        }:
            return TrafficPhase.NORTH_SOUTH

        if direction in {
            "EAST",
            "WEST",
        }:
            return TrafficPhase.EAST_WEST

        raise InvalidEmergencyDirectionError(
            f"Unsupported emergency direction: {direction}"
        )

    # ========================================================
    # MODE RECOVERY
    # ========================================================

    @staticmethod
    @transaction.atomic
    def _ensure_emergency_mode(
        *,
        junction: Junction,
        emergency: EmergencyRequest,
    ) -> None:
        """
        Defensive recovery for restart/stale-mode scenarios.

        An ACTIVE emergency request means emergency control
        owns the junction.
        """

        locked_junction = (
            Junction.objects
            .select_for_update()
            .get(
                pk=junction.pk
            )
        )

        if (
            locked_junction.mode
            == JunctionMode.EMERGENCY
        ):
            return

        previous_mode = (
            locked_junction.mode
        )

        locked_junction.mode = (
            JunctionMode.EMERGENCY
        )

        locked_junction.state_version += 1

        locked_junction.save(
            update_fields=[
                "mode",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=locked_junction,
            event_type="MODE_CHANGE",
            direction=emergency.direction,
            vehicle_id=emergency.vehicle_id,
            message=(
                f"Active emergency request restored "
                f"junction mode from {previous_mode} "
                f"to EMERGENCY."
            ),
            metadata={
                "previous_mode": (
                    previous_mode
                ),
                "new_mode": (
                    JunctionMode.EMERGENCY
                ),
                "reason": (
                    "ACTIVE_EMERGENCY_RECOVERY"
                ),
                "emergency_request_id": (
                    emergency.id
                ),
            },
        )

    # ========================================================
    # RESULT BUILDER
    # ========================================================

    @staticmethod
    def _result(
        *,
        junction: Junction,
        emergency: Optional[
            EmergencyRequest
        ],
        action: str,
        target_phase: Optional[str],
        message: str,
    ) -> EmergencyControlResult:
        return EmergencyControlResult(
            junction_id=(
                junction.junction_id
            ),
            action=action,
            emergency_id=(
                emergency.id
                if emergency
                else None
            ),
            vehicle_id=(
                emergency.vehicle_id
                if emergency
                else None
            ),
            direction=(
                emergency.direction
                if emergency
                else None
            ),
            target_phase=target_phase,
            current_phase=(
                junction.current_phase
            ),
            transition_stage=(
                junction.transition_stage
            ),
            message=message,
        )