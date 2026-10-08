from django.db import models


# ============================================================
# ENUMS / DOMAIN CONSTANTS
# ============================================================


class Direction(models.TextChoices):
    NORTH = "NORTH", "North"
    SOUTH = "SOUTH", "South"
    EAST = "EAST", "East"
    WEST = "WEST", "West"


class SignalColor(models.TextChoices):
    RED = "RED", "Red"
    YELLOW = "YELLOW", "Yellow"
    GREEN = "GREEN", "Green"
    UNKNOWN = "UNKNOWN", "Unknown"


class JunctionMode(models.TextChoices):
    AUTOMATIC = "AUTOMATIC", "Automatic"
    MANUAL = "MANUAL", "Manual"
    EMERGENCY = "EMERGENCY", "Emergency"
    DEGRADED = "DEGRADED", "Degraded / Failure"


class TrafficPhase(models.TextChoices):
    NORTH_SOUTH = "NORTH_SOUTH", "North / South"
    EAST_WEST = "EAST_WEST", "East / West"
    ALL_RED = "ALL_RED", "All Red"


class TransitionStage(models.TextChoices):
    STABLE = "STABLE", "Stable"
    YELLOW = "YELLOW", "Yellow Clearance"
    ALL_RED = "ALL_RED", "All Red Clearance"
    APPLYING_TARGET = "APPLYING_TARGET", "Applying Target Phase"


class VehicleType(models.TextChoices):
    EMERGENCY = "EMERGENCY", "Emergency"
    TRUCK = "TRUCK", "Truck"
    FORKLIFT = "FORKLIFT", "Forklift"
    EMPLOYEE_VEHICLE = "EMPLOYEE_VEHICLE", "Employee Vehicle"


class VehicleEventType(models.TextChoices):
    VEHICLE_ARRIVED = "VEHICLE_ARRIVED", "Vehicle Arrived"
    VEHICLE_CLEARED = "VEHICLE_CLEARED", "Vehicle Cleared"


class ControllerStatus(models.TextChoices):
    ONLINE = "ONLINE", "Online"
    OFFLINE = "OFFLINE", "Offline"
    DEGRADED = "DEGRADED", "Degraded"
    UNKNOWN = "UNKNOWN", "Unknown"


class DeviceType(models.TextChoices):
    SIGNAL_CONTROLLER = "SIGNAL_CONTROLLER", "Signal Controller"
    SIGNAL = "SIGNAL", "Signal"
    SENSOR = "SENSOR", "Sensor"


class DeviceStatusValue(models.TextChoices):
    ONLINE = "ONLINE", "Online"
    OFFLINE = "OFFLINE", "Offline"
    DEGRADED = "DEGRADED", "Degraded"
    WARNING = "WARNING", "Warning"
    UNKNOWN = "UNKNOWN", "Unknown"


class CommandStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SENT = "SENT", "Sent"
    ACK = "ACK", "Acknowledged"
    FAILED = "FAILED", "Failed"
    TIMEOUT = "TIMEOUT", "Timed Out"
    CANCELLED = "CANCELLED", "Cancelled"


class CommandType(models.TextChoices):
    SIGNAL_CHANGE = "SIGNAL_CHANGE", "Signal Change"
    PHASE_CHANGE = "PHASE_CHANGE", "Phase Change"
    ALL_RED = "ALL_RED", "All Red"


class EmergencyStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    CLEARED = "CLEARED", "Cleared"
    EXPIRED = "EXPIRED", "Expired"


class ManualOverrideStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    RELEASED = "RELEASED", "Released"
    EXPIRED = "EXPIRED", "Expired"


# ============================================================
# JUNCTION
# ============================================================


