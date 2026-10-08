import time

from django.core.management.base import BaseCommand
from django.utils import timezone

from traffic.domain.state_machine import (
    TrafficStateMachine,
)

from traffic.models import (
    AuditEvent,
    ControllerStatus,
    Junction,
    JunctionMode,
    TrafficPhase,
    TransitionStage,
)

from traffic.services.automatic_control_service import (
    AutomaticControlError,
    AutomaticControlService,
    AutomaticControlUnavailableError,
)

from traffic.services.controller_service import (
    ControllerService,
)

from traffic.services.emergency_control_service import (
    EmergencyControlError,
    EmergencyControlService,
)


class Command(BaseCommand):
    help = (
        "Runs the factory traffic controller loop. "
        "Handles controller failures, emergency preemption, "
        "safe transitions, and automatic traffic scheduling."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help=(
                "Controller loop interval in seconds. "
                "Default: 1.0"
            ),
        )

        parser.add_argument(
            "--once",
            action="store_true",
            help=(
                "Run exactly one controller cycle "
                "and then exit."
            ),
        )

    # ========================================================
    # COMMAND ENTRY POINT
    # ========================================================

    def handle(
        self,
        *args,
        **options,
    ):
        interval = max(
            0.1,
            options["interval"],
        )

        run_once = options["once"]

        self.stdout.write(
            self.style.SUCCESS(
                "Factory traffic controller started."
            )
        )

        self.stdout.write(
            f"Loop interval: {interval} second(s)"
        )

        if run_once:
            self.stdout.write(
                "Mode: single cycle"
            )

        else:
            self.stdout.write(
                "Press CTRL+C to stop."
            )

        try:
            while True:
                self._run_cycle()

                if run_once:
                    break

                time.sleep(interval)

        except KeyboardInterrupt:
            self.stdout.write("")

            self.stdout.write(
                self.style.WARNING(
                    "Traffic controller stopped."
                )
            )

    # ========================================================
    # MAIN CONTROLLER LOOP
    # ========================================================

    def _run_cycle(self):
        """
        Run one controller cycle for every configured junction.

        A failure in one junction does not stop other junctions.
        """

        junction_ids = list(
            Junction.objects.values_list(
                "junction_id",
                flat=True,
            )
        )

        for junction_id in junction_ids:
            try:
                self._process_junction(
                    junction_id
                )

            except Exception as exc:
                self.stderr.write(
                    self.style.ERROR(
                        f"[{junction_id}] "
                        f"Controller cycle failed: "
                        f"{exc}"
                    )
                )

    # ========================================================
    # SINGLE JUNCTION PROCESSING
    # ========================================================

    def _process_junction(
        self,
        junction_id: str,
    ):
        """
        Control ownership order:

            1. command timeout / controller health
            2. controller fail-safe
            3. existing safe transition
            4. EMERGENCY
            5. MANUAL
            6. AUTOMATIC

        An emergency therefore overrides normal scheduling
        without bypassing TrafficStateMachine.

        UNKNOWN, OFFLINE, and DEGRADED controllers are frozen
        because the backend must never assume physical state.
        """

        # ====================================================
        # 1. EXPIRE PHYSICAL CONTROLLER COMMANDS
        # ====================================================

        timed_out_count = (
            ControllerService
            .expire_timed_out_commands(
                junction_id=junction_id
            )
        )

        if timed_out_count:
            self.stdout.write(
                self.style.ERROR(
                    f"[{junction_id}] "
                    f"{timed_out_count} controller "
                    f"command(s) timed out. "
                    f"Controller marked OFFLINE."
                )
            )

        # Reload because timeout processing may have changed
        # controller_status.
        junction = Junction.objects.get(
            junction_id=junction_id
        )

        # ====================================================
        # 2. FAIL SAFE
        # ====================================================

        if junction.controller_status in {
            ControllerStatus.UNKNOWN,
            ControllerStatus.OFFLINE,
            ControllerStatus.DEGRADED,
        }:
            # Never start or advance signal movement while the
            # physical controller is unavailable or its state
            # has not yet been established.
            #
            # Actual signal state remains authoritative.
            return

        # ====================================================
        # 3. COMPLETE AN EXISTING SAFE TRANSITION FIRST
        # ====================================================

        if (
            junction.transition_stage
            != TransitionStage.STABLE
        ):
            self._advance_transition(
                junction
            )

            junction.refresh_from_db()

        # ----------------------------------------------------
        # A transition may still be waiting for:
        #
        # - physical YELLOW ACK
        # - yellow clearance time
        # - physical RED ACK
        # - ALL_RED clearance
        # - target GREEN ACK
        #
        # Do not allow another owner to replace the transition
        # while any of those are unfinished.
        # ----------------------------------------------------

        if (
            junction.transition_stage
            != TransitionStage.STABLE
        ):
            return

        # ====================================================
        # 4. EMERGENCY CONTROL
        # ====================================================

        if (
            junction.mode
            == JunctionMode.EMERGENCY
        ):
            self._process_emergency(
                junction_id
            )

            return

        # ====================================================
        # 5. MANUAL CONTROL
        # ====================================================

        if (
            junction.mode
            == JunctionMode.MANUAL
        ):
            # Manual commands are initiated through
            # CommandService when the API command arrives.
            #
            # Once the requested transition is STABLE there is
            # nothing for the automatic scheduler to do while
            # MANUAL owns the junction.
            return

        # ====================================================
        # 6. ONLY AUTOMATIC MODE MAY REACH THE SCHEDULER
        # ====================================================

        if (
            junction.mode
            != JunctionMode.AUTOMATIC
        ):
            return

        # ====================================================
        # 7. NORMAL MINIMUM GREEN PERIOD
        # ====================================================

        # Emergency control intentionally does NOT pass through
        # this check.
        #
        # An emergency can preempt immediately, but still has
        # to use:
        #
        # GREEN -> YELLOW -> ALL_RED -> GREEN
        #
        # Automatic scheduling, however, should avoid switching
        # away from a recently activated normal phase.

        if (
            junction.current_phase
            != TrafficPhase.ALL_RED
            and not self._green_period_complete(
                junction
            )
        ):
            return

        # ====================================================
        # 8. AUTOMATIC SCHEDULER
        # ====================================================

        try:
            result = (
                AutomaticControlService.evaluate(
                    junction_id
                )
            )

        except AutomaticControlUnavailableError:
            return

        except AutomaticControlError as exc:
            self.stderr.write(
                self.style.ERROR(
                    f"[{junction_id}] "
                    f"Automatic scheduling failed: "
                    f"{exc}"
                )
            )

            return

        # Keep terminal output useful rather than printing
        # ordinary "hold" decisions every second.

        if (
            result.action
            == "PHASE_CHANGE_REQUESTED"
        ):
            self.stdout.write(
                self.style.WARNING(
                    f"[{junction_id}] "
                    f"PHASE_CHANGE_REQUESTED: "
                    f"{result.current_phase} -> "
                    f"{result.selected_phase} "
                    f"({result.reason})"
                )
            )

        elif (
            result.action
            == "TRANSITION_IN_PROGRESS"
        ):
            self.stdout.write(
                f"[{junction_id}] "
                f"TRANSITION_IN_PROGRESS: "
                f"{result.current_phase} -> "
                f"{result.selected_phase} "
                f"({result.reason})"
            )

    # ========================================================
    # EMERGENCY CONTROL
    # ========================================================

    def _process_emergency(
        self,
        junction_id: str,
    ):
        """
        Give the active emergency ownership of the junction.

        EmergencyControlService selects the emergency and asks
        TrafficStateMachine for the appropriate phase.

        It never directly sets signal colors.
        """

        try:
            result = (
                EmergencyControlService
                .process_junction(
                    junction_id
                )
            )

        except EmergencyControlError as exc:
            self.stderr.write(
                self.style.ERROR(
                    f"[{junction_id}] "
                    f"Emergency control failed: "
                    f"{exc}"
                )
            )

            return

        # ----------------------------------------------------
        # A new emergency transition was started.
        # ----------------------------------------------------

        if (
            result.action
            == "PREEMPTION_REQUESTED"
        ):
            self.stdout.write(
                self.style.WARNING(
                    f"[{junction_id}] "
                    f"EMERGENCY PREEMPTION: "
                    f"{result.vehicle_id} "
                    f"from {result.direction}; "
                    f"{result.current_phase} -> "
                    f"{result.target_phase}"
                )
            )

            return

        # ----------------------------------------------------
        # Emergency already owns the correct stable phase.
        #
        # Do not print this every second during the normal
        # continuous controller loop.
        # ----------------------------------------------------

        if (
            result.action
            == "HOLDING_EMERGENCY_PHASE"
        ):
            return

        # ----------------------------------------------------
        # Normally this branch is handled before reaching
        # EmergencyControlService, but it remains useful for
        # restart/race protection.
        # ----------------------------------------------------

        if (
            result.action
            == "WAITING_FOR_TRANSITION"
        ):
            return

        # ----------------------------------------------------
        # Emergency service discovered controller failure.
        # ----------------------------------------------------

        if (
            result.action
            == "BLOCKED_CONTROLLER"
        ):
            self.stderr.write(
                self.style.ERROR(
                    f"[{junction_id}] "
                    f"{result.message}"
                )
            )

            return

        # ----------------------------------------------------
        # Defensive case:
        #
        # Mode says EMERGENCY but no active EmergencyRequest
        # currently exists.
        #
        # SensorEventService should normally restore the mode
        # when the last emergency clears, so we don't mutate
        # mode here.
        # ----------------------------------------------------

        if (
            result.action
            == "NO_ACTIVE_EMERGENCY"
        ):
            self.stdout.write(
                self.style.WARNING(
                    f"[{junction_id}] "
                    f"EMERGENCY mode has no active "
                    f"emergency request."
                )
            )

    # ========================================================
    # SAFE TRANSITION ADVANCEMENT
    # ========================================================

    def _advance_transition(
        self,
        junction: Junction,
    ):
        """
        Ask TrafficStateMachine whether an existing transition
        may safely advance.

        The runner itself does not decide clearance safety.

        TrafficStateMachine checks:

        - physical YELLOW confirmations
        - YELLOW clearance timing
        - physical ALL_RED confirmations
        - ALL_RED clearance timing
        - physical target GREEN confirmations
        - conflicting physical RED states
        """

        previous_stage = (
            junction.transition_stage
        )

        previous_phase = (
            junction.current_phase
        )

        previous_due_at = (
            junction.transition_due_at
        )

        result = (
            TrafficStateMachine.advance(
                junction.junction_id
            )
        )

        # ----------------------------------------------------
        # Stage or phase changed.
        # ----------------------------------------------------

        if (
            result.stage
            != previous_stage
            or result.current_phase
            != previous_phase
        ):
            self.stdout.write(
                self.style.SUCCESS(
                    f"[{junction.junction_id}] "
                    f"{previous_stage} -> "
                    f"{result.stage}; "
                    f"{previous_phase} -> "
                    f"{result.current_phase}"
                )
            )

            return

        # ----------------------------------------------------
        # Stage remained the same but all required physical
        # ACKs may have arrived, causing the state machine to
        # start its clearance timer.
        # ----------------------------------------------------

        if (
            previous_due_at is None
            and result.due_at is not None
        ):
            self.stdout.write(
                f"[{junction.junction_id}] "
                f"{result.stage} confirmed; "
                f"clearance timer running until "
                f"{result.due_at}."
            )

    # ========================================================
    # NORMAL MINIMUM GREEN PERIOD
    # ========================================================

    def _green_period_complete(
        self,
        junction: Junction,
    ) -> bool:
        """
        Prevent AUTOMATIC mode from immediately switching away
        from a newly activated GREEN phase.

        This restriction applies to normal automatic traffic.

        EMERGENCY preemption deliberately bypasses the normal
        minimum-green scheduling policy, but NOT the signal
        safety state machine.
        """

        phase_event = (
            AuditEvent.objects
            .filter(
                junction=junction,
                event_type=(
                    "SIGNAL_PHASE_ACTIVE"
                ),
            )
            .order_by(
                "-created_at"
            )
            .first()
        )

        # Initial/test databases may not contain an activation
        # audit record yet.
        if phase_event is None:
            return True

        elapsed_seconds = (
            timezone.now()
            - phase_event.created_at
        ).total_seconds()

        return (
            elapsed_seconds
            >= junction.normal_green_seconds
        )