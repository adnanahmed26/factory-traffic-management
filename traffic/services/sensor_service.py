from dataclasses import dataclass

from django.db import IntegrityError, transaction
from django.utils import timezone

from traffic.models import (
    AuditEvent,
    EmergencyRequest,
    EmergencyStatus,
    Junction,
    JunctionMode,
    ManualOverride,
    ManualOverrideStatus,
    VehicleEvent,
    VehicleEventType,
    VehicleQueueEntry,
    VehicleType,
)


class SensorEventError(Exception):
    """
    Base exception for sensor-event processing errors.
    """


class JunctionNotFoundError(
    SensorEventError
):
    pass


class InvalidClearanceError(
    SensorEventError
):
    pass


class InvalidEventError(
    SensorEventError
):
    pass


@dataclass
class SensorEventResult:
    event_id: str
    status: str
    duplicate: bool
    queue_changed: bool
    message: str


class SensorEventService:
    """
    Application/service layer for processing vehicle sensor
    events.

    Responsibilities:

    - event-id idempotency
    - queue creation/removal
    - sequence protection
    - emergency request persistence
    - junction mode ownership

    IMPORTANT:

    This service NEVER directly changes traffic lights.

    Emergency signal preemption is performed separately by
    EmergencyControlService through TrafficStateMachine so
    emergency priority cannot bypass YELLOW / ALL_RED safety.
    """

    # ========================================================
    # PUBLIC API
    # ========================================================

    @staticmethod
    @transaction.atomic
    def process_event(
        data: dict,
    ) -> SensorEventResult:
        event_id = data["event_id"]

        # ----------------------------------------------------
        # 1. Duplicate event-id protection
        # ----------------------------------------------------

        existing_event = (
            VehicleEvent.objects
            .filter(
                event_id=event_id
            )
            .first()
        )

        if existing_event:
            AuditEvent.objects.create(
                junction=(
                    existing_event.junction
                ),
                event_type=(
                    "DUPLICATE_SENSOR_EVENT"
                ),
                direction=(
                    existing_event.direction
                ),
                vehicle_id=(
                    existing_event.vehicle_id
                ),
                message=(
                    f"Duplicate sensor event "
                    f"ignored: {event_id}"
                ),
                metadata={
                    "event_id": event_id,
                },
            )

            return SensorEventResult(
                event_id=event_id,
                status="DUPLICATE",
                duplicate=True,
                queue_changed=False,
                message=(
                    "Duplicate event ignored."
                ),
            )

        # ----------------------------------------------------
        # 2. Lock junction
        # ----------------------------------------------------

        try:
            junction = (
                Junction.objects
                .select_for_update()
                .get(
                    junction_id=(
                        data["junction_id"]
                    )
                )
            )

        except Junction.DoesNotExist as exc:
            raise JunctionNotFoundError(
                f"Unknown junction: "
                f"{data['junction_id']}"
            ) from exc

        # ----------------------------------------------------
        # 3. Persist incoming vehicle event
        # ----------------------------------------------------

        try:
            event = (
                VehicleEvent.objects.create(
                    event_id=event_id,
                    junction=junction,
                    direction=(
                        data["direction"]
                    ),
                    event_type=(
                        data["event_type"]
                    ),
                    vehicle_id=(
                        data["vehicle_id"]
                    ),
                    vehicle_type=(
                        data["vehicle_type"]
                    ),
                    sequence_no=(
                        data["sequence_no"]
                    ),
                    sensor_timestamp=(
                        data["timestamp"]
                    ),
                )
            )

        except IntegrityError:
            # Race-condition fallback:
            #
            # another request may have inserted the same
            # event_id after our initial duplicate check.
            return SensorEventResult(
                event_id=event_id,
                status="DUPLICATE",
                duplicate=True,
                queue_changed=False,
                message=(
                    "Duplicate event ignored."
                ),
            )

        queue_changed = False

        # ----------------------------------------------------
        # 4. ARRIVED
        # ----------------------------------------------------

        if (
            event.event_type
            == VehicleEventType.VEHICLE_ARRIVED
        ):
            queue_changed = (
                SensorEventService
                ._process_arrival(
                    junction=junction,
                    event=event,
                )
            )

        # ----------------------------------------------------
        # 5. CLEARED
        # ----------------------------------------------------

        elif (
            event.event_type
            == VehicleEventType.VEHICLE_CLEARED
        ):
            queue_changed = (
                SensorEventService
                ._process_clearance(
                    junction=junction,
                    event=event,
                )
            )

        # ----------------------------------------------------
        # 6. Unsupported event
        # ----------------------------------------------------

        else:
            event.rejected = True

            event.rejection_reason = (
                f"Unsupported event type: "
                f"{event.event_type}"
            )

            event.processed = False
            event.processed_at = (
                timezone.now()
            )

            event.save(
                update_fields=[
                    "rejected",
                    "rejection_reason",
                    "processed",
                    "processed_at",
                ]
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type=(
                    "SENSOR_EVENT_REJECTED"
                ),
                direction=(
                    event.direction
                ),
                vehicle_id=(
                    event.vehicle_id
                ),
                message=(
                    event.rejection_reason
                ),
                metadata={
                    "event_id": (
                        event.event_id
                    ),
                },
            )

            raise InvalidEventError(
                event.rejection_reason
            )

        # ----------------------------------------------------
        # 7. Mark event processed
        # ----------------------------------------------------

        event.processed = True

        event.processed_at = (
            timezone.now()
        )

        event.save(
            update_fields=[
                "processed",
                "processed_at",
            ]
        )

        # ----------------------------------------------------
        # 8. Increment junction state version
        # ----------------------------------------------------

        junction.state_version += 1

        junction.save(
            update_fields=[
                "state_version",
                "updated_at",
            ]
        )

        return SensorEventResult(
            event_id=event.event_id,
            status="PROCESSED",
            duplicate=False,
            queue_changed=queue_changed,
            message=(
                "Sensor event processed "
                "successfully."
            ),
        )

    # ========================================================
    # VEHICLE ARRIVAL
    # ========================================================

    @staticmethod
    def _process_arrival(
        junction: Junction,
        event: VehicleEvent,
    ) -> bool:
        existing_vehicle = (
            VehicleQueueEntry.objects
            .filter(
                junction=junction,
                vehicle_id=(
                    event.vehicle_id
                ),
            )
            .first()
        )

        # ----------------------------------------------------
        # Vehicle already waiting
        # ----------------------------------------------------

        if existing_vehicle:
            event.rejected = True

            event.rejection_reason = (
                "Vehicle is already waiting "
                "at this junction."
            )

            event.save(
                update_fields=[
                    "rejected",
                    "rejection_reason",
                ]
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type=(
                    "ARRIVAL_REJECTED"
                ),
                direction=(
                    event.direction
                ),
                vehicle_id=(
                    event.vehicle_id
                ),
                message=(
                    event.rejection_reason
                ),
                metadata={
                    "event_id": (
                        event.event_id
                    ),
                    "sequence_no": (
                        event.sequence_no
                    ),
                },
            )

            return False

        # ----------------------------------------------------
        # Add vehicle to queue
        # ----------------------------------------------------

        VehicleQueueEntry.objects.create(
            junction=junction,
            vehicle_id=(
                event.vehicle_id
            ),
            direction=(
                event.direction
            ),
            vehicle_type=(
                event.vehicle_type
            ),
            arrived_at=(
                event.sensor_timestamp
            ),
            last_sequence_no=(
                event.sequence_no
            ),
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "VEHICLE_DETECTED"
            ),
            direction=(
                event.direction
            ),
            vehicle_id=(
                event.vehicle_id
            ),
            message=(
                f"{event.vehicle_type} "
                f"{event.vehicle_id} "
                f"arrived from "
                f"{event.direction}."
            ),
            metadata={
                "event_id": (
                    event.event_id
                ),
                "sequence_no": (
                    event.sequence_no
                ),
                "sensor_timestamp": (
                    event.sensor_timestamp.isoformat()
                    if event.sensor_timestamp
                    else None
                ),
            },
        )

        # ----------------------------------------------------
        # Emergency vehicle
        # ----------------------------------------------------

        if (
            event.vehicle_type
            == VehicleType.EMERGENCY
        ):
            SensorEventService._activate_emergency(
                junction=junction,
                event=event,
            )

        return True

    # ========================================================
    # VEHICLE CLEARANCE
    # ========================================================

    @staticmethod
    def _process_clearance(
        junction: Junction,
        event: VehicleEvent,
    ) -> bool:
        queue_entry = (
            VehicleQueueEntry.objects
            .filter(
                junction=junction,
                vehicle_id=(
                    event.vehicle_id
                ),
            )
            .first()
        )

        # ----------------------------------------------------
        # No matching active vehicle
        # ----------------------------------------------------

        if not queue_entry:
            event.rejected = True

            event.rejection_reason = (
                "Vehicle clearance received "
                "without a matching active "
                "queue entry."
            )

            event.save(
                update_fields=[
                    "rejected",
                    "rejection_reason",
                ]
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type=(
                    "CLEARANCE_REJECTED"
                ),
                direction=(
                    event.direction
                ),
                vehicle_id=(
                    event.vehicle_id
                ),
                message=(
                    event.rejection_reason
                ),
                metadata={
                    "event_id": (
                        event.event_id
                    ),
                    "sequence_no": (
                        event.sequence_no
                    ),
                },
            )

            return False

        # ----------------------------------------------------
        # Out-of-order / stale clearance protection
        # ----------------------------------------------------

        if (
            queue_entry.last_sequence_no
            is not None
            and event.sequence_no
            <= queue_entry.last_sequence_no
        ):
            event.rejected = True

            event.rejection_reason = (
                "Stale or out-of-order "
                "clearance event."
            )

            event.save(
                update_fields=[
                    "rejected",
                    "rejection_reason",
                ]
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type=(
                    "STALE_SENSOR_EVENT"
                ),
                direction=(
                    event.direction
                ),
                vehicle_id=(
                    event.vehicle_id
                ),
                message=(
                    event.rejection_reason
                ),
                metadata={
                    "event_id": (
                        event.event_id
                    ),
                    "sequence_no": (
                        event.sequence_no
                    ),
                    "last_sequence_no": (
                        queue_entry
                        .last_sequence_no
                    ),
                },
            )

            return False

        # ----------------------------------------------------
        # Preserve original queue facts before delete
        # ----------------------------------------------------

        original_direction = (
            queue_entry.direction
        )

        original_vehicle_type = (
            queue_entry.vehicle_type
        )

        queue_entry.delete()

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "VEHICLE_CLEARED"
            ),
            direction=(
                original_direction
            ),
            vehicle_id=(
                event.vehicle_id
            ),
            message=(
                f"{original_vehicle_type} "
                f"{event.vehicle_id} cleared "
                f"{original_direction}."
            ),
            metadata={
                "event_id": (
                    event.event_id
                ),
                "sequence_no": (
                    event.sequence_no
                ),
            },
        )

        # ----------------------------------------------------
        # Emergency clearance
        # ----------------------------------------------------

        if (
            original_vehicle_type
            == VehicleType.EMERGENCY
        ):
            SensorEventService._clear_emergency(
                junction=junction,
                event=event,
                original_direction=(
                    original_direction
                ),
            )

        return True

    # ========================================================
    # EMERGENCY ACTIVATION
    # ========================================================

    @staticmethod
    def _activate_emergency(
        junction: Junction,
        event: VehicleEvent,
    ) -> None:
        """
        Persist emergency intent.

        This method does NOT directly request a signal phase.
        EmergencyControlService owns traffic preemption.
        """

        emergency, created = (
            EmergencyRequest.objects
            .get_or_create(
                junction=junction,
                vehicle_id=(
                    event.vehicle_id
                ),
                status=(
                    EmergencyStatus.ACTIVE
                ),
                defaults={
                    "direction": (
                        event.direction
                    ),
                    "detected_event": event,
                    "detected_at": (
                        event.sensor_timestamp
                    ),
                },
            )
        )

        previous_mode = (
            junction.mode
        )

        if (
            junction.mode
            != JunctionMode.EMERGENCY
        ):
            junction.mode = (
                JunctionMode.EMERGENCY
            )

            junction.save(
                update_fields=[
                    "mode",
                    "updated_at",
                ]
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type="MODE_CHANGE",
                direction=(
                    event.direction
                ),
                vehicle_id=(
                    event.vehicle_id
                ),
                message=(
                    f"Junction mode changed "
                    f"from {previous_mode} "
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
                        "EMERGENCY_DETECTED"
                    ),
                },
            )

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "EMERGENCY_DETECTED"
            ),
            direction=(
                event.direction
            ),
            vehicle_id=(
                event.vehicle_id
            ),
            message=(
                f"Emergency vehicle "
                f"{event.vehicle_id} detected "
                f"from {event.direction}."
            ),
            metadata={
                "event_id": (
                    event.event_id
                ),
                "emergency_request_id": (
                    emergency.id
                ),
                "emergency_request_created": (
                    created
                ),
                "detected_at": (
                    emergency.detected_at.isoformat()
                    if emergency.detected_at
                    else None
                ),
            },
        )

    # ========================================================
    # EMERGENCY CLEARANCE
    # ========================================================

    @staticmethod
    def _clear_emergency(
        *,
        junction: Junction,
        event: VehicleEvent,
        original_direction: str,
    ) -> None:
        emergency = (
            EmergencyRequest.objects
            .filter(
                junction=junction,
                vehicle_id=(
                    event.vehicle_id
                ),
                status=(
                    EmergencyStatus.ACTIVE
                ),
            )
            .first()
        )

        now = timezone.now()

        if emergency:
            emergency.status = (
                EmergencyStatus.CLEARED
            )

            emergency.cleared_at = (
                now
            )

            emergency.save(
                update_fields=[
                    "status",
                    "cleared_at",
                    "updated_at",
                ]
            )

        # ----------------------------------------------------
        # Determine whether another emergency remains active.
        # ----------------------------------------------------

        remaining_emergencies = (
            EmergencyRequest.objects
            .filter(
                junction=junction,
                status=(
                    EmergencyStatus.ACTIVE
                ),
            )
            .exists()
        )

        previous_mode = (
            junction.mode
        )

        # ----------------------------------------------------
        # If no emergency remains, restore control ownership.
        #
        # Priority:
        #
        # EMERGENCY > MANUAL > AUTOMATIC
        # ----------------------------------------------------

        if not remaining_emergencies:
            manual_override_exists = (
                ManualOverride.objects
                .filter(
                    junction=junction,
                    status=(
                        ManualOverrideStatus.ACTIVE
                    ),
                )
                .exists()
            )

            if manual_override_exists:
                restored_mode = (
                    JunctionMode.MANUAL
                )

            else:
                restored_mode = (
                    JunctionMode.AUTOMATIC
                )

            if (
                junction.mode
                != restored_mode
            ):
                junction.mode = (
                    restored_mode
                )

                junction.save(
                    update_fields=[
                        "mode",
                        "updated_at",
                    ]
                )

                AuditEvent.objects.create(
                    junction=junction,
                    event_type="MODE_CHANGE",
                    direction=(
                        original_direction
                    ),
                    vehicle_id=(
                        event.vehicle_id
                    ),
                    message=(
                        f"Emergency cleared; "
                        f"junction mode changed "
                        f"from {previous_mode} "
                        f"to {restored_mode}."
                    ),
                    metadata={
                        "previous_mode": (
                            previous_mode
                        ),
                        "new_mode": (
                            restored_mode
                        ),
                        "reason": (
                            "FINAL_EMERGENCY_CLEARED"
                        ),
                        "manual_override_active": (
                            manual_override_exists
                        ),
                    },
                )

        # ----------------------------------------------------
        # Audit emergency clearance itself
        # ----------------------------------------------------

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "EMERGENCY_CLEARED"
            ),
            direction=(
                original_direction
            ),
            vehicle_id=(
                event.vehicle_id
            ),
            message=(
                f"Emergency vehicle "
                f"{event.vehicle_id} cleared."
            ),
            metadata={
                "event_id": (
                    event.event_id
                ),
                "remaining_emergencies": (
                    remaining_emergencies
                ),
                "restored_mode": (
                    junction.mode
                    if not remaining_emergencies
                    else JunctionMode.EMERGENCY
                ),
            },
        )