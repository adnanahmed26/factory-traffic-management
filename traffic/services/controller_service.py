import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from traffic.models import (
    AuditEvent,
    CommandStatus,
    ControllerCommand,
    ControllerEvent,
    ControllerStatus,
    SignalState,
)


class ControllerServiceError(Exception):
    """Base exception for physical controller processing."""


class ControllerCommandNotFoundError(
    ControllerServiceError
):
    pass


class ControllerJunctionMismatchError(
    ControllerServiceError
):
    pass


class ControllerSignalNotFoundError(
    ControllerServiceError
):
    pass


@dataclass
class ControllerEventResult:
    controller_event_id: str
    command_id: str
    status: str
    actual_state: str | None
    matched_desired_state: bool
    duplicate: bool
    message: str


class ControllerService:
    # ========================================================
    # CONTROLLER EVENT PROCESSING
    # ========================================================

    @staticmethod
    @transaction.atomic
    def process_event(
        data: dict,
    ) -> ControllerEventResult:
        """
        Process an event coming back from the physical
        signal controller.

        ACK represents physical truth and is the only place
        where SignalState.actual_state is updated.
        """

        command_id = data["command_id"]
        junction_id = data["junction_id"]
        incoming_status = data["status"]

        actual_state = data.get(
            "actual_state"
        )

        controller_event_id = (
            data.get("controller_event_id")
            or f"ctrl-{uuid.uuid4().hex[:12]}"
        )

        # ----------------------------------------------------
        # 1. Controller-event idempotency
        # ----------------------------------------------------

        existing_event = (
            ControllerEvent.objects
            .filter(
                controller_event_id=(
                    controller_event_id
                )
            )
            .first()
        )

        if existing_event:
            return ControllerEventResult(
                controller_event_id=(
                    existing_event.controller_event_id
                ),
                command_id=command_id,
                status=(
                    existing_event.status
                ),
                actual_state=(
                    existing_event.actual_state
                ),
                matched_desired_state=False,
                duplicate=True,
                message=(
                    "Duplicate controller event ignored."
                ),
            )

        # ----------------------------------------------------
        # 2. Find and lock command
        # ----------------------------------------------------

        try:
            command = (
                ControllerCommand.objects
                .select_for_update()
                .select_related(
                    "junction"
                )
                .get(
                    command_id=command_id
                )
            )

        except ControllerCommand.DoesNotExist as exc:
            raise (
                ControllerCommandNotFoundError(
                    f"Unknown command: "
                    f"{command_id}"
                )
            ) from exc

        junction = command.junction

        # ----------------------------------------------------
        # 3. Junction must match command
        # ----------------------------------------------------

        if (
            junction.junction_id
            != junction_id
        ):
            raise (
                ControllerJunctionMismatchError(
                    "Controller event junction does "
                    "not match the command junction."
                )
            )

        # ----------------------------------------------------
        # 4. Command already ACKed
        #
        # Store the event for history, but do not apply the
        # same physical state a second time.
        # ----------------------------------------------------

        if (
            command.status
            == CommandStatus.ACK
        ):
            ControllerEvent.objects.create(
                controller_event_id=(
                    controller_event_id
                ),
                command=command,
                junction=junction,
                status=incoming_status,
                actual_state=actual_state,
                controller_timestamp=(
                    data.get("timestamp")
                ),
                processed=True,
            )

            return ControllerEventResult(
                controller_event_id=(
                    controller_event_id
                ),
                command_id=(
                    command.command_id
                ),
                status=(
                    command.status
                ),
                actual_state=(
                    command.actual_state
                ),
                matched_desired_state=True,
                duplicate=True,
                message=(
                    "Command was already acknowledged; "
                    "duplicate ACK had no state effect."
                ),
            )

        # ----------------------------------------------------
        # 5. Persist incoming controller event
        # ----------------------------------------------------

        controller_event = (
            ControllerEvent.objects.create(
                controller_event_id=(
                    controller_event_id
                ),
                command=command,
                junction=junction,
                status=incoming_status,
                actual_state=actual_state,
                controller_timestamp=(
                    data.get("timestamp")
                ),
                processed=False,
            )
        )

        # ----------------------------------------------------
        # 6. Explicit FAILED response
        # ----------------------------------------------------

        if (
            incoming_status
            == CommandStatus.FAILED
        ):
            command.status = (
                CommandStatus.FAILED
            )

            command.last_error = (
                "Controller reported FAILED."
            )

            command.save(
                update_fields=[
                    "status",
                    "last_error",
                ]
            )

            junction.controller_status = (
                ControllerStatus.DEGRADED
            )

            junction.save(
                update_fields=[
                    "controller_status",
                    "updated_at",
                ]
            )

            controller_event.processed = True

            controller_event.save(
                update_fields=[
                    "processed",
                ]
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type=(
                    "CONTROLLER_COMMAND_FAILED"
                ),
                direction=(
                    command.direction
                ),
                command_id=(
                    command.command_id
                ),
                message=(
                    command.last_error
                ),
                metadata={
                    "status": (
                        CommandStatus.FAILED
                    ),
                    "requested_state": (
                        command.requested_state
                    ),
                },
            )

            return ControllerEventResult(
                controller_event_id=(
                    controller_event_id
                ),
                command_id=(
                    command.command_id
                ),
                status=(
                    command.status
                ),
                actual_state=actual_state,
                matched_desired_state=False,
                duplicate=False,
                message=(
                    command.last_error
                ),
            )

        # ----------------------------------------------------
        # 7. Explicit TIMEOUT controller event
        # ----------------------------------------------------

        if (
            incoming_status
            == CommandStatus.TIMEOUT
        ):
            ControllerService._mark_command_timeout(
                command=command,
                junction=junction,
            )

            controller_event.processed = True

            controller_event.save(
                update_fields=[
                    "processed",
                ]
            )

            return ControllerEventResult(
                controller_event_id=(
                    controller_event_id
                ),
                command_id=(
                    command.command_id
                ),
                status=(
                    CommandStatus.TIMEOUT
                ),
                actual_state=actual_state,
                matched_desired_state=False,
                duplicate=False,
                message=(
                    "Controller command timed out."
                ),
            )

        # ----------------------------------------------------
        # 8. Remaining controller event must be ACK
        # ----------------------------------------------------

        if (
            incoming_status
            != CommandStatus.ACK
        ):
            raise ControllerServiceError(
                f"Unsupported controller status: "
                f"{incoming_status}"
            )

        if not actual_state:
            raise ControllerServiceError(
                "actual_state is required for ACK."
            )

        if not command.direction:
            raise ControllerServiceError(
                "Signal command has no direction."
            )

        # ----------------------------------------------------
        # 9. Find and lock corresponding signal
        # ----------------------------------------------------

        try:
            signal = (
                SignalState.objects
                .select_for_update()
                .get(
                    junction=junction,
                    direction=(
                        command.direction
                    ),
                )
            )

        except SignalState.DoesNotExist as exc:
            raise (
                ControllerSignalNotFoundError(
                    f"No signal exists for "
                    f"{command.direction}."
                )
            ) from exc

        now = timezone.now()

        was_late = (
            command.expires_at
            is not None
            and now > command.expires_at
        )

        previous_command_status = (
            command.status
        )

        # ----------------------------------------------------
        # 10. Physical ACK is physical truth
        # ----------------------------------------------------

        signal.actual_state = (
            actual_state
        )

        signal.actual_confirmed_at = (
            now
        )

        signal.save(
            update_fields=[
                "actual_state",
                "actual_confirmed_at",
                "updated_at",
            ]
        )

        command.actual_state = (
            actual_state
        )

        command.acknowledged_at = (
            now
        )

        matched = (
            actual_state
            == command.requested_state
        )

        # ----------------------------------------------------
        # 11. ACK matched requested physical state
        # ----------------------------------------------------

        if matched:
            command.status = (
                CommandStatus.ACK
            )

            command.last_error = ""

            # A matching physical ACK proves that communication
            # with the controller is available again.
            junction.controller_status = (
                ControllerStatus.ONLINE
            )

            message = (
                f"Controller confirmed "
                f"{command.direction} = "
                f"{actual_state}."
            )

            event_type = (
                "SIGNAL_STATE_CONFIRMED"
            )

        # ----------------------------------------------------
        # 12. ACK reported different state
        # ----------------------------------------------------

        else:
            command.status = (
                CommandStatus.FAILED
            )

            command.last_error = (
                f"Desired/actual mismatch: "
                f"requested "
                f"{command.requested_state}, "
                f"controller reported "
                f"{actual_state}."
            )

            junction.controller_status = (
                ControllerStatus.DEGRADED
            )

            message = (
                command.last_error
            )

            event_type = (
                "SIGNAL_STATE_MISMATCH"
            )

        command.save(
            update_fields=[
                "status",
                "actual_state",
                "acknowledged_at",
                "last_error",
            ]
        )

        junction.save(
            update_fields=[
                "controller_status",
                "updated_at",
            ]
        )

        controller_event.processed = True

        controller_event.save(
            update_fields=[
                "processed",
            ]
        )

        # ----------------------------------------------------
        # 13. Audit physical confirmation
        # ----------------------------------------------------

        AuditEvent.objects.create(
            junction=junction,
            event_type=event_type,
            direction=(
                command.direction
            ),
            command_id=(
                command.command_id
            ),
            message=message,
            metadata={
                "requested_state": (
                    command.requested_state
                ),
                "actual_state": (
                    actual_state
                ),
                "matched": matched,
                "late_ack": was_late,
                "previous_command_status": (
                    previous_command_status
                ),
            },
        )

        # ----------------------------------------------------
        # 14. Separate late-ACK recovery audit
        # ----------------------------------------------------

        if (
            matched
            and previous_command_status
            == CommandStatus.TIMEOUT
        ):
            AuditEvent.objects.create(
                junction=junction,
                event_type=(
                    "CONTROLLER_LATE_ACK_RECOVERY"
                ),
                direction=(
                    command.direction
                ),
                command_id=(
                    command.command_id
                ),
                message=(
                    f"Late ACK received for "
                    f"{command.command_id}; "
                    f"controller communication recovered."
                ),
                metadata={
                    "requested_state": (
                        command.requested_state
                    ),
                    "actual_state": (
                        actual_state
                    ),
                    "acknowledged_at": (
                        now.isoformat()
                    ),
                },
            )

        return ControllerEventResult(
            controller_event_id=(
                controller_event_id
            ),
            command_id=(
                command.command_id
            ),
            status=(
                command.status
            ),
            actual_state=(
                actual_state
            ),
            matched_desired_state=(
                matched
            ),
            duplicate=False,
            message=message,
        )

    # ========================================================
    # AUTOMATIC TIMEOUT PROCESSING
    # ========================================================

    @staticmethod
    @transaction.atomic
    def expire_timed_out_commands(
        junction_id: str | None = None,
    ) -> int:
        """
        Mark expired PENDING/SENT commands as TIMEOUT.

        Defensive behavior:

        If a legacy or stale active command has expires_at=NULL,
        reconstruct its deadline as:

            created_at + CONTROLLER_COMMAND_TIMEOUT_SECONDS

        This ensures an active physical command can never wait
        forever simply because an expiry timestamp was missing.

        A timeout does NOT advance the traffic state machine.
        """

        # Local import avoids a module-level circular dependency.
        from traffic.domain.state_machine import (
            TrafficStateMachine,
        )

        now = timezone.now()

        active_commands = (
            ControllerCommand.objects
            .select_for_update()
            .select_related(
                "junction"
            )
            .filter(
                status__in=[
                    CommandStatus.PENDING,
                    CommandStatus.SENT,
                ]
            )
            .order_by(
                "created_at"
            )
        )

        if junction_id is not None:
            active_commands = (
                active_commands.filter(
                    junction__junction_id=(
                        junction_id
                    )
                )
            )

        timed_out_count = 0

        for command in active_commands:
            # ------------------------------------------------
            # Defensive recovery for old/legacy commands that
            # were created without an expires_at value.
            # ------------------------------------------------

            if command.expires_at is None:
                command.expires_at = (
                    command.created_at
                    + timedelta(
                        seconds=(
                            TrafficStateMachine
                            .CONTROLLER_COMMAND_TIMEOUT_SECONDS
                        )
                    )
                )

                command.save(
                    update_fields=[
                        "expires_at",
                    ]
                )

            # ------------------------------------------------
            # Still within acknowledgement window.
            # ------------------------------------------------

            if (
                command.expires_at
                > now
            ):
                continue

            # ------------------------------------------------
            # Deadline passed with no ACK.
            # ------------------------------------------------

            ControllerService._mark_command_timeout(
                command=command,
                junction=command.junction,
            )

            timed_out_count += 1

        return timed_out_count

    # ========================================================
    # TIMEOUT HELPER
    # ========================================================

    @staticmethod
    def _mark_command_timeout(
        *,
        command: ControllerCommand,
        junction,
    ) -> None:
        """
        Mark a physical controller command as timed out.

        The junction becomes OFFLINE.

        Importantly, this method does NOT alter:
        - actual signal states
        - current phase
        - target phase
        - transition stage

        Therefore the traffic state machine remains frozen until
        physical communication is recovered.
        """

        if (
            command.status
            == CommandStatus.TIMEOUT
        ):
            return

        # Do not overwrite a command that has already received
        # its successful physical acknowledgement.
        if (
            command.status
            == CommandStatus.ACK
        ):
            return

        command.status = (
            CommandStatus.TIMEOUT
        )

        command.last_error = (
            "No controller acknowledgement was "
            "received before command expiry."
        )

        command.save(
            update_fields=[
                "status",
                "last_error",
            ]
        )

        junction.controller_status = (
            ControllerStatus.OFFLINE
        )

        junction.save(
            update_fields=[
                "controller_status",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "CONTROLLER_COMMAND_TIMEOUT"
            ),
            direction=(
                command.direction
            ),
            command_id=(
                command.command_id
            ),
            message=(
                f"Controller command "
                f"{command.command_id} timed out."
            ),
            metadata={
                "requested_state": (
                    command.requested_state
                ),
                "created_at": (
                    command.created_at.isoformat()
                    if command.created_at
                    else None
                ),
                "expires_at": (
                    command.expires_at.isoformat()
                    if command.expires_at
                    else None
                ),
                "controller_status": (
                    ControllerStatus.OFFLINE
                ),
            },
        )