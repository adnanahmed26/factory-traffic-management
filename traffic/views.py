from django.db import IntegrityError, transaction

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from traffic.models import (
    AuditEvent,
    CommandStatus,
    ControllerStatus,
    Direction,
    EmergencyStatus,
    Junction,
    JunctionMode,
    ManualOverrideStatus,
    SignalColor,
    SignalState,
    TrafficPhase,
    TransitionStage,
)

from traffic.serializers import (
    ControllerEventSerializer,
    JunctionCommandSerializer,
    JunctionSerializer,
    SensorEventSerializer,
)

from traffic.services.command_service import (
    CommandJunctionNotFoundError,
    CommandService,
    ManualCommandRejectedError,
)

from traffic.services.controller_service import (
    ControllerCommandNotFoundError,
    ControllerJunctionMismatchError,
    ControllerService,
    ControllerServiceError,
)

from traffic.services.sensor_service import (
    InvalidEventError,
    JunctionNotFoundError,
    SensorEventService,
)


# ============================================================
# SHARED HELPERS
# ============================================================


def get_junction_or_none(junction_id):
    """
    Return the requested junction or None.

    Keeping the lookup in one place avoids subtly different
    404 behavior between endpoints.
    """

    try:
        return Junction.objects.get(
            junction_id=junction_id
        )

    except Junction.DoesNotExist:
        return None


def parse_positive_integer(
    raw_value,
    *,
    field_name,
):
    """
    Parse an API value as a strictly positive integer.

    Accepted:
        30
        "30"

    Rejected:
        True
        False
        1.5
        "1.5"
        0
        -1
        ""
        None

    Returns:
        (value, None) on success
        (None, error_message) on failure
    """

    if isinstance(raw_value, bool):
        return (
            None,
            f"{field_name} must be a positive integer.",
        )

    if isinstance(raw_value, int):
        value = raw_value

    elif isinstance(raw_value, str):
        stripped_value = raw_value.strip()

        if not stripped_value.isdigit():
            return (
                None,
                f"{field_name} must be a positive integer.",
            )

        value = int(stripped_value)

    else:
        return (
            None,
            f"{field_name} must be a positive integer.",
        )

    if value <= 0:
        return (
            None,
            f"{field_name} must be greater than zero.",
        )

    return value, None


# ============================================================
# JUNCTION LIST / CREATE
# ============================================================


