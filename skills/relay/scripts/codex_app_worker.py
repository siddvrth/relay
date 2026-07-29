#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import IO

import codex_app_delivery_state as delivery_state
import codex_app_protocol
import transfer_control
from codex_app_delivery_state import DeliveryConfig, LaunchResult


def run(
    config: DeliveryConfig,
    acknowledgement_handle: IO[str] | None,
) -> LaunchResult:
    delivery_acknowledged = False
    acknowledged_thread_id: str | None = None

    def on_acknowledged(
        acknowledgement: codex_app_protocol.ProtocolAcknowledgement,
    ) -> None:
        nonlocal acknowledged_thread_id, delivery_acknowledged
        delivery_state.write(
            config.state_path,
            delivery_state.build(
                config,
                destination_thread_id=acknowledgement.thread_id,
                destination_turn_id=acknowledgement.turn_id,
                acknowledged=False,
            ),
        )
        transfer_control.delivered(
            config.repo,
            source_session_id=config.source_session_id,
            transfer_id=config.transfer_id,
            transport_key=config.delivery_id,
            destination_task_id=acknowledgement.thread_id,
        )
        transfer_control.started(
            config.repo,
            source_session_id=config.source_session_id,
            transfer_id=config.transfer_id,
            destination_session_id=acknowledgement.thread_id,
            destination_task_id=acknowledgement.thread_id,
        )
        delivery_acknowledged = True
        acknowledged_thread_id = acknowledgement.thread_id
        state = delivery_state.build(
            config,
            status="running",
            destination_thread_id=acknowledgement.thread_id,
            destination_turn_id=acknowledgement.turn_id,
            acknowledged=True,
        )
        delivery_state.write(config.state_path, state)
        _emit_acknowledgement(acknowledgement_handle, state)

    try:
        completion = codex_app_protocol.start_protocol(
            codex_app_protocol.ProtocolConfig(
                cwd=config.cwd,
                continuation_prompt=config.continuation_prompt,
                codex_binary=config.codex_binary,
                stderr_path=config.state_path.with_suffix(".app-server.log"),
                response_timeout=config.response_timeout,
                turn_timeout=config.turn_timeout,
            ),
            on_acknowledged=on_acknowledged,
        )
        readable = completion.destination_readable
        state = delivery_state.build(
            config,
            status="completed" if readable else "failed",
            destination_thread_id=completion.acknowledgement.thread_id,
            destination_turn_id=completion.acknowledgement.turn_id,
            destination_readable=readable,
            error=None if readable else "thread/read did not confirm destination cwd",
        )
        if not readable:
            journal_failure = _record_destination_failure(
                config,
                completion.acknowledgement.thread_id,
                str(state["error"]),
            )
            if journal_failure is not None:
                state = delivery_state.build(
                    config,
                    status="failed",
                    acknowledged=True,
                    error=(
                        f"{state['error']}; transfer journal update failed: "
                        f"{journal_failure}"
                    ),
                )
    except (
        codex_app_protocol.AppServerFailure,
        OSError,
        transfer_control.TransferError,
    ) as failure:
        state = delivery_state.build(
            config,
            status="failed",
            acknowledged=delivery_acknowledged,
            error=str(failure),
        )
        if delivery_acknowledged and acknowledged_thread_id is not None:
            journal_failure = _record_destination_failure(
                config,
                acknowledged_thread_id,
                str(failure),
            )
            if journal_failure is not None:
                state = delivery_state.build(
                    config,
                    status="failed",
                    acknowledged=True,
                    error=(
                        f"{failure}; transfer journal update failed: "
                        f"{journal_failure}"
                    ),
                )
        else:
            try:
                transfer_control.record_launch_outcome(
                    config.repo,
                    source_session_id=config.source_session_id,
                    transfer_id=config.transfer_id,
                    outcome="failed",
                    detail=str(failure),
                )
            except transfer_control.TransferError as journal_failure:
                state = delivery_state.build(
                    config,
                    status="failed",
                    acknowledged=False,
                    error=(
                        f"{failure}; transfer journal update failed: "
                        f"{journal_failure}"
                    ),
                )
        if not delivery_acknowledged:
            _emit_acknowledgement(acknowledgement_handle, state)
    delivery_state.write(config.state_path, state)
    return delivery_state.result(state, deduplicated=False)


def _record_destination_failure(
    config: DeliveryConfig,
    destination_thread_id: str,
    detail: str,
) -> str | None:
    try:
        transfer_control.destination_failed(
            config.repo,
            source_session_id=config.source_session_id,
            transfer_id=config.transfer_id,
            destination_session_id=destination_thread_id,
            destination_task_id=destination_thread_id,
            detail=detail,
        )
    except transfer_control.TransferError as failure:
        return str(failure)
    return None


def _emit_acknowledgement(
    handle: IO[str] | None,
    state: delivery_state.JsonObject,
) -> None:
    if handle is None:
        return
    handle.write(json.dumps(state, separators=(",", ":")) + "\n")
    handle.flush()