class Junction(models.Model):
    """
    Persistent state for one physical junction.

    Traffic-control logic should operate on this state through the
    service/domain layer rather than letting API clients directly
    modify signal states.
    """

    junction_id = models.CharField(
        max_length=50,
        unique=True,
        db_index=True,
    )

    name = models.CharField(
        max_length=100,
        default="Junction",
    )

    mode = models.CharField(
        max_length=20,
        choices=JunctionMode.choices,
        default=JunctionMode.AUTOMATIC,
        db_index=True,
    )

    current_phase = models.CharField(
        max_length=30,
        choices=TrafficPhase.choices,
        default=TrafficPhase.ALL_RED,
    )

    # During a safe transition we persist where we are trying to go.
    target_phase = models.CharField(
        max_length=30,
        choices=TrafficPhase.choices,
        null=True,
        blank=True,
    )

    transition_stage = models.CharField(
        max_length=30,
        choices=TransitionStage.choices,
        default=TransitionStage.STABLE,
    )

    transition_started_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    transition_due_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    controller_status = models.CharField(
        max_length=20,
        choices=ControllerStatus.choices,
        default=ControllerStatus.UNKNOWN,
    )

    # Configuration values are stored instead of being deeply
    # hard-coded into the state machine.
    normal_green_seconds = models.PositiveIntegerField(
        default=30,
    )

    yellow_seconds = models.PositiveIntegerField(
        default=5,
    )

    all_red_seconds = models.PositiveIntegerField(
        default=2,
    )

    # Useful for starvation protection in automatic scheduling.
    max_wait_seconds = models.PositiveIntegerField(
        default=120,
    )

    # Can later support optimistic concurrency if needed.
    state_version = models.PositiveBigIntegerField(
        default=0,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["junction_id"]

    def __str__(self):
        return f"{self.junction_id} - {self.name}"


# ============================================================
# SIGNAL STATE
# ============================================================


class SignalState(models.Model):
    """
    Desired state = what the backend wants.
    Actual state = what the physical controller has confirmed.

    These MUST remain separate.
    """

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="signals",
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )

    desired_state = models.CharField(
        max_length=10,
        choices=SignalColor.choices,
        default=SignalColor.RED,
    )

    actual_state = models.CharField(
        max_length=10,
        choices=SignalColor.choices,
        default=SignalColor.UNKNOWN,
    )

    actual_confirmed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["junction", "direction"],
                name="unique_signal_direction_per_junction",
            )
        ]

        indexes = [
            models.Index(
                fields=["junction", "direction"],
                name="signal_junction_direction_idx",
            )
        ]

        ordering = ["junction", "direction"]

    def __str__(self):
        return (
            f"{self.junction.junction_id} "
            f"{self.direction} "
            f"(desired={self.desired_state}, actual={self.actual_state})"
        )


# ============================================================
# SENSOR / VEHICLE EVENTS
# ============================================================


class VehicleEvent(models.Model):
    """
    Immutable-ish record of an incoming sensor event.

    event_id is unique so repeated submission of the same event
    cannot mutate traffic state multiple times.
    """

    event_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="vehicle_events",
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )

    event_type = models.CharField(
        max_length=30,
        choices=VehicleEventType.choices,
    )

    vehicle_id = models.CharField(
        max_length=100,
    )

    vehicle_type = models.CharField(
        max_length=30,
        choices=VehicleType.choices,
    )

    sequence_no = models.BigIntegerField()

    # Timestamp supplied by the sensor/device.
    sensor_timestamp = models.DateTimeField()

    # Timestamp assigned by our backend.
    received_at = models.DateTimeField(
        auto_now_add=True,
    )

    processed = models.BooleanField(
        default=False,
        db_index=True,
    )

    rejected = models.BooleanField(
        default=False,
    )

    rejection_reason = models.TextField(
        blank=True,
        default="",
    )

    processed_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["junction", "direction", "received_at"],
                name="vehicle_event_lookup_idx",
            ),
            models.Index(
                fields=["junction", "vehicle_id"],
                name="vehicle_event_vehicle_idx",
            ),
            models.Index(
                fields=["junction", "sequence_no"],
                name="vehicle_event_sequence_idx",
            ),
        ]

        ordering = ["received_at"]

    def __str__(self):
        return (
            f"{self.event_id} - "
            f"{self.event_type} - "
            f"{self.vehicle_id}"
        )


