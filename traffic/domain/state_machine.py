import uuid

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

from traffic.domain.safety import SafetyRules
from traffic.models import (
    AuditEvent,
    CommandStatus,
    CommandType,
    ControllerCommand,
    Direction,
    Junction,
    SignalColor,
    TrafficPhase,
    TransitionStage,
)


class InvalidTransitionError(Exception):
    pass


@dataclass
class TransitionResult:
    junction_id: str
    stage: str
    current_phase: str
    target_phase: Optional[str]
    due_at: object | None


class TrafficStateMachine:
    """
    Safety-oriented traffic signal state machine.

    Complete transition sequence:

        stable GREEN phase
                ↓
        YELLOW requested
                ↓
        wait for physical YELLOW confirmation
                ↓
        hold confirmed YELLOW for yellow_seconds
                ↓
        ALL_RED requested
                ↓
        wait for all four physical RED confirmations
                ↓
        hold confirmed ALL_RED for all_red_seconds
                ↓
        target GREEN requested
                ↓
        APPLYING_TARGET
                ↓
        wait for physical target GREEN confirmations
                ↓
        verify conflicting directions remain physically RED
                ↓
        STABLE

    desired_state and actual_state remain separate throughout.

    IMPORTANT SAFETY RULE:

    Logical ALL_RED alone is not proof that the physical
    intersection is actually red.

    No GREEN request may be generated from logical ALL_RED
    until NORTH, SOUTH, EAST, and WEST have all physically
    confirmed RED.
    """

    CONTROLLER_COMMAND_TIMEOUT_SECONDS = 10

    # ========================================================
    # PUBLIC API
    # ========================================================

    @staticmethod
    @transaction.atomic
    def request_phase(
        junction_id: str,
        target_phase: str,
    ) -> TransitionResult:
        junction = (
            Junction.objects
            .select_for_update()
            .get(
                junction_id=junction_id
            )
        )

        TrafficStateMachine._validate_target_phase(
            target_phase
        )

        # ----------------------------------------------------
        # Only one transition can own the junction at a time.
        # ----------------------------------------------------

        if (
            junction.transition_stage
            != TransitionStage.STABLE
        ):
            raise InvalidTransitionError(
                "A traffic transition is already in progress."
            )

        # ----------------------------------------------------
        # Already serving the requested phase.
        # ----------------------------------------------------

        if (
            junction.current_phase
            == target_phase
        ):
            return TrafficStateMachine._result(
                junction
            )

        junction.target_phase = target_phase
        junction.transition_started_at = (
            timezone.now()
        )

        # ----------------------------------------------------
        # LOGICAL ALL_RED SAFETY GATE
        # ----------------------------------------------------
        #
        # A database phase of ALL_RED is not sufficient proof
        # that the physical intersection is actually all red.
        #
        # This matters especially after:
        #
        # - process restart
        # - controller outage
        # - command timeout
        # - newly created junction
        # - stale persisted state
        #
        # Before requesting GREEN, every direction must have:
        #
        #     actual_state == RED
        #     actual_confirmed_at != None
        #
        # _signals_confirmed() enforces both requirements.
        # ----------------------------------------------------

        if (
            junction.current_phase
            == TrafficPhase.ALL_RED
        ):
            physical_all_red_confirmed = (
                TrafficStateMachine
                ._signals_confirmed(
                    junction=junction,
                    directions=(
                        Direction.NORTH,
                        Direction.SOUTH,
                        Direction.EAST,
                        Direction.WEST,
                    ),
                    required_state=SignalColor.RED,
                )
            )

            if not physical_all_red_confirmed:
                raise InvalidTransitionError(
                    "Cannot apply a GREEN phase while physical "
                    "ALL_RED has not been confirmed."
                )

            TrafficStateMachine._begin_target_application(
                junction=junction,
                phase=target_phase,
            )

            return TrafficStateMachine._result(
                junction
            )

        # ----------------------------------------------------
        # Leaving an active GREEN phase.
        #
        # Never switch directly to another GREEN phase.
        # First request YELLOW.
        # ----------------------------------------------------

        TrafficStateMachine._apply_yellow(
            junction
        )

        return TrafficStateMachine._result(
            junction
        )

    @staticmethod
    @transaction.atomic
    def advance(
        junction_id: str,
    ) -> TransitionResult:
        """
        Attempt to advance an in-progress transition.

        Safe to call repeatedly.

        The transition only advances when physical controller
        confirmation and the required clearance timing permit it.
        """

        junction = (
            Junction.objects
            .select_for_update()
            .get(
                junction_id=junction_id
            )
        )

        # ----------------------------------------------------
        # Nothing to advance.
        # ----------------------------------------------------

        if (
            junction.transition_stage
            == TransitionStage.STABLE
        ):
            return TrafficStateMachine._result(
                junction
            )

        # ====================================================
        # YELLOW
        # ====================================================

        if (
            junction.transition_stage
            == TransitionStage.YELLOW
        ):
            # Do not begin the clearance timer until the
            # previously green physical directions have
            # explicitly confirmed YELLOW.
            if not (
                TrafficStateMachine
                ._yellow_confirmation_ready(
                    junction
                )
            ):
                return TrafficStateMachine._result(
                    junction
                )

            clearance_finished = (
                TrafficStateMachine
                ._ensure_clearance_timer(
                    junction=junction,
                    required_state=(
                        SignalColor.YELLOW
                    ),
                    directions=(
                        SafetyRules.PHASE_DIRECTIONS[
                            junction.current_phase
                        ]
                    ),
                    duration_seconds=(
                        junction.yellow_seconds
                    ),
                    audit_event_type=(
                        "YELLOW_CLEARANCE_TIMER_STARTED"
                    ),
                )
            )

            if not clearance_finished:
                return TrafficStateMachine._result(
                    junction
                )

            TrafficStateMachine._apply_all_red(
                junction
            )

            return TrafficStateMachine._result(
                junction
            )

        # ====================================================
        # ALL RED
        # ====================================================

        if (
            junction.transition_stage
            == TransitionStage.ALL_RED
        ):
            if not junction.target_phase:
                raise InvalidTransitionError(
                    "Transition cannot continue because "
                    "target_phase is missing."
                )

            # Every physical signal must be confirmed RED
            # before the all-red clearance timer can start.
            if not (
                TrafficStateMachine
                ._all_red_confirmation_ready(
                    junction
                )
            ):
                return TrafficStateMachine._result(
                    junction
                )

            clearance_finished = (
                TrafficStateMachine
                ._ensure_clearance_timer(
                    junction=junction,
                    required_state=SignalColor.RED,
                    directions=(
                        Direction.NORTH,
                        Direction.SOUTH,
                        Direction.EAST,
                        Direction.WEST,
                    ),
                    duration_seconds=(
                        junction.all_red_seconds
                    ),
                    audit_event_type=(
                        "ALL_RED_CLEARANCE_TIMER_STARTED"
                    ),
                )
            )

            if not clearance_finished:
                return TrafficStateMachine._result(
                    junction
                )

            target_phase = (
                junction.target_phase
            )

            TrafficStateMachine._begin_target_application(
                junction=junction,
                phase=target_phase,
            )

            return TrafficStateMachine._result(
                junction
            )

        # ====================================================
        # APPLY TARGET GREEN
        # ====================================================

        if (
            junction.transition_stage
            == TransitionStage.APPLYING_TARGET
        ):
            if not junction.target_phase:
                raise InvalidTransitionError(
                    "APPLYING_TARGET has no target phase."
                )

            # Target directions must confirm GREEN and every
            # conflicting direction must still confirm RED.
            if not (
                TrafficStateMachine
                ._target_phase_confirmation_ready(
                    junction=junction,
                    phase=junction.target_phase,
                )
            ):
                return TrafficStateMachine._result(
                    junction
                )

            target_phase = (
                junction.target_phase
            )

            TrafficStateMachine._finalize_phase(
                junction=junction,
                phase=target_phase,
            )

            return TrafficStateMachine._result(
                junction
            )

        raise InvalidTransitionError(
            f"Unsupported transition stage: "
            f"{junction.transition_stage}"
        )

    # ========================================================
    # YELLOW
    # ========================================================

    @staticmethod
    def _apply_yellow(
        junction: Junction,
    ) -> None:
        if (
            junction.current_phase
            not in {
                TrafficPhase.NORTH_SOUTH,
                TrafficPhase.EAST_WEST,
            }
        ):
            raise InvalidTransitionError(
                f"Cannot apply YELLOW from phase "
                f"{junction.current_phase}."
            )

        states = (
            SafetyRules.yellow_states_for_phase(
                junction.current_phase
            )
        )

        TrafficStateMachine._set_desired_states(
            junction=junction,
            states=states,
        )

        junction.transition_stage = (
            TransitionStage.YELLOW
        )

        # Timer is not started yet.
        #
        # We first wait for controller-confirmed YELLOW.
        junction.transition_due_at = None

        junction.state_version += 1

        junction.save(
            update_fields=[
                "target_phase",
                "transition_stage",
                "transition_started_at",
                "transition_due_at",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type="SIGNAL_TRANSITION_YELLOW",
            message=(
                f"{junction.current_phase} YELLOW "
                f"requested; waiting for controller "
                f"confirmation."
            ),
            metadata={
                "current_phase": (
                    junction.current_phase
                ),
                "target_phase": (
                    junction.target_phase
                ),
                "yellow_seconds": (
                    junction.yellow_seconds
                ),
                "awaiting_confirmation": True,
            },
        )

    # ========================================================
    # ALL RED
    # ========================================================

    @staticmethod
    def _apply_all_red(
        junction: Junction,
    ) -> None:
        states = {
            Direction.NORTH: SignalColor.RED,
            Direction.SOUTH: SignalColor.RED,
            Direction.EAST: SignalColor.RED,
            Direction.WEST: SignalColor.RED,
        }

        SafetyRules.validate_no_conflicting_green(
            states
        )

        TrafficStateMachine._set_desired_states(
            junction=junction,
            states=states,
        )

        # Logical phase becomes ALL_RED immediately because
        # that is now the desired transition phase.
        #
        # Physical truth remains in SignalState.actual_state.
        junction.current_phase = (
            TrafficPhase.ALL_RED
        )

        junction.transition_stage = (
            TransitionStage.ALL_RED
        )

        # ALL_RED timing starts only after every physical
        # signal confirms RED.
        junction.transition_due_at = None

        junction.state_version += 1

        junction.save(
            update_fields=[
                "current_phase",
                "transition_stage",
                "transition_due_at",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type=(
                "SIGNAL_TRANSITION_ALL_RED"
            ),
            message=(
                "ALL_RED requested; waiting for "
                "physical RED confirmations."
            ),
            metadata={
                "target_phase": (
                    junction.target_phase
                ),
                "all_red_seconds": (
                    junction.all_red_seconds
                ),
                "awaiting_confirmation": True,
            },
        )

    # ========================================================
    # TARGET GREEN APPLICATION
    # ========================================================

    @staticmethod
    def _begin_target_application(
        *,
        junction: Junction,
        phase: str,
    ) -> None:
        """
        Request controller states for the target traffic phase.

        The junction remains logically ALL_RED until the target
        GREEN signals have been physically confirmed.

        transition_stage becomes APPLYING_TARGET.
        """

        TrafficStateMachine._validate_target_phase(
            phase
        )

        states = (
            SafetyRules.desired_states_for_phase(
                phase
            )
        )

        SafetyRules.validate_no_conflicting_green(
            states
        )

        TrafficStateMachine._set_desired_states(
            junction=junction,
            states=states,
        )

        junction.target_phase = phase

        junction.transition_stage = (
            TransitionStage.APPLYING_TARGET
        )

        # No timer is needed here.
        #
        # We are waiting for physical GREEN confirmation.
        junction.transition_due_at = None

        junction.state_version += 1

        junction.save(
            update_fields=[
                "target_phase",
                "transition_stage",
                "transition_started_at",
                "transition_due_at",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type="TARGET_PHASE_REQUESTED",
            message=(
                f"{phase} physical GREEN requested; "
                f"waiting for controller confirmation."
            ),
            metadata={
                "target_phase": phase,
                "awaiting_green_confirmation": True,
            },
        )

    # ========================================================
    # FINAL PHASE ACTIVATION
    # ========================================================

    @staticmethod
    def _finalize_phase(
        *,
        junction: Junction,
        phase: str,
    ) -> None:
        TrafficStateMachine._validate_target_phase(
            phase
        )

        if not (
            TrafficStateMachine
            ._target_phase_confirmation_ready(
                junction=junction,
                phase=phase,
            )
        ):
            raise InvalidTransitionError(
                f"Cannot finalize {phase}: "
                f"physical signal confirmation "
                f"is incomplete."
            )

        junction.current_phase = phase
        junction.target_phase = None

        junction.transition_stage = (
            TransitionStage.STABLE
        )

        junction.transition_due_at = None
        junction.transition_started_at = None

        junction.state_version += 1

        junction.save(
            update_fields=[
                "current_phase",
                "target_phase",
                "transition_stage",
                "transition_due_at",
                "transition_started_at",
                "state_version",
                "updated_at",
            ]
        )

        AuditEvent.objects.create(
            junction=junction,
            event_type="SIGNAL_PHASE_ACTIVE",
            message=(
                f"{phase} phase physically confirmed "
                f"and activated."
            ),
            metadata={
                "phase": phase,
                "physical_confirmation_complete": True,
            },
        )

    # ========================================================
    # PHYSICAL CONFIRMATION GATES
    # ========================================================

    @staticmethod
    def _yellow_confirmation_ready(
        junction: Junction,
    ) -> bool:
        required_directions = (
            SafetyRules.PHASE_DIRECTIONS[
                junction.current_phase
            ]
        )

        return (
            TrafficStateMachine
            ._signals_confirmed(
                junction=junction,
                directions=required_directions,
                required_state=SignalColor.YELLOW,
            )
        )

    @staticmethod
    def _all_red_confirmation_ready(
        junction: Junction,
    ) -> bool:
        return (
            TrafficStateMachine
            ._signals_confirmed(
                junction=junction,
                directions=(
                    Direction.NORTH,
                    Direction.SOUTH,
                    Direction.EAST,
                    Direction.WEST,
                ),
                required_state=SignalColor.RED,
            )
        )

    @staticmethod
    def _target_phase_confirmation_ready(
        *,
        junction: Junction,
        phase: str,
    ) -> bool:
        """
        Confirm that the desired physical target phase is real.

        NORTH_SOUTH requires:

            NORTH = GREEN
            SOUTH = GREEN
            EAST  = RED
            WEST  = RED

        EAST_WEST requires:

            EAST  = GREEN
            WEST  = GREEN
            NORTH = RED
            SOUTH = RED
        """

        TrafficStateMachine._validate_target_phase(
            phase
        )

        target_directions = (
            SafetyRules.PHASE_DIRECTIONS[
                phase
            ]
        )

        if (
            phase
            == TrafficPhase.NORTH_SOUTH
        ):
            conflicting_directions = (
                SafetyRules.PHASE_DIRECTIONS[
                    TrafficPhase.EAST_WEST
                ]
            )

        else:
            conflicting_directions = (
                SafetyRules.PHASE_DIRECTIONS[
                    TrafficPhase.NORTH_SOUTH
                ]
            )

        target_green_confirmed = (
            TrafficStateMachine
            ._signals_confirmed(
                junction=junction,
                directions=target_directions,
                required_state=SignalColor.GREEN,
            )
        )

        if not target_green_confirmed:
            return False

        conflicting_red_confirmed = (
            TrafficStateMachine
            ._signals_confirmed(
                junction=junction,
                directions=(
                    conflicting_directions
                ),
                required_state=SignalColor.RED,
            )
        )

        return conflicting_red_confirmed

    @staticmethod
    def _signals_confirmed(
        *,
        junction: Junction,
        directions,
        required_state: str,
    ) -> bool:
        """
        Verify physical controller confirmation.

        A signal only counts as confirmed when:

        1. actual_state equals required_state
        2. actual_confirmed_at is populated

        desired_state is deliberately ignored here.
        """

        signals = {
            signal.direction: signal
            for signal
            in junction.signals
            .select_for_update()
            .all()
        }

        for direction in directions:
            signal = signals.get(
                direction
            )

            if signal is None:
                raise InvalidTransitionError(
                    f"Missing SignalState for "
                    f"{junction.junction_id}/"
                    f"{direction}."
                )

            if (
                signal.actual_state
                != required_state
            ):
                return False

            if (
                signal.actual_confirmed_at
                is None
            ):
                return False

        return True

    # ========================================================
    # CLEARANCE TIMER
    # ========================================================

    @staticmethod
    def _ensure_clearance_timer(
        *,
        junction: Junction,
        required_state: str,
        directions,
        duration_seconds: int,
        audit_event_type: str,
    ) -> bool:
        """
        Start or reconstruct a safe clearance timer.

        The timer is based on the latest controller-confirmed
        timestamp among all required physical signals.

        This allows restart recovery without assuming that a
        requested state became physically active immediately.
        """

        signals = {
            signal.direction: signal
            for signal
            in junction.signals
            .select_for_update()
            .all()
        }

        confirmation_times = []

        for direction in directions:
            signal = signals.get(
                direction
            )

            if signal is None:
                raise InvalidTransitionError(
                    f"Missing SignalState for "
                    f"{junction.junction_id}/"
                    f"{direction}."
                )

            if (
                signal.actual_state
                != required_state
                or signal.actual_confirmed_at
                is None
            ):
                return False

            confirmation_times.append(
                signal.actual_confirmed_at
            )

        if not confirmation_times:
            return False

        latest_confirmation = max(
            confirmation_times
        )

        safe_due_at = (
            latest_confirmation
            + timedelta(
                seconds=duration_seconds
            )
        )

        if (
            junction.transition_due_at
            != safe_due_at
        ):
            previous_due_at = (
                junction.transition_due_at
            )

            junction.transition_due_at = (
                safe_due_at
            )

            junction.save(
                update_fields=[
                    "transition_due_at",
                    "updated_at",
                ]
            )

            # Audit only the first creation of the timer.
            #
            # A reconstructed due_at after process restart
            # should not continuously create duplicate events.
            if previous_due_at is None:
                AuditEvent.objects.create(
                    junction=junction,
                    event_type=audit_event_type,
                    message=(
                        f"{required_state} physical "
                        f"confirmation complete. "
                        f"Clearance timer started."
                    ),
                    metadata={
                        "required_state": (
                            required_state
                        ),
                        "duration_seconds": (
                            duration_seconds
                        ),
                        "latest_confirmation": (
                            latest_confirmation.isoformat()
                        ),
                        "due_at": (
                            safe_due_at.isoformat()
                        ),
                    },
                )

        return (
            timezone.now()
            >= safe_due_at
        )

    # ========================================================
    # DESIRED SIGNAL STATES + CONTROLLER COMMANDS
    # ========================================================

    @staticmethod
    def _set_desired_states(
        *,
        junction: Junction,
        states: dict,
    ) -> None:
        """
        Persist desired signal state and create physical
        controller commands where confirmation is required.

        IMPORTANT:

        This function never mutates actual_state.

        actual_state is owned only by physical controller
        acknowledgement handling.
        """

        SafetyRules.validate_no_conflicting_green(
            states
        )

        signals = {
            signal.direction: signal
            for signal
            in junction.signals
            .select_for_update()
            .all()
        }

        changed_signals = []

        for (
            direction,
            desired_state,
        ) in states.items():
            signal = signals.get(
                direction
            )

            if signal is None:
                raise InvalidTransitionError(
                    f"Missing SignalState for "
                    f"{junction.junction_id}/"
                    f"{direction}."
                )

            previous_state = (
                signal.desired_state
            )

            desired_changed = (
                previous_state
                != desired_state
            )

            physical_not_confirmed = (
                signal.actual_state
                != desired_state
                or signal.actual_confirmed_at
                is None
            )

            if desired_changed:
                signal.desired_state = (
                    desired_state
                )

                changed_signals.append(
                    signal
                )

            # No controller command is needed when desired
            # state and confirmed physical state already match.
            if not (
                desired_changed
                or physical_not_confirmed
            ):
                continue

            # Avoid creating a duplicate active command for the
            # exact same signal request.
            existing_command = (
                ControllerCommand.objects
                .filter(
                    junction=junction,
                    direction=direction,
                    requested_state=desired_state,
                    status__in=[
                        CommandStatus.PENDING,
                        CommandStatus.SENT,
                    ],
                )
                .exists()
            )

            if existing_command:
                continue

            command = (
                TrafficStateMachine
                ._create_controller_command(
                    junction=junction,
                    direction=direction,
                    requested_state=desired_state,
                )
            )

            AuditEvent.objects.create(
                junction=junction,
                event_type="SIGNAL_STATE_REQUESTED",
                direction=direction,
                command_id=(
                    command.command_id
                ),
                message=(
                    f"{direction} physical state "
                    f"requested: {desired_state}."
                ),
                metadata={
                    "previous_desired_state": (
                        previous_state
                    ),
                    "requested_state": (
                        desired_state
                    ),
                    "actual_state": (
                        signal.actual_state
                    ),
                    "command_id": (
                        command.command_id
                    ),
                    "expires_at": (
                        command.expires_at.isoformat()
                        if command.expires_at
                        else None
                    ),
                },
            )

        if changed_signals:
            junction.signals.model.objects.bulk_update(
                changed_signals,
                [
                    "desired_state",
                ],
            )

    @staticmethod
    def _create_controller_command(
        *,
        junction: Junction,
        direction: str,
        requested_state: str,
    ) -> ControllerCommand:
        command_id = (
            f"cmd-{uuid.uuid4().hex[:12]}"
        )

        now = timezone.now()

        return (
            ControllerCommand.objects.create(
                command_id=command_id,
                junction=junction,
                command_type=(
                    CommandType.SIGNAL_CHANGE
                ),
                direction=direction,
                requested_state=requested_state,
                status=CommandStatus.PENDING,
                expires_at=(
                    now
                    + timedelta(
                        seconds=(
                            TrafficStateMachine
                            .CONTROLLER_COMMAND_TIMEOUT_SECONDS
                        )
                    )
                ),
            )
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @staticmethod
    def _validate_target_phase(
        phase: str,
    ) -> None:
        if phase not in {
            TrafficPhase.NORTH_SOUTH,
            TrafficPhase.EAST_WEST,
        }:
            raise InvalidTransitionError(
                f"Invalid target phase: {phase}. "
                f"Expected NORTH_SOUTH or EAST_WEST."
            )

    @staticmethod
    def _result(
        junction: Junction,
    ) -> TransitionResult:
        return TransitionResult(
            junction_id=(
                junction.junction_id
            ),
            stage=(
                junction.transition_stage
            ),
            current_phase=(
                junction.current_phase
            ),
            target_phase=(
                junction.target_phase
            ),
            due_at=(
                junction.transition_due_at
            ),
        )