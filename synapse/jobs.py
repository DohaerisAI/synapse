from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from .models import DeliveryTarget, JobRecord, JobStatus, utc_now
from .tools.registry import ToolResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JobArtifacts:
    job_id: str
    root: Path
    request_path: Path
    progress_path: Path
    result_path: Path
    stdout_path: Path
    stderr_path: Path
    summary_path: Path


class JobService:
    def __init__(
        self,
        *,
        store,
        root: Path,
        execute_job: Callable[[JobRecord, JobArtifacts, threading.Event], Awaitable[ToolResult]],
        on_terminal: Callable[[JobRecord, ToolResult | None, JobArtifacts], None] | None = None,
        concurrency: int = 1,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self.store = store
        self.root = root
        self.execute_job = execute_job
        self.on_terminal = on_terminal
        self.concurrency = max(1, int(concurrency))
        self.poll_interval_seconds = max(0.05, float(poll_interval_seconds))
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self._threads:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            interrupted = self.store.mark_running_jobs_interrupted_on_startup()
            if interrupted:
                logger.warning("marked %d orphaned jobs as failed on startup", interrupted)
            self._stop_event.clear()
            self._threads = []
            for index in range(self.concurrency):
                worker_id = f"job-worker-{index + 1}"
                thread = threading.Thread(
                    target=self._worker_loop,
                    args=(worker_id,),
                    name=worker_id,
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def stop(self) -> None:
        with self._lock:
            if not self._threads:
                return
            self._stop_event.set()
            for event in self._cancel_events.values():
                event.set()
            threads = list(self._threads)
            self._threads = []
        for thread in threads:
            thread.join(timeout=2.0)
        with self._lock:
            self._cancel_events.clear()

    def enqueue_job(
        self,
        *,
        tool_name: str,
        params: dict[str, Any],
        parent_run_id: str | None = None,
        session_key: str | None = None,
        delivery_target: DeliveryTarget | None = None,
        approval_id: str | None = None,
    ) -> JobRecord:
        job_id = uuid4().hex
        artifacts = self.artifact_paths(job_id)
        artifacts.root.mkdir(parents=True, exist_ok=False)
        artifacts.stdout_path.touch()
        artifacts.stderr_path.touch()
        artifacts.result_path.write_text("{}\n", encoding="utf-8")
        artifacts.summary_path.write_text(f"# Job {job_id}\n\nStatus: {JobStatus.QUEUED.value}\n", encoding="utf-8")
        record = self.store.create_job(
            job_id=job_id,
            tool_name=tool_name,
            params=dict(params),
            artifact_root=str(artifacts.root),
            progress_path=str(artifacts.progress_path),
            result_path=str(artifacts.result_path),
            parent_run_id=parent_run_id,
            session_key=session_key,
            delivery_target=delivery_target,
            approval_id=approval_id,
        )
        request_payload = {
            "job_id": job_id,
            "tool_name": tool_name,
            "params": params,
            "parent_run_id": parent_run_id,
            "session_key": session_key,
            "delivery_target": None if delivery_target is None else delivery_target.model_dump(),
            "approval_id": approval_id,
            "created_at": record.created_at,
        }
        artifacts.request_path.write_text(json.dumps(request_payload, indent=2) + "\n", encoding="utf-8")
        self.append_progress(record, status=JobStatus.QUEUED, message=f"queued {tool_name}")
        return record

    def get_job(self, job_id: str) -> JobRecord | None:
        return self.store.get_job(job_id)

    def request_cancel(self, job_id: str) -> JobRecord | None:
        record = self.store.get_job(job_id)
        if record is None:
            return None
        if record.status == JobStatus.QUEUED:
            cancelled = self.store.cancel_queued_job(
                job_id,
                result_summary="Job cancelled before execution.",
                error="cancelled",
            )
            if cancelled is None:
                return self.store.get_job(job_id)
            self.append_progress(cancelled, status=JobStatus.CANCELLED, message="cancelled before execution")
            self._write_terminal_files(cancelled, None)
            self._emit_terminal(cancelled, None)
            return cancelled
        if record.status in {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}:
            updated = self.store.request_job_cancel(job_id)
            if updated is not None:
                self.append_progress(updated, status=updated.status, message="cancellation requested")
            with self._lock:
                cancel_event = self._cancel_events.get(job_id)
            if cancel_event is not None:
                cancel_event.set()
            return updated
        return record

    def artifact_paths(self, job_id: str) -> JobArtifacts:
        root = self.root / job_id
        return JobArtifacts(
            job_id=job_id,
            root=root,
            request_path=root / "request.json",
            progress_path=root / "progress.ndjson",
            result_path=root / "result.json",
            stdout_path=root / "stdout.log",
            stderr_path=root / "stderr.log",
            summary_path=root / "summary.md",
        )

    def append_progress(
        self,
        job: JobRecord,
        *,
        status: JobStatus,
        message: str,
        progress_current: int | None = None,
        progress_total: int | None = None,
    ) -> None:
        timestamp = utc_now().isoformat()
        progress_path = Path(job.progress_path or self.artifact_paths(job.job_id).progress_path)
        progress_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": timestamp,
            "job_id": job.job_id,
            "status": status.value,
            "message": message,
            "progress_current": progress_current,
            "progress_total": progress_total,
        }
        with progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
        self.store.update_job_progress(
            job.job_id,
            progress_current=progress_current,
            progress_total=progress_total,
            progress_message=message,
            status=status,
        )

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop_event.is_set():
            job = self.store.claim_next_job(worker_id)
            if job is None:
                self._stop_event.wait(self.poll_interval_seconds)
                continue
            artifacts = self.artifact_paths(job.job_id)
            cancel_event = threading.Event()
            with self._lock:
                self._cancel_events[job.job_id] = cancel_event
            try:
                self.append_progress(job, status=JobStatus.RUNNING, message=f"running {job.tool_name}")
                result = asyncio.run(self.execute_job(job, artifacts, cancel_event))
                current = self.store.get_job(job.job_id)
                if current is None:
                    continue
                if current.status == JobStatus.CANCEL_REQUESTED or cancel_event.is_set():
                    self.store.cancel_running_job(
                        job.job_id,
                        result_summary="Job cancelled during execution.",
                        error="cancelled",
                    )
                elif result.error:
                    self.store.fail_job(
                        job.job_id,
                        error=result.error,
                        result_summary=_summarize_output(result.output),
                    )
                else:
                    self.store.complete_job(
                        job.job_id,
                        result_summary=_summarize_output(result.output),
                    )
            except Exception as error:
                logger.exception("background job %s failed", job.job_id)
                self.store.fail_job(job.job_id, error=str(error), result_summary=str(error))
                result = ToolResult(output="", error=str(error))
            finally:
                with self._lock:
                    self._cancel_events.pop(job.job_id, None)
            terminal = self.store.get_job(job.job_id)
            if terminal is None:
                continue
            terminal_status = terminal.status
            if terminal_status == JobStatus.CANCELLED:
                self.append_progress(terminal, status=JobStatus.CANCELLED, message="job cancelled")
            elif terminal_status == JobStatus.FAILED:
                self.append_progress(terminal, status=JobStatus.FAILED, message=terminal.error or "job failed")
            else:
                self.append_progress(terminal, status=JobStatus.SUCCEEDED, message=terminal.result_summary or "job completed")
            self._write_terminal_files(terminal, result)
            self._emit_terminal(terminal, result)

    def _write_terminal_files(self, job: JobRecord, result: ToolResult | None) -> None:
        artifacts = self.artifact_paths(job.job_id)
        payload = {
            "job_id": job.job_id,
            "tool_name": job.tool_name,
            "status": job.status.value,
            "output": "" if result is None else result.output,
            "error": job.error,
            "artifacts": None if result is None else result.artifacts,
            "finished_at": job.finished_at,
        }
        artifacts.result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        lines = [
            f"# Job {job.job_id}",
            "",
            f"Tool: {job.tool_name}",
            f"Status: {job.status.value}",
        ]
        if job.result_summary:
            lines.extend(["", job.result_summary])
        if job.error:
            lines.extend(["", f"Error: {job.error}"])
        artifacts.summary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _emit_terminal(self, job: JobRecord, result: ToolResult | None) -> None:
        if self.on_terminal is None:
            return
        try:
            self.on_terminal(job, result, self.artifact_paths(job.job_id))
        except Exception:
            logger.exception("job terminal callback failed for %s", job.job_id)


def _summarize_output(output: str) -> str:
    text = output.strip()
    if not text:
        return ""
    lines = text.splitlines()
    summary = lines[0].strip()
    if len(summary) > 200:
        summary = summary[:197].rstrip() + "..."
    return summary
