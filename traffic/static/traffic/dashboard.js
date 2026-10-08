"use strict";


// ============================================================
// GLOBAL STATE
// ============================================================

const state = {
    junctions: [],
    selectedJunctionId: null,
    status: null,
    pollTimer: null,
};


const DIRECTIONS = [
    "NORTH",
    "SOUTH",
    "EAST",
    "WEST",
];


// ============================================================
// DOM HELPERS
// ============================================================

function byId(id) {
    return document.getElementById(id);
}


function escapeHtml(value) {
    return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
}


function formatDate(value) {
    if (!value) {
        return "—";
    }

    const date = new Date(value);

    if (Number.isNaN(date.getTime())) {
        return String(value);
    }

    return date.toLocaleString();
}


function makeEventId(prefix) {
    if (
        window.crypto
        && typeof window.crypto.randomUUID === "function"
    ) {
        return `${prefix}-${window.crypto.randomUUID()}`;
    }

    return (
        `${prefix}-`
        + `${Date.now()}-`
        + `${Math.random().toString(16).slice(2)}`
    );
}


// ============================================================
// API
// ============================================================

async function apiRequest(
    path,
    options = {},
) {
    const response = await fetch(
        path,
        {
            headers: {
                "Content-Type": "application/json",
                ...(options.headers || {}),
            },
            ...options,
        }
    );

    let data = null;

    try {
        data = await response.json();
    } catch {
        data = null;
    }

    if (!response.ok) {
        const message =
            data?.detail
            || `Request failed with status ${response.status}.`;

        throw new Error(message);
    }

    return data;
}


// ============================================================
// MESSAGE BAR
// ============================================================

let messageTimer = null;


function showMessage(
    message,
    type = "success",
) {
    const element = byId("messageBar");

    if (!element) {
        return;
    }

    element.textContent = message;
    element.className = `message-bar ${type}`;

    clearTimeout(messageTimer);

    messageTimer = setTimeout(
        () => {
            element.className = "message-bar hidden";
        },
        5000
    );
}


// ============================================================
// NAVIGATION
// ============================================================

function showSection(sectionName) {
    document
        .querySelectorAll(".dashboard-section")
        .forEach((section) => {
            section.classList.remove("active");
        });

    document
        .querySelectorAll(".nav-item")
        .forEach((button) => {
            button.classList.remove("active");
        });

    const section = byId(
        `${sectionName}Section`
    );

    if (section) {
        section.classList.add("active");
    }

    const navButton = document.querySelector(
        `[data-section="${sectionName}"]`
    );

    if (navButton) {
        navButton.classList.add("active");
    }

    if (sectionName === "history") {
        loadHistory();
    }
}


// ============================================================
// JUNCTION LIST
// ============================================================