class JunctionListAPIView(APIView):
    """
    GET /api/junctions/

        Return all configured junctions.

    POST /api/junctions/

        Create a new junction in a conservative initial state.

        Newly created junctions begin with:

            mode               = AUTOMATIC
            phase              = ALL_RED
            transition_stage   = STABLE
            controller_status  = UNKNOWN

        Desired signal state is RED for every direction.

        Actual physical state is UNKNOWN until the controller
        explicitly confirms its state.
    """

    def get(self, request):
        junctions = Junction.objects.all().order_by(
            "junction_id"
        )

        serializer = JunctionSerializer(
            junctions,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        # ----------------------------------------------------
        # Junction ID
        # ----------------------------------------------------

        raw_junction_id = request.data.get(
            "junction_id",
            "",
        )

        if raw_junction_id is None:
            raw_junction_id = ""

        junction_id = str(
            raw_junction_id
        ).strip()

        if not junction_id:
            return Response(
                {
                    "detail": (
                        "junction_id is required."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(junction_id) > 50:
            return Response(
                {
                    "detail": (
                        "junction_id must be "
                        "50 characters or fewer."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ----------------------------------------------------
        # Name
        # ----------------------------------------------------

        raw_name = request.data.get(
            "name",
            f"Junction {junction_id}",
        )

        if raw_name is None:
            raw_name = ""

        name = str(
            raw_name
        ).strip()

        if not name:
            name = f"Junction {junction_id}"

        if len(name) > 100:
            return Response(
                {
                    "detail": (
                        "name must be "
                        "100 characters or fewer."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ----------------------------------------------------
        # Timing configuration
        # ----------------------------------------------------

        config_defaults = {
            "normal_green_seconds": 30,
            "yellow_seconds": 5,
            "all_red_seconds": 2,
            "max_wait_seconds": 120,
        }

        config = {}

        for field_name, default_value in (
            config_defaults.items()
        ):
            raw_value = request.data.get(
                field_name,
                default_value,
            )

            value, error_message = (
                parse_positive_integer(
                    raw_value,
                    field_name=field_name,
                )
            )

            if error_message:
                return Response(
                    {
                        "detail": error_message
                    },
                    status=(
                        status.HTTP_400_BAD_REQUEST
                    ),
                )

            config[field_name] = value

        # ----------------------------------------------------
        # Fast duplicate check
        # ----------------------------------------------------

        if Junction.objects.filter(
            junction_id=junction_id
        ).exists():
            return Response(
                {
                    "detail": (
                        f"Junction {junction_id} "
                        f"already exists."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        # ----------------------------------------------------
        # Atomic creation
        # ----------------------------------------------------

        try:
            with transaction.atomic():
                junction = Junction.objects.create(
                    junction_id=junction_id,
                    name=name,
                    mode=JunctionMode.AUTOMATIC,
                    current_phase=TrafficPhase.ALL_RED,
                    target_phase=None,
                    transition_stage=(
                        TransitionStage.STABLE
                    ),
                    transition_started_at=None,
                    transition_due_at=None,
                    controller_status=(
                        ControllerStatus.UNKNOWN
                    ),
                    normal_green_seconds=config[
                        "normal_green_seconds"
                    ],
                    yellow_seconds=config[
                        "yellow_seconds"
                    ],
                    all_red_seconds=config[
                        "all_red_seconds"
                    ],
                    max_wait_seconds=config[
                        "max_wait_seconds"
                    ],
                    state_version=0,
                )

                SignalState.objects.bulk_create(
                    [
                        SignalState(
                            junction=junction,
                            direction=direction,
                            desired_state=(
                                SignalColor.RED
                            ),
                            actual_state=(
                                SignalColor.UNKNOWN
                            ),
                            actual_confirmed_at=None,
                        )
                        for direction
                        in Direction.values
                    ]
                )

                AuditEvent.objects.create(
                    junction=junction,
                    event_type="JUNCTION_CREATED",
                    message=(
                        f"Junction {junction_id} "
                        f"created in conservative "
                        f"ALL_RED state."
                    ),
                    metadata={
                        "mode": (
                            JunctionMode.AUTOMATIC
                        ),
                        "current_phase": (
                            TrafficPhase.ALL_RED
                        ),
                        "transition_stage": (
                            TransitionStage.STABLE
                        ),
                        "controller_status": (
                            ControllerStatus.UNKNOWN
                        ),
                        "normal_green_seconds": (
                            config[
                                "normal_green_seconds"
                            ]
                        ),
                        "yellow_seconds": (
                            config[
                                "yellow_seconds"
                            ]
                        ),
                        "all_red_seconds": (
                            config[
                                "all_red_seconds"
                            ]
                        ),
                        "max_wait_seconds": (
                            config[
                                "max_wait_seconds"
                            ]
                        ),
                    },
                )

        except IntegrityError:
            # Handles the race where two requests pass the
            # pre-check simultaneously.
            return Response(
                {
                    "detail": (
                        f"Junction {junction_id} "
                        f"already exists."
                    )
                },
                status=status.HTTP_409_CONFLICT,
            )

        serializer = JunctionSerializer(
            junction
        )

        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED,
        )


# ============================================================
# JUNCTION DETAIL
# ============================================================


class JunctionDetailAPIView(APIView):
    """
    GET /api/junctions/<junction_id>/
    """

    def get(
        self,
        request,
        junction_id,
    ):
        junction = get_junction_or_none(
            junction_id
        )

        if junction is None:
            return Response(
                {
                    "detail": (
                        "Junction not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = JunctionSerializer(
            junction
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )


# ============================================================
# JUNCTION HISTORY
# ============================================================


class JunctionHistoryAPIView(APIView):
    """
    GET /api/junctions/<junction_id>/history/

    Optional query parameters:

        ?limit=50

        ?event_type=EMERGENCY_CLEARED

    Events are returned newest first.

    Ordering is deterministic:

        1. newest created_at first
        2. highest database ID first when timestamps tie
    """

    DEFAULT_LIMIT = 50
    MAX_LIMIT = 200

    def get(
        self,
        request,
        junction_id,
    ):
        junction = get_junction_or_none(
            junction_id
        )

        if junction is None:
            return Response(
                {
                    "detail": (
                        "Junction not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # ----------------------------------------------------
        # Limit
        # ----------------------------------------------------

        raw_limit = request.query_params.get(
            "limit",
            self.DEFAULT_LIMIT,
        )

        try:
            limit = int(raw_limit)

        except (
            TypeError,
            ValueError,
        ):
            return Response(
                {
                    "detail": (
                        "limit must be an integer."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if limit <= 0:
            return Response(
                {
                    "detail": (
                        "limit must be greater than zero."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        limit = min(
            limit,
            self.MAX_LIMIT,
        )

        # ----------------------------------------------------
        # Query
        # ----------------------------------------------------

        events = AuditEvent.objects.filter(
            junction=junction
        )

        event_type = request.query_params.get(
            "event_type"
        )

        if event_type:
            event_type = event_type.strip()

            if event_type:
                events = events.filter(
                    event_type=event_type
                )
            else:
                event_type = None

        # Deterministic newest-first ordering.
        #
        # SQLite timestamps may occasionally compare equal when
        # events are created extremely close together. Using
        # descending ID as a secondary key guarantees stable
        # audit ordering.
        events = events.order_by(
            "-created_at",
            "-id",
        )

        total_count = events.count()

        limited_events = events[:limit]

        history = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "direction": event.direction,
                "vehicle_id": (
                    event.vehicle_id
                    or None
                ),
                "command_id": (
                    event.command_id
                    or None
                ),
                "message": event.message,
                "metadata": event.metadata,
                "created_at": event.created_at,
            }
            for event in limited_events
        ]

        return Response(
            {
                "junction_id": (
                    junction.junction_id
                ),
                "total_count": total_count,
                "returned_count": len(history),
                "limit": limit,
                "event_type": event_type,
                "events": history,
            },
            status=status.HTTP_200_OK,
        )


# ============================================================
# JUNCTION STATUS
# ============================================================


class JunctionStatusAPIView(APIView):
    """
    GET /api/junctions/<junction_id>/status/

    Return current traffic-control state including:

    - operating mode
    - current phase
    - transition information
    - desired signal state
    - controller-confirmed actual state
    - queue counts
    - emergency state
    - manual override
    - pending physical-controller commands
    """

    def get(
        self,
        request,
        junction_id,
    ):
        try:
            junction = (
                Junction.objects
                .prefetch_related(
                    "signals",
                    "queue_entries",
                    "emergency_requests",
                    "manual_overrides",
                    "controller_commands",
                )
                .get(
                    junction_id=junction_id
                )
            )

        except Junction.DoesNotExist:
            return Response(
                {
                    "detail": (
                        "Junction not found."
                    )
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        # ----------------------------------------------------
        # Signals
        # ----------------------------------------------------

        signals = {}

        for signal in (
            junction.signals
            .all()
            .order_by(
                "direction"
            )
        ):
            signals[signal.direction] = {
                "desired": (
                    signal.desired_state
                ),
                "actual": (
                    signal.actual_state
                ),
                "confirmed_at": (
                    signal.actual_confirmed_at
                ),
            }

        # ----------------------------------------------------
        # Queues
        # ----------------------------------------------------

        queues = {
            direction: (
                junction.queue_entries
                .filter(
                    direction=direction
                )
                .count()
            )
            for direction
            in Direction.values
        }

        # ----------------------------------------------------
        # Active emergencies
        # ----------------------------------------------------

        active_emergencies = (
            junction.emergency_requests
            .filter(
                status=EmergencyStatus.ACTIVE
            )
            .values(
                "vehicle_id",
                "direction",
                "detected_at",
                "expires_at",
            )
            .order_by(
                "detected_at",
                "id",
            )
        )

        # ----------------------------------------------------
        # Active manual override
        # ----------------------------------------------------

        active_manual_override = (
            junction.manual_overrides
            .filter(
                status=ManualOverrideStatus.ACTIVE
            )
            .values(
                "id",
                "requested_direction",
                "requested_by",
                "requested_at",
                "expires_at",
            )
            .order_by(
                "-requested_at",
                "-id",
            )
            .first()
        )

        # ----------------------------------------------------
        # Pending physical commands
        # ----------------------------------------------------

        pending_commands = (
            junction.controller_commands
            .filter(
                status__in=[
                    CommandStatus.PENDING,
                    CommandStatus.SENT,
                ]
            )
            .values(
                "command_id",
                "command_type",
                "direction",
                "requested_state",
                "target_phase",
                "status",
                "attempt_count",
                "created_at",
                "sent_at",
                "expires_at",
            )
            .order_by(
                "created_at",
                "id",
            )
        )

        return Response(
            {
                "junction_id": (
                    junction.junction_id
                ),
                "name": (
                    junction.name
                ),
                "mode": (
                    junction.mode
                ),
                "phase": (
                    junction.current_phase
                ),
                "target_phase": (
                    junction.target_phase
                ),
                "transition_stage": (
                    junction.transition_stage
                ),
                "transition_started_at": (
                    junction.transition_started_at
                ),
                "transition_due_at": (
                    junction.transition_due_at
                ),
                "controller_status": (
                    junction.controller_status
                ),
                "signals": signals,
                "queues": queues,
                "active_emergencies": list(
                    active_emergencies
                ),
                "manual_override": (
                    active_manual_override
                ),
                "pending_commands": list(
                    pending_commands
                ),
                "state_version": (
                    junction.state_version
                ),
            },
            status=status.HTTP_200_OK,
        )


# ============================================================
# SENSOR EVENTS
# ============================================================


class SensorEventAPIView(APIView):
    """
    POST /api/sensor-events/

    Process:

        VEHICLE_ARRIVED
        VEHICLE_CLEARED

    SensorEventService owns idempotency, queue mutation,
    emergency detection, and event-order validation.
    """

    def post(
        self,
        request,
    ):
        serializer = SensorEventSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:
            result = (
                SensorEventService.process_event(
                    serializer.validated_data
                )
            )

        except JunctionNotFoundError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except InvalidEventError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        response_status = (
            status.HTTP_200_OK
            if result.duplicate
            else status.HTTP_201_CREATED
        )

        return Response(
            {
                "event_id": (
                    result.event_id
                ),
                "status": (
                    result.status
                ),
                "duplicate": (
                    result.duplicate
                ),
                "queue_changed": (
                    result.queue_changed
                ),
                "message": (
                    result.message
                ),
            },
            status=response_status,
        )


# ============================================================
# MANUAL JUNCTION COMMANDS
# ============================================================


class JunctionCommandAPIView(APIView):
    """
    POST /api/junctions/<junction_id>/commands/

    Supported commands:

        MANUAL_GREEN_REQUEST
        RETURN_TO_AUTOMATIC

    Manual requests express control intent only.

    CommandService and TrafficStateMachine remain responsible
    for determining and executing the safe transition.
    """

    def post(
        self,
        request,
        junction_id,
    ):
        serializer = JunctionCommandSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        command = (
            serializer.validated_data[
                "command"
            ]
        )

        direction = (
            serializer.validated_data.get(
                "direction"
            )
        )

        requested_by = ""

        if (
            hasattr(request, "user")
            and request.user
            and request.user.is_authenticated
        ):
            requested_by = (
                request.user.get_username()
            )

        try:
            result = (
                CommandService.process_command(
                    junction_id=junction_id,
                    command=command,
                    direction=direction,
                    requested_by=requested_by,
                )
            )

        except CommandJunctionNotFoundError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except ManualCommandRejectedError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_409_CONFLICT,
            )

        return Response(
            {
                "junction_id": (
                    result.junction_id
                ),
                "command": (
                    result.command
                ),
                "accepted": (
                    result.accepted
                ),
                "mode": (
                    result.mode
                ),
                "current_phase": (
                    result.current_phase
                ),
                "target_phase": (
                    result.target_phase
                ),
                "transition_stage": (
                    result.transition_stage
                ),
                "message": (
                    result.message
                ),
            },
            status=status.HTTP_200_OK,
        )


# ============================================================
# PHYSICAL CONTROLLER EVENT / ACK
# ============================================================


class ControllerEventAPIView(APIView):
    """
    POST /api/controller-events/

    Supported controller statuses:

        ACK
        FAILED
        TIMEOUT

    This endpoint updates controller-confirmed physical state.

    Desired state alone is never considered physical truth.
    """

    def post(
        self,
        request,
    ):
        serializer = ControllerEventSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:
            result = (
                ControllerService.process_event(
                    serializer.validated_data
                )
            )

        except ControllerCommandNotFoundError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        except ControllerJunctionMismatchError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_409_CONFLICT,
            )

        except ControllerServiceError as exc:
            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {
                "controller_event_id": (
                    result.controller_event_id
                ),
                "command_id": (
                    result.command_id
                ),
                "status": (
                    result.status
                ),
                "actual_state": (
                    result.actual_state
                ),
                "matched_desired_state": (
                    result.matched_desired_state
                ),
                "duplicate": (
                    result.duplicate
                ),
                "message": (
                    result.message
                ),
            },
            status=status.HTTP_200_OK,
        )