# ============================================================
# ACTIVE VEHICLE QUEUE
# ============================================================


class VehicleQueueEntry(models.Model):
    """
    Represents a vehicle that is CURRENTLY waiting.

    VehicleEvent = historical input.
    VehicleQueueEntry = current queue state.

    A vehicle disappears from this table after a valid
    VEHICLE_CLEARED event.
    """

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="queue_entries",
    )

    vehicle_id = models.CharField(
        max_length=100,
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )

    vehicle_type = models.CharField(
        max_length=30,
        choices=VehicleType.choices,
    )

    arrived_at = models.DateTimeField()

    last_sequence_no = models.BigIntegerField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["junction", "vehicle_id"],
                name="unique_waiting_vehicle_per_junction",
            )
        ]

        indexes = [
            models.Index(
                fields=["junction", "direction", "arrived_at"],
                name="queue_direction_wait_idx",
            ),
            models.Index(
                fields=["junction", "vehicle_type"],
                name="queue_vehicle_type_idx",
            ),
        ]

        ordering = ["arrived_at", "id"]

    def __str__(self):
        return (
            f"{self.junction.junction_id} - "
            f"{self.direction} - "
            f"{self.vehicle_id}"
        )


# ============================================================
# EMERGENCY REQUESTS
# ============================================================


class EmergencyRequest(models.Model):
    """
    Persisted emergency state.

    Emergency handling will still use the same safe transition
    mechanism as normal/manual traffic.
    """

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="emergency_requests",
    )

    vehicle_id = models.CharField(
        max_length=100,
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )

    status = models.CharField(
        max_length=20,
        choices=EmergencyStatus.choices,
        default=EmergencyStatus.ACTIVE,
        db_index=True,
    )

    detected_event = models.ForeignKey(
        VehicleEvent,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="emergency_requests",
    )

    detected_at = models.DateTimeField()

    cleared_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["junction", "status", "detected_at"],
                name="emergency_active_idx",
            )
        ]

        ordering = ["detected_at"]

    def __str__(self):
        return (
            f"{self.junction.junction_id} - "
            f"{self.vehicle_id} - "
            f"{self.status}"
        )


# ============================================================
# MANUAL OVERRIDE
# ============================================================


class ManualOverride(models.Model):
    """
    Stores administrator/manual-control intent.

    The requested direction does NOT directly set a signal GREEN.
    The state machine must safely transition to the corresponding
    phase.
    """

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="manual_overrides",
    )

    requested_direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
    )

    status = models.CharField(
        max_length=20,
        choices=ManualOverrideStatus.choices,
        default=ManualOverrideStatus.ACTIVE,
        db_index=True,
    )

    requested_by = models.CharField(
        max_length=150,
        blank=True,
        default="",
    )

    requested_at = models.DateTimeField(
        auto_now_add=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    released_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["junction", "status"],
                name="manual_override_status_idx",
            )
        ]

        ordering = ["-requested_at"]

    def __str__(self):
        return (
            f"{self.junction.junction_id} - "
            f"{self.requested_direction} - "
            f"{self.status}"
        )


# ============================================================
# CONTROLLER COMMANDS
# ============================================================