async function loadJunctions(
    preferredJunctionId = null,
) {
    try {
        const junctions = await apiRequest(
            "/api/junctions/"
        );

        state.junctions = junctions;

        const select = byId("junctionSelect");

        if (!select) {
            return;
        }

        select.innerHTML = "";

        if (!junctions.length) {
            select.innerHTML = `
                <option value="">
                    No junctions
                </option>
            `;

            state.selectedJunctionId = null;

            clearDashboard();

            return;
        }

        for (const junction of junctions) {
            const option =
                document.createElement("option");

            option.value =
                junction.junction_id;

            option.textContent =
                `${junction.junction_id} — ${junction.name}`;

            select.appendChild(option);
        }

        const preferredExists =
            junctions.some(
                (junction) =>
                    junction.junction_id
                    === preferredJunctionId
            );

        const previousExists =
            junctions.some(
                (junction) =>
                    junction.junction_id
                    === state.selectedJunctionId
            );

        if (preferredExists) {
            state.selectedJunctionId =
                preferredJunctionId;

        } else if (!previousExists) {
            state.selectedJunctionId =
                junctions[0].junction_id;
        }

        select.value =
            state.selectedJunctionId;

        await refreshDashboard();

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


// ============================================================
// STATUS
// ============================================================

async function loadStatus() {
    if (!state.selectedJunctionId) {
        return null;
    }

    const junctionId =
        encodeURIComponent(
            state.selectedJunctionId
        );

    const data = await apiRequest(
        `/api/junctions/${junctionId}/status/`
    );

    state.status = data;

    renderStatus(data);

    return data;
}


function renderStatus(data) {
    byId("modeValue").textContent =
        data.mode || "—";

    byId("manualCurrentMode").textContent =
        data.mode || "—";

    byId("phaseValue").textContent =
        data.phase || "—";

    byId("intersectionPhase").textContent =
        String(
            data.phase || "UNKNOWN"
        ).replaceAll("_", " ");

    byId("controllerValue").textContent =
        data.controller_status || "—";

    byId("stateVersionValue").textContent =
        data.state_version ?? "—";


    // --------------------------------------------------------
    // MODE BADGE
    // --------------------------------------------------------

    const modeBadge =
        byId("modeBadge");

    modeBadge.textContent =
        data.mode || "—";

    modeBadge.className =
        `badge ${(data.mode || "").toLowerCase()}`;


    // --------------------------------------------------------
    // TRANSITION SUMMARY
    // --------------------------------------------------------

    let transitionText =
        data.transition_stage || "STABLE";

    if (data.target_phase) {
        transitionText +=
            ` → ${data.target_phase}`;
    }

    byId("transitionText").textContent =
        transitionText;


    // --------------------------------------------------------
    // INTERSECTION CENTER
    // --------------------------------------------------------

    renderIntersectionState(data);


    // --------------------------------------------------------
    // CONTROLLER
    // --------------------------------------------------------

    setControllerStatus(
        data.controller_status
    );


    // --------------------------------------------------------
    // SIGNALS
    // --------------------------------------------------------

    for (const direction of DIRECTIONS) {
        const signal =
            data.signals?.[direction]
            || {
                desired: "UNKNOWN",
                actual: "UNKNOWN",
            };

        renderSignal(
            direction,
            signal
        );
    }


    // --------------------------------------------------------
    // QUEUES
    // --------------------------------------------------------

    const queues =
        data.queues || {};

    const north =
        queues.NORTH || 0;

    const south =
        queues.SOUTH || 0;

    const east =
        queues.EAST || 0;

    const west =
        queues.WEST || 0;

    byId("queueNorth").textContent =
        north;

    byId("queueSouth").textContent =
        south;

    byId("queueEast").textContent =
        east;

    byId("queueWest").textContent =
        west;

    byId("queueTotal").textContent =
        north
        + south
        + east
        + west;


    // --------------------------------------------------------
    // OTHER LIVE STATE
    // --------------------------------------------------------

    renderEmergencies(
        data.active_emergencies || []
    );

    renderManualOverride(
        data.manual_override
    );

    renderPendingCommands(
        data.pending_commands || []
    );
}


// ============================================================
// INTERSECTION CENTER STATUS
// ============================================================

function renderIntersectionState(data) {
    const transitionContainer =
        byId("intersectionTransition");

    const transitionStage =
        byId("intersectionTransitionStage");

    const targetPhase =
        byId("intersectionTargetPhase");

    const controllerWarning =
        byId("intersectionControllerWarning");

    if (
        !transitionContainer
        || !transitionStage
        || !targetPhase
        || !controllerWarning
    ) {
        return;
    }


    // --------------------------------------------------------
    // TRANSITION
    // --------------------------------------------------------

    const isTransitioning =
        Boolean(
            data.transition_stage
        )
        && data.transition_stage !== "STABLE";

    if (isTransitioning) {
        transitionContainer.classList.remove(
            "hidden"
        );

        transitionStage.textContent =
            String(
                data.transition_stage
            ).replaceAll("_", " ");

        targetPhase.textContent =
            data.target_phase
                ? String(
                    data.target_phase
                ).replaceAll("_", " ")
                : "WAITING";

    } else {
        transitionContainer.classList.add(
            "hidden"
        );

        transitionStage.textContent =
            "—";

        targetPhase.textContent =
            "—";
    }


    // --------------------------------------------------------
    // CONTROLLER WARNING
    // --------------------------------------------------------

    const controllerStatus =
        String(
            data.controller_status
            || "UNKNOWN"
        ).toUpperCase();

    if (controllerStatus === "ONLINE") {
        controllerWarning.classList.add(
            "hidden"
        );

        controllerWarning.textContent =
            "";

        return;
    }

    controllerWarning.classList.remove(
        "hidden"
    );

    if (controllerStatus === "OFFLINE") {
        controllerWarning.textContent =
            "Controller offline — physical state frozen";

    } else if (
        controllerStatus === "DEGRADED"
    ) {
        controllerWarning.textContent =
            "Controller degraded — transitions paused";

    } else {
        controllerWarning.textContent =
            "Physical controller state unknown — movement blocked";
    }
}


// ============================================================
// CONTROLLER STATUS
// ============================================================

function setControllerStatus(status) {
    const normalized =
        String(
            status || "UNKNOWN"
        ).toLowerCase();

    const controllerDot =
        byId("controllerDot");

    const sidebarDot =
        byId("sidebarStatusDot");

    if (controllerDot) {
        controllerDot.className =
            `status-dot ${normalized}`;
    }

    if (sidebarDot) {
        sidebarDot.className =
            `status-dot ${normalized}`;
    }

    const sidebarStatus =
        byId("sidebarControllerStatus");

    if (sidebarStatus) {
        sidebarStatus.textContent =
            status || "UNKNOWN";
    }
}


// ============================================================
// SIGNAL RENDERING
// ============================================================

function renderSignal(
    direction,
    signal,
) {
    const container =
        document.querySelector(
            `[data-direction="${direction}"]`
        );

    if (!container) {
        return;
    }

    container
        .querySelectorAll(".lamp")
        .forEach((lamp) => {
            lamp.classList.remove(
                "active"
            );
        });

    const actual =
        String(
            signal.actual || "UNKNOWN"
        ).toUpperCase();

    if (
        [
            "RED",
            "YELLOW",
            "GREEN",
        ].includes(actual)
    ) {
        const activeLamp =
            container.querySelector(
                `[data-color="${actual}"]`
            );

        if (activeLamp) {
            activeLamp.classList.add(
                "active"
            );
        }
    }

    const actualText =
        document.querySelector(
            `[data-actual="${direction}"]`
        );

    const desiredText =
        document.querySelector(
            `[data-desired="${direction}"]`
        );

    if (actualText) {
        actualText.textContent =
            signal.actual || "UNKNOWN";
    }

    if (desiredText) {
        desiredText.textContent =
            signal.desired || "UNKNOWN";
    }
}


// ============================================================
// EMERGENCY LIST
// ============================================================

function renderEmergencies(emergencies) {
    const container =
        byId("emergencyList");

    if (!container) {
        return;
    }

    if (!emergencies.length) {
        container.className =
            "empty-state";

        container.textContent =
            "No active emergency vehicles.";

        return;
    }

    container.className =
        "data-list";

    container.innerHTML =
        emergencies
            .map(
                (emergency) => `
                    <div class="data-item">

                        <div>
                            <strong>
                                ${escapeHtml(
                                    emergency.vehicle_id
                                )}
                            </strong>

                            <span>
                                ${escapeHtml(
                                    emergency.direction
                                )}
                                · detected
                                ${escapeHtml(
                                    formatDate(
                                        emergency.detected_at
                                    )
                                )}
                            </span>
                        </div>

                        <span class="badge emergency">
                            EMERGENCY
                        </span>

                    </div>
                `
            )
            .join("");
}


// ============================================================
// MANUAL OVERRIDE
// ============================================================

function renderManualOverride(override) {
    const container =
        byId("manualOverrideDetail");

    if (!container) {
        return;
    }

    if (!override) {
        container.className =
            "empty-state";

        container.textContent =
            "No active manual override.";

        return;
    }

    container.className =
        "data-list";

    container.innerHTML = `
        <div class="data-item">

            <div>

                <strong>
                    ${escapeHtml(
                        override.requested_direction
                    )}
                    requested
                </strong>

                <span>
                    Requested
                    ${escapeHtml(
                        formatDate(
                            override.requested_at
                        )
                    )}
                </span>

                ${
                    override.requested_by
                        ? `
                            <span>
                                By:
                                ${escapeHtml(
                                    override.requested_by
                                )}
                            </span>
                        `
                        : ""
                }

            </div>

            <span class="badge manual">
                ACTIVE
            </span>

        </div>
    `;
}


// ============================================================
// PENDING CONTROLLER COMMANDS
// ============================================================

function renderPendingCommands(commands) {
    const container =
        byId("pendingCommandList");

    if (!container) {
        return;
    }

    if (!commands.length) {
        container.className =
            "empty-state";

        container.textContent =
            "No pending controller commands.";

        return;
    }

    container.className =
        "data-list";

    container.innerHTML =
        commands
            .map(
                (command) => `
                    <div class="data-item">

                        <div>

                            <strong>
                                ${escapeHtml(
                                    command.direction
                                    || "JUNCTION"
                                )}
                                →
                                ${escapeHtml(
                                    command.requested_state
                                    || command.target_phase
                                    || "COMMAND"
                                )}
                            </strong>

                            <span>
                                ${escapeHtml(
                                    command.command_id
                                )}
                            </span>

                        </div>


                        <div class="data-item-actions">

                            <span class="badge neutral">
                                ${escapeHtml(
                                    command.status
                                )}
                            </span>

                            ${
                                command.direction
                                && command.requested_state
                                    ? `
                                        <button
                                            type="button"
                                            class="mini-button"
                                            data-ack-command="${escapeHtml(
                                                command.command_id
                                            )}"
                                            data-ack-state="${escapeHtml(
                                                command.requested_state
                                            )}"
                                        >
                                            ACK
                                        </button>
                                    `
                                    : ""
                            }

                        </div>

                    </div>
                `
            )
            .join("");


    container
        .querySelectorAll(
            "[data-ack-command]"
        )
        .forEach((button) => {

            button.addEventListener(
                "click",
                async () => {
                    await acknowledgeCommand(
                        button.dataset.ackCommand,
                        button.dataset.ackState
                    );
                }
            );

        });
}


