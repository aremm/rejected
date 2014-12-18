"""
Common Mixin Classes

"""
import logging
import time

LOGGER = logging.getLogger(__name__)


try:
    from logging import NullHandler
except ImportError:
    # Python 2.6 does not have a NullHandler
    class NullHandler(logging.Handler):
        def emit(self, record):
            pass


def add_null_handler():
    logger = logging.getLogger()
    logger.addHandler(NullHandler())


class State(object):
    """Class that is to be extended by MCP and process for maintaining the
    internal state of the application.

    """
    # State constants
    STATE_INITIALIZING = 0x01
    STATE_CONNECTING = 0x02
    STATE_IDLE = 0x03
    STATE_ACTIVE = 0x04
    STATE_SLEEPING = 0x05
    STATE_STOP_REQUESTED = 0x06
    STATE_SHUTTING_DOWN = 0x07
    STATE_STOPPED = 0x08

    # For reverse lookup
    _STATES = {0x01: 'Initializing',
               0x02: 'Connecting',
               0x03: 'Idle',
               0x04: 'Active',
               0x05: 'Sleeping',
               0x06: 'Stop Requested',
               0x07: 'Shutting down',
               0x08: 'Stopped'}

    def __init__(self):
        """Initialize the state of the object"""
        self._state = self.STATE_INITIALIZING
        self._state_start = time.time()

    def set_state(self, new_state):
        """Assign the specified state to this consumer object.

        :param int new_state: The new state of the object
        :raises: ValueError

        """
        # Make sure it's a valid state
        if new_state not in self._STATES:
            raise ValueError('Invalid state value: %r' % new_state)

        # Set the state
        LOGGER.debug('State changing from %s to %s',
                     self._STATES[self._state], self._STATES[new_state])
        self._state = new_state
        self._state_start = time.time()

    @property
    def is_connecting(self):
        """Returns a bool specifying if the process is currently connecting.

        :rtype: bool

        """
        return self._state == self.STATE_CONNECTING

    @property
    def is_idle(self):
        """Returns a bool specifying if the process is currently idle.

        :rtype: bool

        """
        return self._state == self.STATE_IDLE

    @property
    def is_running(self):
        """Returns a bool determining if the process is in a running state or
        not

        :rtype: bool

        """
        return self._state in [self.STATE_IDLE, self.STATE_ACTIVE,
                               self.STATE_SLEEPING]

    @property
    def is_shutting_down(self):
        """Designates if the process is shutting down.

        :rtype: bool

        """
        return self._state == self.STATE_SHUTTING_DOWN


    @property
    def is_sleeping(self):
        """Returns a bool determining if the process is sleeping

        :rtype: bool

        """
        return self._state == self.STATE_SLEEPING

    @property
    def is_stopped(self):
        """Returns a bool determining if the process is stopped or stopping

        :rtype: bool

        """
        return self._state == self.STATE_STOPPED

    @property
    def is_waiting_to_shutdown(self):
        """Designates if the process is waiting to start shutdown

        :rtype: bool

        """
        return self._state == self.STATE_STOP_REQUESTED

    @property
    def state_description(self):
        """Return the string description of our running state.

        :rtype: str

        """
        return self._STATES[self._state]