class ControllerCommand(models.Model):
    """
    Command requested by the backend from the physical controller.

    Creation of a command does NOT imply that the physical controller
    actually performed it.
    """

    command_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="controller_commands",
    )

    command_type = models.CharField(
        max_length=30,
        choices=CommandType.choices,
        default=CommandType.SIGNAL_CHANGE,
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
        null=True,
        blank=True,
    )

    requested_state = models.CharField(
        max_length=10,
        choices=SignalColor.choices,
        null=True,
        blank=True,
    )

    target_phase = models.CharField(
        max_length=30,
        choices=TrafficPhase.choices,
        null=True,
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=CommandStatus.choices,
        default=CommandStatus.PENDING,
        db_index=True,
    )

    actual_state = models.CharField(
        max_length=10,
        choices=SignalColor.choices,
        null=True,
        blank=True,
    )

    attempt_count = models.PositiveIntegerField(
        default=0,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    sent_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    acknowledged_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    expires_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    last_error = models.TextField(
        blank=True,
        default="",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["junction", "status", "created_at"],
                name="controller_pending_idx",
            )
        ]

        ordering = ["created_at"]

    def __str__(self):
        return (
            f"{self.command_id} - "
            f"{self.junction.junction_id} - "
            f"{self.status}"
        )


# ============================================================
# CONTROLLER ACK / EVENT
# ============================================================


class ControllerEvent(models.Model):
    """
    Incoming acknowledgement/status event from the simulated or
    physical controller.

    controller_event_id gives us idempotency for repeated controller
    messages in the same way event_id does for sensor events.
    """

    controller_event_id = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
    )

    command = models.ForeignKey(
        ControllerCommand,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="controller_events",
    )

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="controller_events",
    )

    status = models.CharField(
        max_length=20,
        choices=CommandStatus.choices,
    )

    actual_state = models.CharField(
        max_length=10,
        choices=SignalColor.choices,
        null=True,
        blank=True,
    )

    controller_timestamp = models.DateTimeField(
        null=True,
        blank=True,
    )

    received_at = models.DateTimeField(
        auto_now_add=True,
    )

    processed = models.BooleanField(
        default=False,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["junction", "received_at"],
                name="controller_event_time_idx",
            )
        ]

        ordering = ["received_at"]

    def __str__(self):
        return self.controller_event_id


# ============================================================
# DEVICE STATUS / FAILURE
# ============================================================


class DeviceStatus(models.Model):
    """
    Current known health/state of controllers, signals, and sensors.
    """

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="device_statuses",
    )

    device_type = models.CharField(
        max_length=30,
        choices=DeviceType.choices,
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
        null=True,
        blank=True,
    )

    device_identifier = models.CharField(
        max_length=100,
        default="default",
    )

    status = models.CharField(
        max_length=20,
        choices=DeviceStatusValue.choices,
        default=DeviceStatusValue.UNKNOWN,
    )

    last_seen_at = models.DateTimeField(
        null=True,
        blank=True,
    )

    details = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "junction",
                    "device_type",
                    "device_identifier",
                ],
                name="unique_device_per_junction",
            )
        ]

        indexes = [
            models.Index(
                fields=["junction", "status"],
                name="device_status_lookup_idx",
            )
        ]

    def __str__(self):
        return (
            f"{self.junction.junction_id} - "
            f"{self.device_type} - "
            f"{self.device_identifier}"
        )


# ============================================================
# AUDIT / HISTORY
# ============================================================


class AuditEvent(models.Model):
    """
    Human-readable event history explaining how the system reached
    its current state.
    """

    junction = models.ForeignKey(
        Junction,
        on_delete=models.CASCADE,
        related_name="audit_events",
    )

    event_type = models.CharField(
        max_length=100,
        db_index=True,
    )

    direction = models.CharField(
        max_length=10,
        choices=Direction.choices,
        null=True,
        blank=True,
    )

    vehicle_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    command_id = models.CharField(
        max_length=100,
        blank=True,
        default="",
    )

    message = models.TextField()

    metadata = models.JSONField(
        default=dict,
        blank=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
        db_index=True,
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["junction", "-created_at"],
                name="audit_junction_time_idx",
            ),
            models.Index(
                fields=["junction", "event_type"],
                name="audit_event_type_idx",
            ),
        ]

        ordering = ["-created_at"]

    def __str__(self):
        return (
            f"{self.junction.junction_id} - "
            f"{self.event_type} - "
            f"{self.created_at}"
        )