// ============================================================
// CONTROLLER ACK
// ============================================================

async function acknowledgeCommand(
    commandId,
    actualState,
) {
    if (!state.selectedJunctionId) {
        return;
    }

    try {
        await apiRequest(
            "/api/controller-events/",
            {
                method: "POST",

                body: JSON.stringify({
                    controller_event_id:
                        makeEventId(
                            "controller"
                        ),

                    command_id:
                        commandId,

                    junction_id:
                        state.selectedJunctionId,

                    status:
                        "ACK",

                    actual_state:
                        actualState,

                    timestamp:
                        new Date().toISOString(),
                }),
            }
        );

        showMessage(
            `Controller ACK accepted for ${commandId}.`
        );

        await refreshDashboard();

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


// ============================================================
// MANUAL CONTROL
// ============================================================

async function requestManualGreen(
    direction,
) {
    if (!state.selectedJunctionId) {
        showMessage(
            "Select a junction first.",
            "error"
        );

        return;
    }

    try {
        const junctionId =
            encodeURIComponent(
                state.selectedJunctionId
            );

        const response =
            await apiRequest(
                `/api/junctions/${junctionId}/commands/`,
                {
                    method: "POST",

                    body: JSON.stringify({
                        command:
                            "MANUAL_GREEN_REQUEST",

                        direction,
                    }),
                }
            );

        showMessage(
            response.message
            || `Manual ${direction} request accepted.`
        );

        await refreshDashboard();

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


async function returnToAutomatic() {
    if (!state.selectedJunctionId) {
        showMessage(
            "Select a junction first.",
            "error"
        );

        return;
    }

    try {
        const junctionId =
            encodeURIComponent(
                state.selectedJunctionId
            );

        const response =
            await apiRequest(
                `/api/junctions/${junctionId}/commands/`,
                {
                    method: "POST",

                    body: JSON.stringify({
                        command:
                            "RETURN_TO_AUTOMATIC",
                    }),
                }
            );

        showMessage(
            response.message
            || "Returned to automatic control."
        );

        await refreshDashboard();

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


// ============================================================
// SENSOR EVENT SIMULATOR
// ============================================================

async function sendSensorEvent(event) {
    event.preventDefault();

    if (!state.selectedJunctionId) {
        showMessage(
            "Select a junction first.",
            "error"
        );

        return;
    }

    const vehicleId =
        byId("vehicleId")
            .value
            .trim();

    const direction =
        byId("sensorDirection")
            .value;

    const vehicleType =
        byId("vehicleType")
            .value;

    const eventType =
        byId("sensorEventType")
            .value;

    const sequenceNumber =
        Number(
            byId("sequenceNumber")
                .value
        );

    if (!vehicleId) {
        showMessage(
            "Vehicle ID is required.",
            "error"
        );

        return;
    }

    if (
        !Number.isInteger(sequenceNumber)
        || sequenceNumber <= 0
    ) {
        showMessage(
            "Sequence number must be a positive integer.",
            "error"
        );

        return;
    }

    const payload = {
        event_id:
            makeEventId("sensor"),

        junction_id:
            state.selectedJunctionId,

        direction,

        event_type:
            eventType,

        vehicle_id:
            vehicleId,

        vehicle_type:
            vehicleType,

        sequence_no:
            sequenceNumber,

        timestamp:
            new Date().toISOString(),
    };

    try {
        const response =
            await apiRequest(
                "/api/sensor-events/",
                {
                    method: "POST",

                    body: JSON.stringify(
                        payload
                    ),
                }
            );

        showMessage(
            response.message
            || "Sensor event processed."
        );

        byId("sequenceNumber").value =
            String(
                sequenceNumber + 1
            );

        await refreshDashboard();

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


// ============================================================
// HISTORY
// ============================================================

async function loadHistory() {
    if (!state.selectedJunctionId) {
        return;
    }

    const body =
        byId("historyBody");

    if (!body) {
        return;
    }

    body.innerHTML = `
        <tr>
            <td colspan="5">
                Loading history...
            </td>
        </tr>
    `;

    try {
        const junctionId =
            encodeURIComponent(
                state.selectedJunctionId
            );

        const rawFilter =
            byId("historyFilter")
                ?.value
                .trim()
            || "";

        let path =
            `/api/junctions/${junctionId}/history/?limit=100`;

        if (rawFilter) {
            path +=
                `&event_type=${encodeURIComponent(
                    rawFilter
                )}`;
        }

        const data =
            await apiRequest(path);

        if (!data.events.length) {
            body.innerHTML = `
                <tr>
                    <td colspan="5">
                        No matching history events.
                    </td>
                </tr>
            `;

            return;
        }

        body.innerHTML =
            data.events
                .map(
                    (historyEvent) => `
                        <tr>

                            <td>
                                ${escapeHtml(
                                    formatDate(
                                        historyEvent.created_at
                                    )
                                )}
                            </td>

                            <td class="history-event">
                                ${escapeHtml(
                                    historyEvent.event_type
                                )}
                            </td>

                            <td>
                                ${escapeHtml(
                                    historyEvent.direction
                                    || "—"
                                )}
                            </td>

                            <td>
                                ${escapeHtml(
                                    historyEvent.vehicle_id
                                    || "—"
                                )}
                            </td>

                            <td>
                                ${escapeHtml(
                                    historyEvent.message
                                    || ""
                                )}
                            </td>

                        </tr>
                    `
                )
                .join("");

    } catch (error) {
        body.innerHTML = `
            <tr>
                <td colspan="5">
                    ${escapeHtml(
                        error.message
                    )}
                </td>
            </tr>
        `;
    }
}


// ============================================================
// CREATE JUNCTION MODAL
// ============================================================

function openJunctionModal() {
    const modal =
        byId("junctionModal");

    if (!modal) {
        return;
    }

    modal.classList.remove(
        "hidden"
    );

    byId("newJunctionId")
        ?.focus();
}


function closeJunctionModal() {
    const modal =
        byId("junctionModal");

    if (!modal) {
        return;
    }

    modal.classList.add(
        "hidden"
    );
}


// ============================================================
// CREATE JUNCTION
// ============================================================

async function createJunction(event) {
    event.preventDefault();

    const junctionId =
        byId("newJunctionId")
            .value
            .trim();

    const name =
        byId("newJunctionName")
            .value
            .trim();

    if (!junctionId) {
        showMessage(
            "Junction ID is required.",
            "error"
        );

        return;
    }

    const normalGreenSeconds =
        Number(
            byId("newGreenSeconds")
                .value
        );

    const yellowSeconds =
        Number(
            byId("newYellowSeconds")
                .value
        );

    const allRedSeconds =
        Number(
            byId("newAllRedSeconds")
                .value
        );

    const maxWaitSeconds =
        Number(
            byId("newMaxWaitSeconds")
                .value
        );

    const timingValues = [
        normalGreenSeconds,
        yellowSeconds,
        allRedSeconds,
        maxWaitSeconds,
    ];

    if (
        timingValues.some(
            (value) =>
                !Number.isInteger(value)
                || value <= 0
        )
    ) {
        showMessage(
            "All timing values must be positive integers.",
            "error"
        );

        return;
    }

    const payload = {
        junction_id:
            junctionId,

        name:
            name
            || `Junction ${junctionId}`,

        normal_green_seconds:
            normalGreenSeconds,

        yellow_seconds:
            yellowSeconds,

        all_red_seconds:
            allRedSeconds,

        max_wait_seconds:
            maxWaitSeconds,
    };

    try {
        await apiRequest(
            "/api/junctions/",
            {
                method: "POST",

                body: JSON.stringify(
                    payload
                ),
            }
        );

        closeJunctionModal();

        byId("junctionForm")
            .reset();

        showMessage(
            `Junction ${junctionId} created safely.`
        );

        await loadJunctions(
            junctionId
        );

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


// ============================================================
// DASHBOARD REFRESH
// ============================================================

async function refreshDashboard() {
    if (!state.selectedJunctionId) {
        return;
    }

    try {
        await loadStatus();

        const historySection =
            byId("historySection");

        const historyVisible =
            historySection
            && historySection
                .classList
                .contains("active");

        if (historyVisible) {
            await loadHistory();
        }

    } catch (error) {
        showMessage(
            error.message,
            "error"
        );
    }
}


// ============================================================
// CLEAR DASHBOARD
// ============================================================

function clearDashboard() {
    state.status = null;

    byId("modeValue").textContent =
        "—";

    byId("phaseValue").textContent =
        "—";

    byId("controllerValue").textContent =
        "—";

    byId("stateVersionValue").textContent =
        "—";

    byId("manualCurrentMode").textContent =
        "—";

    byId("intersectionPhase").textContent =
        "UNKNOWN";

    byId("transitionText").textContent =
        "Waiting for data";


    renderIntersectionState({
        transition_stage: "STABLE",
        target_phase: null,
        controller_status: "UNKNOWN",
    });


    for (const direction of DIRECTIONS) {
        renderSignal(
            direction,
            {
                desired: "UNKNOWN",
                actual: "UNKNOWN",
            }
        );
    }


    byId("queueNorth").textContent =
        "0";

    byId("queueSouth").textContent =
        "0";

    byId("queueEast").textContent =
        "0";

    byId("queueWest").textContent =
        "0";

    byId("queueTotal").textContent =
        "0";


    renderEmergencies([]);

    renderManualOverride(null);

    renderPendingCommands([]);


    setControllerStatus(
        "UNKNOWN"
    );
}


// ============================================================
// POLLING
// ============================================================

function startPolling() {
    clearInterval(
        state.pollTimer
    );

    state.pollTimer =
        setInterval(
            () => {
                refreshDashboard();
            },
            3000
        );
}


// ============================================================
// EVENT BINDINGS
// ============================================================

function bindEvents() {
    document
        .querySelectorAll(".nav-item")
        .forEach((button) => {

            button.addEventListener(
                "click",
                () => {
                    showSection(
                        button.dataset.section
                    );
                }
            );

        });


    byId("junctionSelect")
        ?.addEventListener(
            "change",
            async (event) => {

                state.selectedJunctionId =
                    event.target.value;

                await refreshDashboard();

            }
        );


    byId("refreshButton")
        ?.addEventListener(
            "click",
            refreshDashboard
        );


    byId("historyRefreshButton")
        ?.addEventListener(
            "click",
            loadHistory
        );


    byId("historyFilter")
        ?.addEventListener(
            "keydown",
            (event) => {

                if (
                    event.key === "Enter"
                ) {
                    event.preventDefault();

                    loadHistory();
                }

            }
        );


    document
        .querySelectorAll(
            "[data-manual-direction]"
        )
        .forEach((button) => {

            button.addEventListener(
                "click",
                () => {

                    requestManualGreen(
                        button.dataset
                            .manualDirection
                    );

                }
            );

        });


    byId("returnAutomaticButton")
        ?.addEventListener(
            "click",
            returnToAutomatic
        );


    byId("sensorForm")
        ?.addEventListener(
            "submit",
            sendSensorEvent
        );


    byId("createJunctionButton")
        ?.addEventListener(
            "click",
            openJunctionModal
        );


    byId("closeModalButton")
        ?.addEventListener(
            "click",
            closeJunctionModal
        );


    const modalBackdrop =
        document.querySelector(
            ".modal-backdrop"
        );

    modalBackdrop
        ?.addEventListener(
            "click",
            closeJunctionModal
        );


    byId("junctionForm")
        ?.addEventListener(
            "submit",
            createJunction
        );


    document.addEventListener(
        "keydown",
        (event) => {

            if (
                event.key === "Escape"
            ) {
                closeJunctionModal();
            }

        }
    );
}


// ============================================================
// INITIALIZE
// ============================================================

async function initialize() {
    bindEvents();

    await loadJunctions();

    startPolling();
}


document.addEventListener(
    "DOMContentLoaded",
    initialize
);