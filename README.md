# Factory Traffic Management System

Backend Developer Intern Assessment V2 implementation using Django, Django REST Framework, SQLite, and a plain HTML/CSS/JavaScript dashboard.

## Safety Model

The backend uses an explicit traffic state machine:

```text
GREEN
  ↓
YELLOW requested
  ↓
wait for physical YELLOW ACK
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
wait for target GREEN ACK
  ↓
verify conflicting directions remain physically RED
  ↓
STABLE
```

Key invariants:

- Desired signal state and controller-confirmed actual state are stored separately.
- Sending a command never automatically changes actual state.
- Actual state changes only after controller acknowledgement.
- Conflicting directions are never intentionally assigned GREEN simultaneously.
- Logical `ALL_RED` is not treated as proof of physical all-red.
- GREEN may begin from `ALL_RED` only after NORTH, SOUTH, EAST, and WEST all physically confirm RED.
- UNKNOWN, OFFLINE, or DEGRADED controller state blocks new automatic movement.
- Manual and emergency control use the same safety state machine.
- Traffic timing is handled by persisted timestamps and the controller runner, not long blocking sleeps inside HTTP requests.

## Modes and Priority

Modes:

- `AUTOMATIC`
- `MANUAL`
- `EMERGENCY`
- `DEGRADED`

Controller priority:

```text
controller timeout / health
        ↓
existing safe transition
        ↓
EMERGENCY
        ↓
MANUAL
        ↓
AUTOMATIC
```

Emergency control therefore overrides manual and automatic control, but does not bypass safe transitions.

## Automatic Scheduling

Vehicle priority values:

- EMERGENCY: 100
- TRUCK: 30
- FORKLIFT: 20
- EMPLOYEE_VEHICLE: 10

Scheduler behavior includes queue weighting, waiting-time contribution, a starvation bonus, and a switch threshold. A starved phase is preferred over normal scoring. If both phases are starved, the phase containing the longest-waiting vehicle is selected.

## Persistence and Concurrency

Safety-sensitive operations use database transactions and `select_for_update()` row locking. Important state is persisted, including:

- Junction configuration
- Current mode and phase
- Target phase and transition stage
- Desired signal states
- Controller-confirmed actual states
- Physical confirmation timestamps
- Queue state
- Processed sensor events
- Emergency requests
- Manual overrides
- Controller commands/events
- Audit history
- State version

Restart logic relies on persisted physical confirmation state rather than assuming previously requested physical states were executed.

## API

Base URL:

```text
http://127.0.0.1:8000
```

### Junctions

```text
GET  /api/junctions/
POST /api/junctions/
GET  /api/junctions/{junction_id}/
GET  /api/junctions/{junction_id}/status/
```

Create example:

```json
{
  "junction_id": "A",
  "name": "Junction A",
  "normal_green_seconds": 30,
  "yellow_seconds": 5,
  "all_red_seconds": 2,
  "max_wait_seconds": 120
}
```

New junctions start conservatively:

```text
mode = AUTOMATIC
phase = ALL_RED
transition_stage = STABLE
controller_status = UNKNOWN
desired signals = RED
actual signals = UNKNOWN
```

### Sensor Events

```text
POST /api/sensor-events/
```

Example:

```json
{
  "event_id": "evt-10001",
  "junction_id": "A",
  "direction": "NORTH",
  "event_type": "VEHICLE_ARRIVED",
  "vehicle_id": "VH-501",
  "vehicle_type": "TRUCK",
  "sequence_no": 1501,
  "timestamp": "2026-10-08T10:15:20Z"
}
```

Duplicate event IDs are idempotent.

### Manual Control

```text
POST /api/junctions/{junction_id}/commands/
```

Manual request:

```json
{
  "command": "MANUAL_GREEN_REQUEST",
  "direction": "WEST"
}
```

Return to automatic:

```json
{
  "command": "RETURN_TO_AUTOMATIC"
}
```

Manual command processing is transactional. If the state machine rejects the request, the attempted manual override and mode change are rolled back.

### Controller Events

```text
POST /api/controller-events/
```

Example ACK:

```json
{
  "controller_event_id": "ctrl-ack-001",
  "command_id": "cmd-replace-with-real-id",
  "junction_id": "A",
  "status": "ACK",
  "actual_state": "GREEN",
  "timestamp": "2026-10-08T10:31:00Z"
}
```

Timeouts mark the controller OFFLINE while preserving the last known actual state. Late ACK recovery is supported.

### History

```text
GET /api/junctions/{junction_id}/history/
```

Optional query parameters:

```text
limit=50
event_type=JUNCTION_CREATED
```

## Dashboard

Open:

```text
http://127.0.0.1:8000/
```

The dashboard includes:

- Junction selector and creation
- Mode, phase, controller health, state version
- Desired vs actual signal visualization
- Queue counts
- Active emergencies
- Pending commands
- Manual control
- Sensor simulator
- Audit history

## Setup

```powershell
cd C:\Users\HP\factory-traffic-management
.\venv\Scripts\Activate.ps1
python manage.py migrate
python manage.py check
```

## Run

Terminal 1:

```powershell
python manage.py runserver --noreload
```

Terminal 2:

```powershell
python manage.py run_traffic_controller
```

Single controller cycle:

```powershell
python manage.py run_traffic_controller --once
```

Because `--noreload` is used, restart Django after Python code changes.

## Tests

```powershell
python manage.py check
python manage.py test traffic -v 2
```

Current result:

```text
Found 18 test(s).
Ran 18 tests
OK
```

Coverage includes:

- safe junction creation
- duplicate junction rejection
- sensor idempotency
- emergency handling
- safe emergency preemption
- controller timeout
- late ACK recovery
- unknown-controller fail-safe
- physical ALL_RED safety gate
- desired vs actual separation
- safe manual override
- return to automatic
- priority scheduling
- starvation protection
- deterministic history ordering/filtering

## Assessment Assumptions

1. REST is used as the controller/IoT simulator; MQTT can be added later without changing domain rules.
2. Emergency requests have higher priority than manual overrides.
3. Oldest active emergency wins when multiple emergencies compete.
4. Manual control remains active until explicitly released.
5. An unfinished physical transition is not cancelled merely because manual ownership is released.
6. UNKNOWN, OFFLINE, or DEGRADED controller state is fail-safe.
7. Physical acknowledgements are authoritative for actual signal state.
8. SQLite is acceptable for this assessment; PostgreSQL would be preferable for a production high-write deployment.

## Known Scope

This is an assessment implementation, not a production traffic-control appliance. Production authentication, MQTT transport, hardware drivers, distributed workers, monitoring, and production deployment configuration are outside the current scope.
