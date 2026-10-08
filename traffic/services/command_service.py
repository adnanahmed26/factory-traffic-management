from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from traffic.domain.state_machine import (
    InvalidTransitionError,
    TrafficStateMachine,
)

from traffic.models import (
    AuditEvent,
    Direction,
    EmergencyRequest,
    EmergencyStatus,
    Junction,
    JunctionMode,
    ManualOverride,
    ManualOverrideStatus,
    TrafficPhase,
)


class CommandServiceError(Exception):
    """Base exception for junction command processing."""


class CommandJunctionNotFoundError(
    CommandServiceError
):
    pass


class ManualCommandRejectedError(
    CommandServiceError
):
    pass


@dataclass
class CommandResult:
    junction_id: str
    command: str
    accepted: bool
    mode: str
    current_phase: str
    target_phase: str | None
    transition_stage: str
    message: str


class CommandService:
    MANUAL_GREEN_REQUEST = (
        "MANUAL_GREEN_REQUEST"
    )

    RETURN_TO_AUTOMATIC = (
        "RETURN_TO_AUTOMATIC"
    )

    @staticmethod
    def _phase_for_direction(
        direction: str,
    ) -> str:
        if direction in {
            Direction.NORTH,
            Direction.SOUTH,
        }:
            return (
                TrafficPhase.NORTH_SOUTH
            )

        if direction in {
            Direction.EAST,
            Direction.WEST,
        }:
            return (
                TrafficPhase.EAST_WEST
            )

        raise ManualCommandRejectedError(
            f"Unsupported direction: {direction}"
        )

    @staticmethod
    @transaction.atomic
    def process_command(
        junction_id: str,
        command: str,
        direction: str | None = None,
        requested_by: str = "",
    ) -> CommandResult:
        try:
            junction = (
                Junction.objects
                .select_for_update()
                .get(
                    junction_id=junction_id
                )
            )

        except Junction.DoesNotExist as exc:
            raise CommandJunctionNotFoundError(
                f"Unknown junction: {junction_id}"
            ) from exc

        if (
            command
            == CommandService.MANUAL_GREEN_REQUEST
        ):
            return (
                CommandService
                ._manual_green_request(
                    junction=junction,
                    direction=direction,
                    requested_by=requested_by,
                )
            )

        if (
            command
            == CommandService.RETURN_TO_AUTOMATIC
        ):
            return (
                CommandService
                ._return_to_automatic(
                    junction=junction,
                    requested_by=requested_by,
                )
            )

        raise ManualCommandRejectedError(
            f"Unsupported command: {command}"
        )

    @staticmethod
    def _manual_green_request(
        junction: Junction,
        direction: str | None,
        requested_by: str,
    ) -> CommandResult:
        if not direction:
            raise ManualCommandRejectedError(
                "direction is required for "
                "MANUAL_GREEN_REQUEST."
            )

        # Emergency always has priority over
        # manual control.
        active_emergency = (
            EmergencyRequest.objects
            .filter(
                junction=junction,
                status=(
                    EmergencyStatus.ACTIVE
                ),
            )
            .exists()
        )

        if active_emergency:
            raise ManualCommandRejectedError(
                "Manual control cannot override "
                "an active emergency."
            )

        target_phase = (
            CommandService
            ._phase_for_direction(
                direction
            )
        )

        # Do not allow overlapping manual overrides.
        active_override = (
            ManualOverride.objects
            .filter(
                junction=junction,
                status=(
                    ManualOverrideStatus.ACTIVE
                ),
            )
            .first()
        )

        if active_override:
            raise ManualCommandRejectedError(
                "A manual override is already active. "
                "Return the junction to automatic "
                "mode first."
            )

        override = (
            ManualOverride.objects.create(
                junction=junction,
                requested_direction=direction,
                status=(
                    ManualOverrideStatus.ACTIVE
                ),
                requested_by=requested_by,
            )
        )

        junction.mode = (
            JunctionMode.MANUAL
        )

        junction.state_version += 1

        junction.save(
            update_fields=[
                "mode",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type="MANUAL_OVERRIDE",
            direction=direction,
            message=(
                f"Manual GREEN requested for "
                f"{direction}."
            ),
            metadata={
                "manual_override_id": (
                    override.id
                ),
                "requested_by": (
                    requested_by
                ),
                "target_phase": (
                    target_phase
                ),
            },
        )

        try:
            TrafficStateMachine.request_phase(
                junction.junction_id,
                target_phase,
            )

        except InvalidTransitionError as exc:
            # This entire operation is inside
            # transaction.atomic(), so raising here
            # rolls back the manual override and mode.
            raise ManualCommandRejectedError(
                str(exc)
            ) from exc

        junction.refresh_from_db()

        return CommandResult(
            junction_id=(
                junction.junction_id
            ),
            command=(
                CommandService
                .MANUAL_GREEN_REQUEST
            ),
            accepted=True,
            mode=(
                junction.mode
            ),
            current_phase=(
                junction.current_phase
            ),
            target_phase=(
                junction.target_phase
            ),
            transition_stage=(
                junction.transition_stage
            ),
            message=(
                f"Manual request accepted for "
                f"{direction}. "
                f"Safe transition initiated."
            ),
        )

    @staticmethod
    def _return_to_automatic(
        junction: Junction,
        requested_by: str,
    ) -> CommandResult:
        active_emergency = (
            EmergencyRequest.objects
            .filter(
                junction=junction,
                status=(
                    EmergencyStatus.ACTIVE
                ),
            )
            .exists()
        )

        if active_emergency:
            raise ManualCommandRejectedError(
                "Cannot return to automatic mode "
                "while an emergency is active."
            )

        active_overrides = (
            ManualOverride.objects
            .filter(
                junction=junction,
                status=(
                    ManualOverrideStatus.ACTIVE
                ),
            )
        )

        now = timezone.now()

        for override in active_overrides:
            override.status = (
                ManualOverrideStatus.RELEASED
            )

            override.released_at = now

        if active_overrides:
            ManualOverride.objects.bulk_update(
                active_overrides,
                [
                    "status",
                    "released_at",
                ],
            )

        junction.mode = (
            JunctionMode.AUTOMATIC
        )

        junction.state_version += 1

        junction.save(
            update_fields=[
                "mode",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "RETURN_TO_AUTOMATIC"
            ),
            message=(
                "Manual override released. "
                "Junction returned to automatic mode."
            ),
            metadata={
                "requested_by": (
                    requested_by
                ),
            },
        )

        return CommandResult(
            junction_id=(
                junction.junction_id
            ),
            command=(
                CommandService
                .RETURN_TO_AUTOMATIC
            ),
            accepted=True,
            mode=(
                junction.mode
            ),
            current_phase=(
                junction.current_phase
            ),
            target_phase=(
                junction.target_phase
            ),
            transition_stage=(
                junction.transition_stage
            ),
            message=(
                "Junction returned to automatic mode."
            ),
        )