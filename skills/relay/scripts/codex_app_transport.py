#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import codex_app_delivery_state as delivery_state
import codex_app_protocol
import codex_app_worker
import transfer_control
from codex_app_delivery_state import DeliveryConfig, LaunchResult


def launch(
    config: DeliveryConfig,
    *,
    detach: bool = True,
    acknowledgement_timeout: float = 30.0,
) -> LaunchResult:
    validated = delivery_state.validate(config)
    lock_path = validated.state_path.with_suffix(".delivery.lock")
    with delivery_state.locked(lock_path):
        try:
            existing = delivery_state.read(validated.state_path)
        except codex_app_protocol.AppServerFailure as failure:
            return _failure_result(failure)
        if existing is not None and existing.get("delivery_id") == validated.delivery_id:
            return delivery_state.result(existing, deduplicated=True)
        if existing is not None and existing.get("status") != "failed":
            return _failure_result(
                codex_app_protocol.AppServerFailure(
                    code="delivery_state_conflict",
                    detail="another delivery is still authoritative",
                )
            )
        try:
            requested = transfer_control.launch_requested(
                validated.repo,
                source_session_id=validated.source_session_id,
                transfer_id=validated.transfer_id,
                transport_key=validated.delivery_id,
            )
        except transfer_control.TransferError as failure:
            return _failure_result(failure)
        if requested.get("idempotent") is True:
            return _unknown_launch(
                validated,
                "canonical transfer already records this launch but delivery state is missing",
            )
        delivery_state.write(validated.state_path, delivery_state.build(validated))
        if not detach:
            return codex_app_worker.run(validated, None)
        return _spawn_worker(
            validated,
            acknowledgement_timeout=acknowledgement_timeout,
        )


def _spawn_worker(
    config: DeliveryConfig,
    *,
    acknowledgement_timeout: float,
) -> LaunchResult:
    request_path = config.state_path.with_suffix(
        f".{config.delivery_id}.request.json"
    )
    delivery_state.write(request_path, delivery_state.config_json(config))
    read_fd, write_fd = os.pipe()
    try:
        worker = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker-request",
                str(request_path),
                "--ack-fd",
                str(write_fd),
            ],
            cwd=config.cwd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(write_fd,),
            start_new_session=True,
        )
    except OSError as failure:
        os.close(read_fd)
        os.close(write_fd)
        return _spawn_failure(config, failure)
    os.close(write_fd)
    ready, _, _ = select.select([read_fd], [], [], acknowledgement_timeout)
    if not ready:
        os.close(read_fd)
        _stop_worker_group(worker)
        return _unknown_launch(
            config,
            "launch acknowledgement timed out; worker was stopped",
        )
    with os.fdopen(read_fd, "r", encoding="utf-8") as handle:
        line = handle.readline()
    state = delivery_state.decode(line) if line else delivery_state.read(config.state_path)
    if state is None:
        _stop_worker_group(worker)
        return _unknown_launch(
            config,
            "worker acknowledgement channel closed without a valid result",
        )
    if state is not None and state.get("status") == "failed":
        try:
            worker.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _stop_worker_group(worker)
    return delivery_state.result(
        state or delivery_state.build(config),
        deduplicated=False,
    )


def _spawn_failure(config: DeliveryConfig, failure: OSError) -> LaunchResult:
    state = delivery_state.build(config, status="failed", error=str(failure))
    delivery_state.write(config.state_path, state)
    transfer_control.record_launch_outcome(
        config.repo,
        source_session_id=config.source_session_id,
        transfer_id=config.transfer_id,
        outcome="failed",
        detail=str(failure),
    )
    return delivery_state.result(state, deduplicated=False)


def _unknown_launch(config: DeliveryConfig, detail: str) -> LaunchResult:
    state = delivery_state.build(
        config,
        status="failed",
        acknowledged=False,
        error=f"launch_outcome_unknown: {detail}",
    )
    delivery_state.write(config.state_path, state)
    try:
        transfer_control.record_launch_outcome(
            config.repo,
            source_session_id=config.source_session_id,
            transfer_id=config.transfer_id,
            outcome="unknown",
            detail=detail,
        )
    except transfer_control.TransferError as journal_failure:
        state = delivery_state.build(
            config,
            status="failed",
            acknowledged=False,
            error=(
                f"launch_outcome_unknown: {detail}; "
                f"transfer journal update failed: {journal_failure}"
            ),
        )
        delivery_state.write(config.state_path, state)
    return delivery_state.result(state, deduplicated=False)


def _stop_worker_group(worker: subprocess.Popen[bytes]) -> None:
    if worker.poll() is not None:
        return
    os.killpg(worker.pid, signal.SIGTERM)
    try:
        worker.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(worker.pid, signal.SIGKILL)
        worker.wait(timeout=5)


def _failure_result(failure: Exception) -> LaunchResult:
    return LaunchResult(
        acknowledged=False,
        deduplicated=False,
        destination_thread_id=None,
        destination_turn_id=None,
        status="failed",
        error=str(failure),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-request")
    parser.add_argument("--ack-fd", type=int)
    args = parser.parse_args()
    if not args.worker_request or args.ack_fd is None:
        return 2
    request_path = Path(args.worker_request)
    payload = delivery_state.read(request_path)
    if payload is None:
        return 2
    request_path.unlink(missing_ok=True)
    config = delivery_state.config_from_json(payload)
    with os.fdopen(args.ack_fd, "w", encoding="utf-8") as acknowledgement:
        result = codex_app_worker.run(config, acknowledgement)
    return 0 if result.acknowledged else 1


if __name__ == "__main__":
    raise SystemExit(main())
