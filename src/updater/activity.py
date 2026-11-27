"""长步骤的活动报告，不把周期心跳冒充构建完成或真实百分比。"""

from contextlib import contextmanager
from threading import Event, Thread

from .state import StateStore

ACTIVITY_INTERVAL_SECONDS = 15


@contextmanager
def report_activity(store: StateStore, job_id: str):
    stopped = Event()

    def pulse():
        while not stopped.wait(ACTIVITY_INTERVAL_SECONDS):
            store.activity(job_id)

    thread = Thread(target=pulse, name="2apis-update-activity", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join()
