import ctypes
import signal
import threading

from ..logging import get_logger
from .common import current_millis
from .middleware import Middleware


class TimeLimitExceeded(BaseException):
    """Raised asynchronously inside worker threads when actors exceed
    their time limits.

    This is intentionally *not* a subclass of DramatiqError to avoid
    it being caught unintentionally.
    """


class TimeLimit(Middleware):
    """Middleware that cancels actors that run for too long.

    Note:
      This works by setting an async exception in the worker thread
      that runs the actor.  This means that the exception will only get
      called the next time that thread acquires the GIL.  Concretely,
      this means that this middleware can't cancel system calls.

    Parameters:
      time_limit(int): The maximum number of milliseconds actors may
        run for.
      interval(int): The interval (in milliseconds) with which to
        check for actors that have exceeded the limit.
    """

    def __init__(self, *, time_limit=600000, interval=1000):
        self.time_limit = time_limit
        self.interval = interval
        self.threads = {}
        self.logger = get_logger(__name__, type(self))

        signal.setitimer(signal.ITIMER_REAL, interval / 1000, interval / 1000)
        signal.signal(signal.SIGALRM, self._handle)

    def _handle(self, signum, mask):
        current_time = current_millis()
        for thread_id, deadline in self.threads.items():
            if current_time >= deadline:
                self.logger.warning("Time limit exceeded. Raising exception in worker thread %r.", thread_id)
                thread_id = ctypes.c_long(thread_id)
                exception = ctypes.py_object(TimeLimitExceeded)
                count = ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, exception)
                if count == 0:  # pragma: no cover
                    self.logger.critical("Failed to set exception in worker thread.")
                elif count > 1:  # pragma: no cover
                    self.logger.critical("Exception was set in multiple threads.  Undoing...")
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(thread_id, ctypes.c_long(0))

    @property
    def actor_options(self):
        return set(["time_limit"])

    def before_process_message(self, broker, message):
        actor = broker.get_actor(message.actor_name)
        deadline = current_millis() + actor.options.get("time_limit", self.time_limit)
        self.threads[threading.get_ident()] = deadline

    def after_process_message(self, broker, message, *, result=None, exception=None):
        del self.threads[threading.get_ident()]