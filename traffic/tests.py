from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from rest_framework.test import APIClient

from traffic.domain.scheduler import TrafficScheduler
from traffic.domain.state_machine import (
    InvalidTransitionError,
    TrafficStateMachine,
)

from traffic.models import (
    AuditEvent,
    CommandStatus,
    ControllerCommand,
    ControllerStatus,
    Direction,
    EmergencyRequest,
    EmergencyStatus,
    Junction,
    JunctionMode,
    ManualOverride,
    ManualOverrideStatus,
    SignalColor,
    SignalState,
    TrafficPhase,
    TransitionStage,
    VehicleQueueEntry,
    VehicleType,
)


class TrafficSystemTestCase(TestCase):
    """
    Core regression tests for the Factory Traffic Management System.

    These tests focus on safety-critical and assessment-relevant
    backend behavior rather than UI behavior.
    """

    def setUp(self):
        self.client = APIClient()

    # ========================================================
    # HELPERS
    # ========================================================

    def create_junction(
        self,
        junction_id="A",
        *,
        mode=JunctionMode.AUTOMATIC,
        current_phase=TrafficPhase.ALL_RED,
        controller_status=ControllerStatus.ONLINE,
    ):
        junction = Junction.objects.create(
            junction_id=junction_id,
            name=f"Junction {junction_id}",
            mode=mode,
            current_phase=current_phase,
            target_phase=None,
            transition_stage=TransitionStage.STABLE,
            controller_status=controller_status,
            normal_green_seconds=30,
            yellow_seconds=5,
            all_red_seconds=2,
            max_wait_seconds=120,
        )

        for direction in Direction.values:
            SignalState.objects.create(
                junction=junction,
                direction=direction,
                desired_state=SignalColor.RED,
                actual_state=SignalColor.RED,
                actual_confirmed_at=timezone.now(),
            )

        return junction

    def configure_east_west_green(
        self,
        junction,
    ):
        """
        Put the junction in a physically confirmed
        EAST_WEST green state.
        """

        for direction in [
            Direction.EAST,
            Direction.WEST,
        ]:
            signal = SignalState.objects.get(
                junction=junction,
                direction=direction,
            )

            signal.desired_state = SignalColor.GREEN
            signal.actual_state = SignalColor.GREEN
            signal.actual_confirmed_at = timezone.now()

            signal.save(
                update_fields=[
                    "desired_state",
                    "actual_state",
                    "actual_confirmed_at",
                    "updated_at",
                ]
            )

        for direction in [
            Direction.NORTH,
            Direction.SOUTH,
        ]:
            signal = SignalState.objects.get(
                junction=junction,
                direction=direction,
            )

            signal.desired_state = SignalColor.RED
            signal.actual_state = SignalColor.RED
            signal.actual_confirmed_at = timezone.now()

            signal.save(
                update_fields=[
                    "desired_state",
                    "actual_state",
                    "actual_confirmed_at",
                    "updated_at",
                ]
            )

    # ========================================================
    # JUNCTION CREATION
    # ========================================================

    def test_create_junction_starts_in_safe_state(self):
        response = self.client.post(
            "/api/junctions/",
            {
                "junction_id": "B",
                "name": "Junction B",
                "normal_green_seconds": 30,
                "yellow_seconds": 5,
                "all_red_seconds": 2,
                "max_wait_seconds": 120,
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            201,
        )

        junction = Junction.objects.get(
            junction_id="B"
        )

        self.assertEqual(
            junction.mode,
            JunctionMode.AUTOMATIC,
        )

        self.assertEqual(
            junction.current_phase,
            TrafficPhase.ALL_RED,
        )

        self.assertEqual(
            junction.transition_stage,
            TransitionStage.STABLE,
        )

        self.assertEqual(
            junction.controller_status,
            ControllerStatus.UNKNOWN,
        )

        signals = SignalState.objects.filter(
            junction=junction
        )

        self.assertEqual(
            signals.count(),
            4,
        )

        for signal in signals:
            self.assertEqual(
                signal.desired_state,
                SignalColor.RED,
            )

            self.assertEqual(
                signal.actual_state,
                SignalColor.UNKNOWN,
            )

            self.assertIsNone(
                signal.actual_confirmed_at
            )

        self.assertTrue(
            AuditEvent.objects.filter(
                junction=junction,
                event_type="JUNCTION_CREATED",
            ).exists()
        )

    def test_duplicate_junction_is_rejected(self):
        self.create_junction(
            junction_id="A"
        )

        response = self.client.post(
            "/api/junctions/",
            {
                "junction_id": "A",
                "name": "Duplicate Junction",
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            409,
        )

        self.assertEqual(
            Junction.objects.filter(
                junction_id="A"
            ).count(),
            1,
        )

    # ========================================================
    # HISTORY
    # ========================================================

    def test_history_returns_newest_events_first(self):
        junction = self.create_junction(
            junction_id="A"
        )

        first = AuditEvent.objects.create(
            junction=junction,
            event_type="TEST_EVENT_FIRST",
            message="First event.",
        )

        second = AuditEvent.objects.create(
            junction=junction,
            event_type="TEST_EVENT_SECOND",
            message="Second event.",
        )

        response = self.client.get(
            "/api/junctions/A/history/?limit=10"
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.data["junction_id"],
            "A",
        )

        self.assertEqual(
            response.data["returned_count"],
            2,
        )

        events = response.data["events"]

        self.assertEqual(
            events[0]["id"],
            second.id,
        )

        self.assertEqual(
            events[1]["id"],
            first.id,
        )

    def test_history_event_type_filter(self):
        junction = self.create_junction(
            junction_id="A"
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type="EMERGENCY_CLEARED",
            message="Emergency cleared.",
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type="OTHER_EVENT",
            message="Other event.",
        )

        response = self.client.get(
            (
                "/api/junctions/A/history/"
                "?event_type=EMERGENCY_CLEARED"
            )
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.data["returned_count"],
            1,
        )

        self.assertEqual(
            response.data["events"][0]["event_type"],
            "EMERGENCY_CLEARED",
        )

    # ========================================================
    # SENSOR IDEMPOTENCY
    # ========================================================

    def test_duplicate_sensor_event_is_idempotent(self):
        self.create_junction(
            junction_id="A"
        )

        payload = {
            "event_id": "evt-duplicate-001",
            "junction_id": "A",
            "direction": "NORTH",
            "event_type": "VEHICLE_ARRIVED",
            "vehicle_id": "TRUCK-001",
            "vehicle_type": "TRUCK",
            "sequence_no": 1,
            "timestamp": (
                timezone.now().isoformat()
            ),
        }

        first_response = self.client.post(
            "/api/sensor-events/",
            payload,
            format="json",
        )

        second_response = self.client.post(
            "/api/sensor-events/",
            payload,
            format="json",
        )

        self.assertEqual(
            first_response.status_code,
            201,
        )

        self.assertEqual(
            second_response.status_code,
            200,
        )

        self.assertFalse(
            first_response.data["duplicate"]
        )

        self.assertTrue(
            second_response.data["duplicate"]
        )

        self.assertEqual(
            VehicleQueueEntry.objects.filter(
                junction__junction_id="A",
                vehicle_id="TRUCK-001",
            ).count(),
            1,
        )

    # ========================================================
    # EMERGENCY DETECTION
    # ========================================================

    def test_emergency_arrival_sets_emergency_mode(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.EAST_WEST,
            controller_status=ControllerStatus.ONLINE,
        )

        self.configure_east_west_green(
            junction
        )

        response = self.client.post(
            "/api/sensor-events/",
            {
                "event_id": "evt-emergency-001",
                "junction_id": "A",
                "direction": "NORTH",
                "event_type": "VEHICLE_ARRIVED",
                "vehicle_id": "AMB-001",
                "vehicle_type": "EMERGENCY",
                "sequence_no": 1,
                "timestamp": (
                    timezone.now().isoformat()
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            201,
        )

        junction.refresh_from_db()

        self.assertEqual(
            junction.mode,
            JunctionMode.EMERGENCY,
        )

        self.assertTrue(
            EmergencyRequest.objects.filter(
                junction=junction,
                vehicle_id="AMB-001",
                direction=Direction.NORTH,
                status=EmergencyStatus.ACTIVE,
            ).exists()
        )

        # Sensor detection itself must never directly
        # manipulate the physical signal state.
        north = SignalState.objects.get(
            junction=junction,
            direction=Direction.NORTH,
        )

        self.assertEqual(
            north.actual_state,
            SignalColor.RED,
        )

    def test_emergency_preemption_uses_safe_transition(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.EAST_WEST,
            controller_status=ControllerStatus.ONLINE,
        )

        self.configure_east_west_green(
            junction
        )

        response = self.client.post(
            "/api/sensor-events/",
            {
                "event_id": "evt-emergency-002",
                "junction_id": "A",
                "direction": "NORTH",
                "event_type": "VEHICLE_ARRIVED",
                "vehicle_id": "AMB-002",
                "vehicle_type": "EMERGENCY",
                "sequence_no": 1,
                "timestamp": (
                    timezone.now().isoformat()
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            201,
        )

        out = StringIO()

        call_command(
            "run_traffic_controller",
            "--once",
            stdout=out,
        )

        junction.refresh_from_db()

        self.assertEqual(
            junction.mode,
            JunctionMode.EMERGENCY,
        )

        self.assertEqual(
            junction.current_phase,
            TrafficPhase.EAST_WEST,
        )

        self.assertEqual(
            junction.target_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertEqual(
            junction.transition_stage,
            TransitionStage.YELLOW,
        )

        east = SignalState.objects.get(
            junction=junction,
            direction=Direction.EAST,
        )

        west = SignalState.objects.get(
            junction=junction,
            direction=Direction.WEST,
        )

        north = SignalState.objects.get(
            junction=junction,
            direction=Direction.NORTH,
        )

        south = SignalState.objects.get(
            junction=junction,
            direction=Direction.SOUTH,
        )

        self.assertEqual(
            east.desired_state,
            SignalColor.YELLOW,
        )

        self.assertEqual(
            west.desired_state,
            SignalColor.YELLOW,
        )

        self.assertEqual(
            north.desired_state,
            SignalColor.RED,
        )

        self.assertEqual(
            south.desired_state,
            SignalColor.RED,
        )

    # ========================================================
    # CONTROLLER TIMEOUT
    # ========================================================

    def test_timed_out_command_marks_controller_offline(self):
        junction = self.create_junction(
            junction_id="A",
            controller_status=ControllerStatus.ONLINE,
        )

        ControllerCommand.objects.create(
            command_id="cmd-timeout-001",
            junction=junction,
            direction=Direction.NORTH,
            requested_state=SignalColor.RED,
            status=CommandStatus.PENDING,
            expires_at=(
                timezone.now()
                - timedelta(seconds=1)
            ),
        )

        out = StringIO()

        call_command(
            "run_traffic_controller",
            "--once",
            stdout=out,
        )

        junction.refresh_from_db()

        command = ControllerCommand.objects.get(
            command_id="cmd-timeout-001"
        )

        self.assertEqual(
            command.status,
            CommandStatus.TIMEOUT,
        )

        self.assertEqual(
            junction.controller_status,
            ControllerStatus.OFFLINE,
        )

    # ========================================================
    # LATE ACK RECOVERY
    # ========================================================

    def test_late_ack_recovers_controller(self):
        junction = self.create_junction(
            junction_id="A",
            controller_status=ControllerStatus.OFFLINE,
        )

        north = SignalState.objects.get(
            junction=junction,
            direction=Direction.NORTH,
        )

        north.desired_state = SignalColor.GREEN
        north.actual_state = SignalColor.RED

        north.save(
            update_fields=[
                "desired_state",
                "actual_state",
                "updated_at",
            ]
        )

        ControllerCommand.objects.create(
            command_id="cmd-late-ack-001",
            junction=junction,
            direction=Direction.NORTH,
            requested_state=SignalColor.GREEN,
            status=CommandStatus.TIMEOUT,
            expires_at=(
                timezone.now()
                - timedelta(seconds=10)
            ),
            last_error=(
                "Controller acknowledgement timeout."
            ),
        )

        response = self.client.post(
            "/api/controller-events/",
            {
                "controller_event_id": (
                    "ctrl-late-ack-001"
                ),
                "command_id": (
                    "cmd-late-ack-001"
                ),
                "junction_id": "A",
                "status": "ACK",
                "actual_state": "GREEN",
                "timestamp": (
                    timezone.now().isoformat()
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertTrue(
            response.data[
                "matched_desired_state"
            ]
        )

        junction.refresh_from_db()
        north.refresh_from_db()

        self.assertEqual(
            junction.controller_status,
            ControllerStatus.ONLINE,
        )

        self.assertEqual(
            north.actual_state,
            SignalColor.GREEN,
        )

        command = ControllerCommand.objects.get(
            command_id="cmd-late-ack-001"
        )

        self.assertEqual(
            command.status,
            CommandStatus.ACK,
        )

        self.assertTrue(
            AuditEvent.objects.filter(
                junction=junction,
                event_type=(
                    "CONTROLLER_LATE_ACK_RECOVERY"
                ),
            ).exists()
        )

    # ========================================================
    # UNKNOWN CONTROLLER FAIL SAFE
    # ========================================================

    def test_unknown_controller_does_not_generate_commands(self):
        junction = self.create_junction(
            junction_id="B",
            current_phase=TrafficPhase.ALL_RED,
            controller_status=ControllerStatus.UNKNOWN,
        )

        SignalState.objects.filter(
            junction=junction
        ).update(
            desired_state=SignalColor.RED,
            actual_state=SignalColor.UNKNOWN,
            actual_confirmed_at=None,
        )

        VehicleQueueEntry.objects.create(
            junction=junction,
            vehicle_id="TRUCK-B-001",
            direction=Direction.NORTH,
            vehicle_type=VehicleType.TRUCK,
            arrived_at=timezone.now(),
            last_sequence_no=1,
        )

        out = StringIO()

        call_command(
            "run_traffic_controller",
            "--once",
            stdout=out,
        )

        junction.refresh_from_db()

        self.assertEqual(
            junction.controller_status,
            ControllerStatus.UNKNOWN,
        )

        self.assertEqual(
            junction.current_phase,
            TrafficPhase.ALL_RED,
        )

        self.assertEqual(
            junction.transition_stage,
            TransitionStage.STABLE,
        )

        self.assertEqual(
            ControllerCommand.objects.filter(
                junction=junction
            ).count(),
            0,
        )

    # ========================================================
    # PHYSICAL ALL-RED SAFETY
    # ========================================================

    def test_all_red_does_not_request_green_without_physical_confirmation(
        self,
    ):
        """
        Logical ALL_RED alone is not enough to permit GREEN.

        Even when the controller is marked ONLINE, all four
        physical signal states must have explicitly confirmed
        RED before target GREEN commands may be created.
        """

        junction = self.create_junction(
            junction_id="SAFE-A",
            current_phase=TrafficPhase.ALL_RED,
            controller_status=ControllerStatus.ONLINE,
        )

        SignalState.objects.filter(
            junction=junction
        ).update(
            desired_state=SignalColor.RED,
            actual_state=SignalColor.UNKNOWN,
            actual_confirmed_at=None,
        )

        with self.assertRaises(
            InvalidTransitionError
        ):
            TrafficStateMachine.request_phase(
                junction_id="SAFE-A",
                target_phase=(
                    TrafficPhase.NORTH_SOUTH
                ),
            )

        junction.refresh_from_db()

        self.assertEqual(
            junction.current_phase,
            TrafficPhase.ALL_RED,
        )

        self.assertIsNone(
            junction.target_phase
        )

        self.assertEqual(
            junction.transition_stage,
            TransitionStage.STABLE,
        )

        self.assertIsNone(
            junction.transition_started_at
        )

        self.assertEqual(
            ControllerCommand.objects.filter(
                junction=junction,
                requested_state=SignalColor.GREEN,
            ).count(),
            0,
        )

        for signal in SignalState.objects.filter(
            junction=junction
        ):
            self.assertEqual(
                signal.desired_state,
                SignalColor.RED,
            )

            self.assertEqual(
                signal.actual_state,
                SignalColor.UNKNOWN,
            )

    def test_all_red_can_request_green_after_physical_red_confirmation(
        self,
    ):
        """
        Once all four physical signal states explicitly confirm
        RED, the state machine may request the target GREEN
        phase while keeping current_phase at ALL_RED until
        controller acknowledgement arrives.
        """

        junction = self.create_junction(
            junction_id="SAFE-B",
            current_phase=TrafficPhase.ALL_RED,
            controller_status=ControllerStatus.ONLINE,
        )

        confirmed_at = timezone.now()

        SignalState.objects.filter(
            junction=junction
        ).update(
            desired_state=SignalColor.RED,
            actual_state=SignalColor.RED,
            actual_confirmed_at=confirmed_at,
        )

        result = TrafficStateMachine.request_phase(
            junction_id="SAFE-B",
            target_phase=TrafficPhase.NORTH_SOUTH,
        )

        junction.refresh_from_db()

        self.assertEqual(
            result.stage,
            TransitionStage.APPLYING_TARGET,
        )

        self.assertEqual(
            result.current_phase,
            TrafficPhase.ALL_RED,
        )

        self.assertEqual(
            result.target_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertEqual(
            junction.current_phase,
            TrafficPhase.ALL_RED,
        )

        self.assertEqual(
            junction.target_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertEqual(
            junction.transition_stage,
            TransitionStage.APPLYING_TARGET,
        )

        self.assertIsNotNone(
            junction.transition_started_at
        )

        north = SignalState.objects.get(
            junction=junction,
            direction=Direction.NORTH,
        )

        south = SignalState.objects.get(
            junction=junction,
            direction=Direction.SOUTH,
        )

        east = SignalState.objects.get(
            junction=junction,
            direction=Direction.EAST,
        )

        west = SignalState.objects.get(
            junction=junction,
            direction=Direction.WEST,
        )

        self.assertEqual(
            north.desired_state,
            SignalColor.GREEN,
        )

        self.assertEqual(
            south.desired_state,
            SignalColor.GREEN,
        )

        self.assertEqual(
            east.desired_state,
            SignalColor.RED,
        )

        self.assertEqual(
            west.desired_state,
            SignalColor.RED,
        )

        # Physical state must not be changed simply because
        # GREEN was requested.
        self.assertEqual(
            north.actual_state,
            SignalColor.RED,
        )

        self.assertEqual(
            south.actual_state,
            SignalColor.RED,
        )

        self.assertEqual(
            east.actual_state,
            SignalColor.RED,
        )

        self.assertEqual(
            west.actual_state,
            SignalColor.RED,
        )

        # Only the two target directions need new physical
        # commands because EAST/WEST are already confirmed RED.
        green_commands = (
            ControllerCommand.objects
            .filter(
                junction=junction,
                requested_state=SignalColor.GREEN,
                status=CommandStatus.PENDING,
            )
        )

        self.assertEqual(
            green_commands.count(),
            2,
        )

        self.assertSetEqual(
            set(
                green_commands.values_list(
                    "direction",
                    flat=True,
                )
            ),
            {
                Direction.NORTH,
                Direction.SOUTH,
            },
        )

    # ========================================================
    # STATUS API
    # ========================================================

    def test_status_keeps_desired_and_actual_separate(self):
        junction = self.create_junction(
            junction_id="A"
        )

        north = SignalState.objects.get(
            junction=junction,
            direction=Direction.NORTH,
        )

        north.desired_state = SignalColor.GREEN
        north.actual_state = SignalColor.RED

        north.save(
            update_fields=[
                "desired_state",
                "actual_state",
                "updated_at",
            ]
        )

        response = self.client.get(
            "/api/junctions/A/status/"
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertEqual(
            response.data[
                "signals"
            ]["NORTH"]["desired"],
            "GREEN",
        )

        self.assertEqual(
            response.data[
                "signals"
            ]["NORTH"]["actual"],
            "RED",
        )

    # ========================================================
    # MANUAL CONTROL
    # ========================================================

    def test_manual_override_uses_safe_transition(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.EAST_WEST,
            controller_status=ControllerStatus.ONLINE,
        )

        self.configure_east_west_green(
            junction
        )

        response = self.client.post(
            "/api/junctions/A/commands/",
            {
                "command": (
                    "MANUAL_GREEN_REQUEST"
                ),
                "direction": "NORTH",
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertTrue(
            response.data["accepted"]
        )

        junction.refresh_from_db()

        self.assertEqual(
            junction.mode,
            JunctionMode.MANUAL,
        )

        self.assertEqual(
            junction.current_phase,
            TrafficPhase.EAST_WEST,
        )

        self.assertEqual(
            junction.target_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertEqual(
            junction.transition_stage,
            TransitionStage.YELLOW,
        )

        east = SignalState.objects.get(
            junction=junction,
            direction=Direction.EAST,
        )

        west = SignalState.objects.get(
            junction=junction,
            direction=Direction.WEST,
        )

        north = SignalState.objects.get(
            junction=junction,
            direction=Direction.NORTH,
        )

        south = SignalState.objects.get(
            junction=junction,
            direction=Direction.SOUTH,
        )

        # Existing EAST/WEST GREEN must first move
        # through YELLOW.
        self.assertEqual(
            east.desired_state,
            SignalColor.YELLOW,
        )

        self.assertEqual(
            west.desired_state,
            SignalColor.YELLOW,
        )

        # Requested NORTH/SOUTH phase must not jump
        # directly to GREEN.
        self.assertEqual(
            north.desired_state,
            SignalColor.RED,
        )

        self.assertEqual(
            south.desired_state,
            SignalColor.RED,
        )

        self.assertTrue(
            ManualOverride.objects.filter(
                junction=junction,
                requested_direction=Direction.NORTH,
                status=ManualOverrideStatus.ACTIVE,
            ).exists()
        )

    def test_return_to_automatic_releases_manual_control(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.NORTH_SOUTH,
            controller_status=ControllerStatus.ONLINE,
        )

        ManualOverride.objects.create(
            junction=junction,
            requested_direction=Direction.NORTH,
            status=ManualOverrideStatus.ACTIVE,
            requested_by="test-admin",
        )

        junction.mode = JunctionMode.MANUAL

        junction.save(
            update_fields=[
                "mode",
                "updated_at",
            ]
        )

        response = self.client.post(
            "/api/junctions/A/commands/",
            {
                "command": (
                    "RETURN_TO_AUTOMATIC"
                ),
            },
            format="json",
        )

        self.assertEqual(
            response.status_code,
            200,
        )

        self.assertTrue(
            response.data["accepted"]
        )

        junction.refresh_from_db()

        self.assertEqual(
            junction.mode,
            JunctionMode.AUTOMATIC,
        )

        self.assertFalse(
            ManualOverride.objects.filter(
                junction=junction,
                status=ManualOverrideStatus.ACTIVE,
            ).exists()
        )

    # ========================================================
    # AUTOMATIC SCHEDULER PRIORITY
    # ========================================================

    def test_scheduler_prefers_higher_vehicle_priority(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.EAST_WEST,
            controller_status=ControllerStatus.ONLINE,
        )

        now = timezone.now()

        north_entry = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="TRUCK-NORTH-001",
                direction=Direction.NORTH,
                vehicle_type=VehicleType.TRUCK,
                arrived_at=now,
                last_sequence_no=1,
            )
        )

        east_entry = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="CAR-EAST-001",
                direction=Direction.EAST,
                vehicle_type=(
                    VehicleType.EMPLOYEE_VEHICLE
                ),
                arrived_at=now,
                last_sequence_no=1,
            )
        )

        decision = TrafficScheduler.choose_phase(
            queue_entries=[
                north_entry,
                east_entry,
            ],
            current_phase=TrafficPhase.EAST_WEST,
            max_wait_seconds=120,
            now=now,
        )

        self.assertEqual(
            decision.selected_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertGreater(
            (
                decision
                .north_south
                .vehicle_priority_score
            ),
            (
                decision
                .east_west
                .vehicle_priority_score
            ),
        )

    # ========================================================
    # STARVATION PROTECTION
    # ========================================================

    def test_starvation_overrides_busier_phase(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.EAST_WEST,
            controller_status=ControllerStatus.ONLINE,
        )

        now = timezone.now()

        # One lower-priority vehicle has waited beyond
        # max_wait_seconds.
        old_north = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="OLD-NORTH-001",
                direction=Direction.NORTH,
                vehicle_type=(
                    VehicleType.EMPLOYEE_VEHICLE
                ),
                arrived_at=(
                    now
                    - timedelta(
                        seconds=180
                    )
                ),
                last_sequence_no=1,
            )
        )

        # EAST/WEST has several newer high-priority trucks.
        east_1 = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="TRUCK-EAST-001",
                direction=Direction.EAST,
                vehicle_type=VehicleType.TRUCK,
                arrived_at=(
                    now
                    - timedelta(
                        seconds=10
                    )
                ),
                last_sequence_no=1,
            )
        )

        east_2 = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="TRUCK-EAST-002",
                direction=Direction.EAST,
                vehicle_type=VehicleType.TRUCK,
                arrived_at=(
                    now
                    - timedelta(
                        seconds=10
                    )
                ),
                last_sequence_no=2,
            )
        )

        east_3 = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="TRUCK-EAST-003",
                direction=Direction.EAST,
                vehicle_type=VehicleType.TRUCK,
                arrived_at=(
                    now
                    - timedelta(
                        seconds=10
                    )
                ),
                last_sequence_no=3,
            )
        )

        decision = TrafficScheduler.choose_phase(
            queue_entries=[
                old_north,
                east_1,
                east_2,
                east_3,
            ],
            current_phase=TrafficPhase.EAST_WEST,
            max_wait_seconds=120,
            now=now,
        )

        self.assertTrue(
            decision.north_south.starved
        )

        self.assertFalse(
            decision.east_west.starved
        )

        self.assertEqual(
            decision.selected_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertIn(
            "starvation",
            decision.reason.lower(),
        )

    def test_both_starved_serves_longest_waiting_phase(self):
        junction = self.create_junction(
            junction_id="A",
            current_phase=TrafficPhase.EAST_WEST,
            controller_status=ControllerStatus.ONLINE,
        )

        now = timezone.now()

        north = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="NORTH-OLD",
                direction=Direction.NORTH,
                vehicle_type=(
                    VehicleType.EMPLOYEE_VEHICLE
                ),
                arrived_at=(
                    now
                    - timedelta(
                        seconds=240
                    )
                ),
                last_sequence_no=1,
            )
        )

        east = (
            VehicleQueueEntry.objects.create(
                junction=junction,
                vehicle_id="EAST-OLD",
                direction=Direction.EAST,
                vehicle_type=VehicleType.TRUCK,
                arrived_at=(
                    now
                    - timedelta(
                        seconds=150
                    )
                ),
                last_sequence_no=1,
            )
        )

        decision = TrafficScheduler.choose_phase(
            queue_entries=[
                north,
                east,
            ],
            current_phase=TrafficPhase.EAST_WEST,
            max_wait_seconds=120,
            now=now,
        )

        self.assertTrue(
            decision.north_south.starved
        )

        self.assertTrue(
            decision.east_west.starved
        )

        self.assertEqual(
            decision.selected_phase,
            TrafficPhase.NORTH_SOUTH,
        )

        self.assertGreater(
            (
                decision
                .north_south
                .oldest_wait_seconds
            ),
            (
                decision
                .east_west
                .oldest_wait_seconds
            ),
        )