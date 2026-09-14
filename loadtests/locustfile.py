from __future__ import annotations

import os
from pathlib import Path
import uuid

import gevent
from locust import HttpUser, LoadTestShape, between, events, task
from locust.exception import StopUser
from locust.runners import MasterRunner, WorkerRunner
import psutil
import redis

from loadtests.accounts import configured_accounts
from loadtests.coordination import RunCoordinator
from loadtests.reporting import write_locust_artifacts
from loadtests.shape_config import (
    build_shape,
    required_workers,
    validate_shape,
)
from loadtests.workflow import (
    FullWorkflowRunner,
    WorkflowFailure,
)


_CONFIGURED_RUN_ID = os.getenv("LOADTEST_RUN_ID", "").strip()
RUN_ID = (
    _CONFIGURED_RUN_ID
    or f"loadtest-{uuid.uuid4().hex[:12]}"
)
SHAPE_NAME = os.getenv(
    "LOADTEST_SHAPE",
    "smoke",
).strip().lower()
COORDINATION_URL = os.getenv(
    "LOADTEST_COORDINATION_REDIS_URL",
    "redis://localhost:7238/0",
)
ACCOUNTS_FILE = Path(
    os.getenv(
        "LOADTEST_ACCOUNTS_FILE",
        "loadtests/data/accounts.jsonl",
    )
)
_coordinator: RunCoordinator | None = None
_sampler = None


def coordinator() -> RunCoordinator:
    global _coordinator
    if _coordinator is None:
        client = redis.Redis.from_url(
            COORDINATION_URL,
            socket_connect_timeout=5,
            socket_timeout=5,
            health_check_interval=15,
        )
        client.ping()
        _coordinator = RunCoordinator(
            client,
            RUN_ID,
            lease_seconds=1800,
        )
    return _coordinator


def _sample_worker_resources() -> None:
    process = psutil.Process()
    psutil.cpu_percent(interval=None)
    while True:
        gevent.sleep(5)
        coordinator().record_worker_sample(
            cpu_percent=psutil.cpu_percent(interval=None),
            process_rss_bytes=process.memory_info().rss,
        )


@events.test_start.add_listener
def on_test_start(environment, **_kwargs) -> None:
    global _sampler
    accounts = configured_accounts(ACCOUNTS_FILE)
    if SHAPE_NAME != "smoke" and not _CONFIGURED_RUN_ID:
        raise RuntimeError(
            "distributed tests require an explicit shared LOADTEST_RUN_ID"
        )
    if len(accounts) < 480:
        raise RuntimeError(
            "400-user tests require the configured 480-account pool"
        )
    coordinator().seed_accounts(accounts)
    runner = environment.runner
    if isinstance(runner, MasterRunner):
        expected = int(
            os.getenv(
                "LOADTEST_EXPECTED_WORKERS",
                "8" if SHAPE_NAME != "smoke" else "1",
            )
        )
        if runner.worker_count < expected:
            environment.process_exit_code = 2
            runner.quit()
            raise RuntimeError(
                f"distributed run requires {expected} connected workers, "
                f"got {runner.worker_count}"
            )
        coordinator().record_worker_count(runner.worker_count)
    if not isinstance(runner, MasterRunner):
        _sampler = gevent.spawn(_sample_worker_resources)


@events.test_stop.add_listener
def on_test_stop(environment, **_kwargs) -> None:
    global _sampler
    if _sampler is not None:
        _sampler.kill(block=False)
        _sampler = None
    if isinstance(environment.runner, WorkerRunner):
        return
    failures = write_locust_artifacts(
        environment,
        coordinator(),
        run_id=RUN_ID,
    )
    if failures:
        environment.process_exit_code = 1


class FullWorkflowUser(HttpUser):
    wait_time = between(0.1, 0.5)
    network_timeout = 480.0
    connection_timeout = 20.0

    def on_start(self) -> None:
        if coordinator().is_draining():
            raise StopUser()
        self.account = coordinator().claim_account()
        self.workflow: FullWorkflowRunner | None = None
        self.terminal = True
        self.account_released = False

    def _start_workflow(self) -> None:
        coordinator().heartbeat_account(self.account)
        self.workflow = FullWorkflowRunner(
            user=self,
            account=self.account,
            coordinator=coordinator(),
            run_id=RUN_ID,
        )
        self.terminal = False
        coordinator().mark_attempted()

    def _release_account(self) -> None:
        if not getattr(self, "account_released", True):
            coordinator().release_account(self.account)
            self.account_released = True

    def context(self) -> dict[str, object]:
        account = getattr(self, "account", None)
        return {
            "run_id": RUN_ID,
            "account_index": getattr(account, "index", None),
        }

    @task
    def complete_full_workflow(self) -> None:
        if coordinator().is_draining():
            raise StopUser()
        self._start_workflow()
        failure_type = "unknown"
        pending_error: BaseException | None = None
        try:
            assert self.workflow is not None
            self.workflow.run()
            coordinator().mark_completed()
            self.terminal = True
        except WorkflowFailure as exc:
            message = str(exc).casefold()
            failure_type = (
                "isolation"
                if "isolation" in message
                or "cross-session" in message
                else "workflow"
            )
            pending_error = exc
        except Exception as exc:
            failure_type = "unexpected"
            pending_error = exc
        finally:
            assert self.workflow is not None
            self.workflow.logout_best_effort()
            if not self.terminal:
                coordinator().mark_failed(
                    failure_type,
                    decrement_active=self.workflow.marked_started,
                )
                self.terminal = True
        if pending_error is not None:
            coordinator().record_failure_sample(
                failure_type,
                account_index=self.account.index,
                message=str(pending_error),
            )
            events.request.fire(
                request_type="WORKFLOW",
                name="full workflow",
                response_time=0,
                response_length=0,
                exception=pending_error,
                context=self.context(),
            )
        if SHAPE_NAME == "smoke" or coordinator().is_draining():
            raise StopUser()

    def on_stop(self) -> None:
        workflow = getattr(self, "workflow", None)
        if not getattr(self, "terminal", True):
            if workflow is not None:
                workflow.logout_best_effort()
            coordinator().mark_failed(
                "cancelled",
                decrement_active=bool(
                    workflow and workflow.marked_started
                ),
            )
            self.terminal = True
        self._release_account()


class SystemLoadShape(LoadTestShape):
    def __init__(self) -> None:
        super().__init__()
        self.stages = build_shape(SHAPE_NAME, os.environ)
        validate_shape(self.stages)
        self.drain_started = False

    def tick(self):
        progress = coordinator().progress()
        if (
            SHAPE_NAME == "smoke"
            and int(progress.get("completed", 0))
            + int(progress.get("failed", 0))
            >= 1
        ):
            self.drain_started = True
        run_time = self.get_run_time()
        elapsed = 0
        if not self.drain_started:
            for stage in self.stages:
                elapsed += stage.duration_seconds
                if run_time < elapsed:
                    if isinstance(self.runner, MasterRunner):
                        required = required_workers(stage.users)
                        if self.runner.worker_count < required:
                            available = (
                                self.runner.worker_count * 50
                            )
                            return (
                                min(stage.users, available),
                                stage.spawn_rate,
                            )
                    return (stage.users, stage.spawn_rate)
            self.drain_started = True
        coordinator().set_draining()
        current_users = int(
            getattr(self.runner, "user_count", 0) or 0
        )
        active = max(0, int(progress.get("active", 0)))
        if current_users == 0 and active == 0:
            return None
        return (
            current_users,
            max(1.0, min(50.0, current_users)),
        )
