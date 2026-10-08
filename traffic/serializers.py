from rest_framework import serializers

from traffic.models import (
    CommandStatus,
    Direction,
    Junction,
    SignalColor,
    VehicleEventType,
    VehicleType,
)


# ============================================================
# SENSOR EVENTS
# ============================================================


class SensorEventSerializer(serializers.Serializer):
    event_id = serializers.CharField(
        max_length=100,
    )

    junction_id = serializers.CharField(
        max_length=50,
    )

    direction = serializers.ChoiceField(
        choices=Direction.choices,
    )

    event_type = serializers.ChoiceField(
        choices=VehicleEventType.choices,
    )

    vehicle_id = serializers.CharField(
        max_length=100,
    )

    vehicle_type = serializers.ChoiceField(
        choices=VehicleType.choices,
    )

    sequence_no = serializers.IntegerField(
        min_value=0,
    )

    timestamp = serializers.DateTimeField()


# ============================================================
# JUNCTION
# ============================================================


class JunctionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Junction

        fields = [
            "junction_id",
            "name",
            "mode",
            "current_phase",
            "target_phase",
            "transition_stage",
            "transition_started_at",
            "transition_due_at",
            "controller_status",
            "normal_green_seconds",
            "yellow_seconds",
            "all_red_seconds",
            "max_wait_seconds",
            "state_version",
            "created_at",
            "updated_at",
        ]

        read_only_fields = fields


# ============================================================
# MANUAL JUNCTION COMMANDS
# ============================================================


class JunctionCommandSerializer(
    serializers.Serializer
):
    MANUAL_GREEN_REQUEST = (
        "MANUAL_GREEN_REQUEST"
    )

    RETURN_TO_AUTOMATIC = (
        "RETURN_TO_AUTOMATIC"
    )

    COMMAND_CHOICES = [
        (
            MANUAL_GREEN_REQUEST,
            "Manual Green Request",
        ),
        (
            RETURN_TO_AUTOMATIC,
            "Return To Automatic",
        ),
    ]

    command = serializers.ChoiceField(
        choices=COMMAND_CHOICES,
    )

    direction = serializers.ChoiceField(
        choices=Direction.choices,
        required=False,
        allow_null=True,
    )

    def validate(
        self,
        attrs,
    ):
        command = attrs.get(
            "command"
        )

        direction = attrs.get(
            "direction"
        )

        if (
            command
            == self.MANUAL_GREEN_REQUEST
            and not direction
        ):
            raise serializers.ValidationError(
                {
                    "direction": (
                        "This field is required for "
                        "MANUAL_GREEN_REQUEST."
                    )
                }
            )

        if (
            command
            == self.RETURN_TO_AUTOMATIC
            and direction is not None
        ):
            raise serializers.ValidationError(
                {
                    "direction": (
                        "Do not provide direction when "
                        "returning to automatic mode."
                    )
                }
            )

        return attrs


# ============================================================
# PHYSICAL CONTROLLER EVENTS / ACKS
# ============================================================


class ControllerEventSerializer(
    serializers.Serializer
):
    """
    Validates events received from the simulated physical
    signal controller.

    Examples:

    ACK:
    {
        "controller_event_id": "ctrl-001",
        "command_id": "cmd-001",
        "junction_id": "A",
        "status": "ACK",
        "actual_state": "GREEN"
    }

    FAILED:
    {
        "controller_event_id": "ctrl-002",
        "command_id": "cmd-002",
        "junction_id": "A",
        "status": "FAILED"
    }
    """

    controller_event_id = (
        serializers.CharField(
            max_length=100,
            required=False,
        )
    )

    command_id = serializers.CharField(
        max_length=100,
    )

    junction_id = serializers.CharField(
        max_length=50,
    )

    status = serializers.ChoiceField(
        choices=[
            CommandStatus.ACK,
            CommandStatus.FAILED,
            CommandStatus.TIMEOUT,
        ],
    )

    actual_state = serializers.ChoiceField(
        choices=SignalColor.choices,
        required=False,
        allow_null=True,
    )

    timestamp = serializers.DateTimeField(
        required=False,
        allow_null=True,
    )

    def validate(
        self,
        attrs,
    ):
        controller_status = attrs.get(
            "status"
        )

        actual_state = attrs.get(
            "actual_state"
        )

        # ACK means the physical controller is claiming
        # a real physical signal state, so it must provide it.
        if (
            controller_status
            == CommandStatus.ACK
            and not actual_state
        ):
            raise serializers.ValidationError(
                {
                    "actual_state": (
                        "actual_state is required "
                        "when status is ACK."
                    )
                }
            )

        # FAILED / TIMEOUT do not need actual_state.
        if (
            controller_status
            in {
                CommandStatus.FAILED,
                CommandStatus.TIMEOUT,
            }
            and actual_state is not None
        ):
            raise serializers.ValidationError(
                {
                    "actual_state": (
                        "Do not provide actual_state "
                        "for FAILED or TIMEOUT events."
                    )
                }
            )

        return attrs