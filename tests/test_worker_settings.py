"""Ensure arq WorkerSettings wires import + S3 cleanup."""

from app.workers.import_csv import import_csv
from app.workers.s3_cleanup import process_s3_deletions
from app.workers.settings import WorkerSettings


def test_worker_settings_registers_phase_8_and_9_jobs():
    assert import_csv in WorkerSettings.functions
    assert process_s3_deletions in WorkerSettings.functions
    cron_fns = {job.coroutine for job in WorkerSettings.cron_jobs}
    assert process_s3_deletions in cron_fns
