"""
The :py:class:`Consumer`, and :py:class:`SmartConsumer` provide base
classes to extend for consumer applications.

While the :py:class:`Consumer` class provides all the structure required for
implementing a rejected consumer, the :py:class:`SmartConsumer` adds
functionality designed to make writing consumers even easier. When messages
are received by consumers extending :py:class:`SmartConsumer`, if the message's
``content_type`` property contains one of the supported mime-types, the message
body will automatically be deserialized, making the deserialized message body
available via the ``body`` attribute. Additionally, should one of the supported
``content_encoding`` types (``gzip`` or ``bzip2``) be specified in the
message's property, it will automatically be decoded.

Supported `SmartConsumer` MIME types are:

 - application/json
 - application/msgpack (with u-msgpack-python installed)
 - application/pickle
 - application/x-pickle
 - application/x-plist
 - application/x-vnd.python.pickle
 - application/vnd.python.pickle
 - text/csv
 - text/html (with beautifulsoup4 installed)
 - text/xml (with beautifulsoup4 installed)
 - text/yaml
 - text/x-yaml

"""
import contextlib
import datetime
import flatdict
import importlib
import io
import logging
import pkg_resources
import sys
import time
import uuid

from ietfparse import headers
import pika
from tornado import concurrent, gen, locks

from rejected import data, errors, log, utils

# Python3 Support
try:
    unicode()
except NameError:  # pragma: nocover
    unicode = str

_DEFAULT_CHANNEL = 'default'
_DROPPED_MESSAGE = 'X-Rejected-Dropped'
_PROCESSING_EXCEPTIONS = 'X-Processing-Exceptions'
_EXCEPTION_FROM = 'X-Exception-From'
_PYTHON3 = True if sys.version_info > (3, 0, 0) else False


class Consumer(object):
    """Base consumer class that defines the contract between rejected and
    consumer applications. You must extend the
    :meth:`~rejected.consumer.Consumer.process` method in your child class
    to properly implement a consumer application. All other methods are
    optional.

    """
    MESSAGE_TYPE = None
    """A value or list of values that are compared against the AMQP `type`
    message property to determine if the consumer can support the
    processing of the received message. If not specified, type checking is
    not performed. Use in conjunction with
    :const:`~rejected.consumer.Consumer.DROP_INVALID_MESSAGES`.

    :default: :class:`None`
    :type: str or list(str)
    """

    DROP_INVALID_MESSAGES = False
    """Set to :class:`True` to automatically drop messages that do not match a
    supported message type as defined in
    :const:`~rejected.consumer.Consumer.MESSAGE_TYPE`.

    :default: :class:`False`
    :type: bool
    """

    DROP_EXCHANGE = None
    """Assign an exchange to publish dropped messages to. If set to
    :class:`None`, dropped messages are not republished.

    :default: :class:`None`
    :type: :class:`str` or :class:`None`
    """

    ERROR_MAX_RETRIES = None
    """Assign an integer value to limit the number of times a
    :exc:`~rejected.consumer.ProcessingException` is raised for a message
    before it is dropped. `None` disables the dropping of messages due to
    :exc:`~rejected.consumer.ProcessingException`.
    Replaces the deprecated :const:`ERROR_MAX_RETRY` attribute.

    .. versionadded:: 4.0.0

    :default: :class:`None`
    :type: int or None
    """

    ERROR_MAX_RETRY = None
    """Assign an integer value to limit the number of times a
    :exc:`~rejected.consumer.ProcessingException` is raised for a message
    before it is dropped. `None` disables the dropping of messages due to
    :exc:`~rejected.consumer.ProcessingException`.
    Deprecated by the :const:`ERROR_MAX_RETRIES` attribute.

    .. deprecated:: 4.0.0

    :default: :class:`None`
    :type: int or None
    """

    ERROR_EXCHANGE = 'errors'
    """The exchange a message will be published to if a
    :exc:`~rejected.consumer.ProcessingException` is raised.

    :default: :const:`errors`
    :type: str
    """

    IGNORE_OOB_STATS = False
    """Suppress warnings when calls to the stats methods are made while no
    message is currently being processed.

    .. versionadded:: 4.0.0

    :default: :class:`False`
    :type: bool
    """

    MESSAGE_AGE_KEY = 'message_age'
    """Specify a value that is used for the automatic recording of per-message
    statistics for the message age. You can also override the
    :meth:`~rejected.consumer.Consumer.message_age_key` method to create
    compound keys. For example, to create a key that includes the message
    priority:

    .. code-block:: python

        class Consumer(consumer.Consumer):

            def message_age_key(self):
                return 'priority-{}.message_age'.format(self.priority or 0)

    .. versionadded:: 3.18.6

    :default: :const:`message_age`
    :type: str
    """

    def __init__(self, *args, **kwargs):
        """Creates a new instance of the :class:`~rejected.consumer.Consumer`
        class. To perform initialization tasks, extend
        :meth:`~rejected.consumer.Consumer.initialize`.

        """
        self._confirmation_futures = {}
        self._connections = {}
        self._correlation_id = None
        self._drop_exchange = kwargs.get('drop_exchange') or self.DROP_EXCHANGE
        self._drop_invalid = (kwargs.get('drop_invalid_messages')
                              or self.DROP_INVALID_MESSAGES)
        self._error_exchange = (kwargs.get('error_exchange')
                                or self.ERROR_EXCHANGE)
        self._error_max_retry = (kwargs.get('error_max_retry')
                                 or self.ERROR_MAX_RETRIES
                                 or self.ERROR_MAX_RETRY)
        self._finished = False
        self._message = None
        self._message_type = kwargs.get('message_type') or self.MESSAGE_TYPE
        self._measurement = None
        self._message_body = None
        self._process = kwargs['process']
        self._settings = kwargs['settings']
        self._yield_condition = locks.Condition()

        # Create a logger that attaches correlation ID to the record
        logger = logging.getLogger(
            kwargs.get('settings').get('_import_module', __name__))
        self.logger = log.CorrelationIDAdapter(logger, {'parent': self})

        # Set a Sentry context for the consumer
        self.set_sentry_context('consumer', self.name)

        # Run any child object specified initialization
        self.initialize()

    def initialize(self):
        """Extend this method for any initialization tasks that occur only when
        the :class:`~rejected.consumer.Consumer` class is created.

        """
        pass

    def prepare(self):
        """Called when a message is received before
        :meth:`~rejected.consumer.Consumer.process`. One use for extending
        this method is to pre-process messages and isolate logic that
        validates messages and rejected them if data is missing.

        .. note:: Asynchronous support: Decorate this method with
            :func:`tornado.gen.coroutine` to make it asynchronous.

        If this method returns a :class:`~tornado.concurrent.Future`, execution
        will not proceed until the Future has completed.

        :raises: :exc:`rejected.consumer.ConsumerException`
        :raises: :exc:`rejected.consumer.MessageException`
        :raises: :exc:`rejected.consumer.ProcessingException`

        """
        pass

    def process(self):
        """Implement this method for the primary, top-level message processing
        logic for your consumer.

        If the message can not be processed and the Consumer should stop after
        ``N`` failures to process messages, raise the
        :exc:`~rejected.consumer.ConsumerException`.

        .. note:: Asynchronous support: Decorate this method with
            :func:`tornado.gen.coroutine` to make it asynchronous.

        :raises: :exc:`rejected.consumer.ConsumerException`
        :raises: :exc:`rejected.consumer.MessageException`
        :raises: :exc:`rejected.consumer.ProcessingException`

        """
        raise NotImplementedError

    def message_age_key(self):
        """Return the key part that is used in submitting message age stats.
        Override this method to change the key part. This could be used to
        include message priority in the key, for example.

        .. versionadded:: 3.18.6

        :rtype: str

        """
        return self.MESSAGE_AGE_KEY

    def on_finish(self):
        """Called after a message has been processed.

        Override this method to perform cleanup, logging, etc.
        This method is a counterpart to
        :meth:`~rejected.consumer.Consumer.prepare`.  ``on_finish`` may
        not produce any output, as it is called after all processing has
        taken place.

        If any exception is raised during the processing of a message,
        :meth:`~rejected.consumer.Consumer.on_finish` is not called.

        .. note:: Asynchronous support: Decorate this method with
            :func:`tornado.gen.coroutine` to make it asynchronous.

        """
        pass

    def on_blocked(self, name):  # pragma: nocover
        """Called when a connection for this consumer is blocked.

        Implement this method to respond to being blocked.

        .. versionadded:: 3.17

        :param str name: The connection name that is blocked

        """
        pass

    def on_unblocked(self, name):  # pragma: nocover
        """Called when a connection for this consumer is unblocked.

        Implement this method to respond to being blocked.

        .. versionadded:: 3.17

        :param str name: The connection name that is blocked

        """
        pass

    def shutdown(self):  # pragma: nocover
        """Implement to cleanly shutdown your application code when rejected is
        stopping the consumer.

        This could be used for closing database connections or other such
        activities.

        """
        pass

    """Quick-access properties"""

    @utils.MessageProperty
    def app_id(self):
        """Access the current message's ``app-id`` property as an attribute of
        the consumer class.

        :rtype: str

        """
        return self._message.properties.app_id

    @utils.MessageProperty
    def body(self):
        """Access the opaque body from the current message.

        :rtype: str

        """
        return self._message.body

    @utils.MessageProperty
    def content_encoding(self):
        """Access the current message's ``content-encoding`` AMQP message
        property as an attribute of the consumer class.

        :rtype: str

        """
        return (self._message.properties.content_encoding
                or '').lower() or None

    @utils.MessageProperty
    def content_type(self):
        """Access the current message's ``content-type`` AMQP message property
        as an attribute of the consumer class.

        :rtype: str

        """
        return (self._message.properties.content_type or '').lower() or None

    @utils.MessageProperty
    def correlation_id(self):
        """Access the current message's ``correlation-id`` AMAP message
        property as an attribute of the consumer class. If the message does not
        have a ``correlation-id`` then, each message is assigned a new UUIDv4
        based ``correlation-id`` value.

        :rtype: str

        """
        return self._correlation_id

    @utils.MessageProperty
    def exchange(self):
        """Access the AMQP exchange the message was published to as an
        attribute of the consumer class.

        :rtype: str

        """
        return self._message.exchange

    @utils.MessageProperty
    def expiration(self):
        """Access the current message's ``expiration`` AMQP message property as
        an attribute of the consumer class.

        :rtype: str

        """
        return self._message.properties.expiration

    @utils.MessageProperty
    def headers(self):
        """Access the current message's ``headers`` AMQP message property as an
        attribute of the consumer class.

        :rtype: dict

        """
        return self._message.properties.headers or dict()

    @property
    def is_finished(self):
        """Returns a boolean indicating if the consumer has finished processing
        the current message.

        .. versionadded:: 4.0.0

        :rtype: bool

        """
        return self._finished

    @utils.MessageProperty
    def io_loop(self):
        """Access the :py:class:`tornado.ioloop.IOLoop` instance for the
        current message.

        .. versionadded:: 3.18.4

        :rtype: tornado.ioloop.IOLoop

        """
        return self._connections[self._message.connection].io_loop

    @utils.MessageProperty
    def message_id(self):
        """Access the current message's ``message-id`` AMQP message property as
        an attribute of the consumer class.

        :rtype: str

        """
        return self._message.properties.message_id

    @property
    def measurement(self):
        """Access the current message's :class:`rejected.data.Measurement`
        instance.

        .. versionadded:: 4.0.0

        :rtype: rejected.data.Measurement

        """
        return self._measurement

    @property
    def name(self):
        """Property returning the name of the consumer class.

        :rtype: str

        """
        return self.__class__.__name__

    @utils.MessageProperty
    def priority(self):
        """Access the current message's ``priority`` AMQP message property as
        an attribute of the consumer class.

        :rtype: int

        """
        return self._message.properties.priority

    @utils.MessageProperty
    def properties(self):
        """Access the current message's AMQP message properties in dict form as
        an attribute of the consumer class.

        :rtype: dict

        """
        return dict(self._message.properties)

    @utils.MessageProperty
    def redelivered(self):
        """Indicates if the current message has been redelivered.

        :rtype: bool

        """
        return self._message.redelivered

    @utils.MessageProperty
    def reply_to(self):
        """Access the current message's ``reply-to`` AMQP message property as
        an attribute of the consumer class.

        :rtype: str

        """
        return self._message.properties.reply_to

    @utils.MessageProperty
    def routing_key(self):
        """Access the routing key for the current message.

        :rtype: str

        """
        return self._message.routing_key

    @utils.MessageProperty
    def message_type(self):
        """Access the current message's ``type`` AMQP message property as an
        attribute of the consumer class.

        :rtype: str

        """
        return self._message.properties.type

    @property
    def sentry_client(self):
        """Access the Sentry raven ``Client`` instance or ``None``

        Use this object to add tags or additional context to Sentry
        error reports (see :meth:`raven.base.Client.tags_context`) or
        to report messages (via :meth:`raven.base.Client.captureMessage`)
        directly to Sentry.

        :rtype: :class:`raven.base.Client`

        """
        if hasattr(self._process, 'sentry_client'):
            return self._process.sentry_client

    @property
    def settings(self):
        """Access the consumer settings as specified by the ``config`` section
        for the consumer in the rejected configuration.

        :rtype: dict

        """
        return self._settings

    @utils.MessageProperty
    def timestamp(self):
        """Access the unix epoch timestamp value from the AMQP message
        properties of the current message.

        :rtype: int

        """
        return self._message.properties.timestamp

    @utils.MessageProperty
    def user_id(self):
        """Access the ``user-id`` AMQP message property from the current
        message's properties.

        :rtype: str

        """
        return self._message.properties.user_id

    """Utility Methods for use by Consumer Code"""

    def require_setting(self, name, feature='this feature'):
        """Raises an exception if the given app setting is not defined.

        As a generalization, this method should called from a Consumer's
        :py:meth:`~rejected.consumer.Consumer.initialize` method. If a required
        setting is not found, this method will cause the
        consumer to shutdown prior to receiving any messages from RabbitMQ.

        :param str name: The parameter name
        :param str feature: A friendly name for the setting feature
        :raises: rejected.consumer.ConfigurationException

        """
        if name not in self.settings:
            raise ConfigurationException(
                "You must define the '{}' setting in your "
                "application to use {}".format(name, feature))

    def finish(self):
        """Finishes message processing for the current message. If this is
        called in :meth:`~rejected.consumer.Consumer.prepare`, the
        :meth:`~rejected.consumer.Consumer.process` method is not invoked
        for the current message.

        """
        if self._finished:
            self.logger.warning('Finished called when already finished')
            return
        self._finished = True
        self.on_finish()

    def publish_message(self,
                        exchange,
                        routing_key,
                        properties,
                        body,
                        channel=None,
                        connection=None):
        """Publish a message to RabbitMQ on the same channel the original
        message was received on. If
        `publisher confirmations <https://www.rabbitmq.com/confirms.html>`_
        are enabled, the method will return a
        :class:`~tornado.concurrent.Future` that will resolve a :type:`bool`
        that indicates if the publishing was successful.

        .. versionchanged:: 4.0.0
           - Return a :class:`~tornado.concurrent.Future` if publisher
                confirmations are enabled
           - Deprecated ``channel``

        :param str exchange: The exchange to publish to
        :param str routing_key: The routing key to publish with
        :param dict properties: The message properties
        :param str body: The message body
        :param str channel: **Deprecated in 4.0.0** Specify the connection
            parameter instead.
        :param str connection: The connection to use. If it is not
            specified, the channel that the message was delivered on is used.
        :rtype: tornado.concurrent.Future or None

        """
        conn = self._publish_connection(channel or connection)
        self.logger.debug('Publishing message to %s:%s (%s)', exchange,
                          routing_key, conn.name)
        basic_properties = self._get_pika_properties(properties)
        with self._measurement.track_duration('publish.{}.{}'.format(
                exchange, routing_key)):
            conn.channel.basic_publish(
                exchange=exchange,
                routing_key=routing_key,
                properties=basic_properties,
                body=body,
                mandatory=conn.publisher_confirmations)
            return self._publisher_confirmation_future(
                conn.name, exchange, routing_key, basic_properties)

    def rpc_reply(self,
                  body,
                  properties=None,
                  exchange=None,
                  reply_to=None,
                  connection=None):
        """Reply to the message that is currently being processed.

        If the exchange is not specified, the exchange of the message that is
        currently being processed by the Consumer will be used.

        If ``reply_to`` is not provided, it will attempt to use the
        ``reply_to`` property of the message that is currently being processed
        by the consumer. If both are not set, a :exc:`ValueError` is raised.

        If any of the following message properties are not provided, they will
        automatically be assigned:

          - ``app_id``
          - ``correlation_id``
          - ``message_id``
          - ``timestamp``

        The ``correlation_id`` will only be automatically assigned if the
        original message provided a ``message_id``.

        If the connection that the message is being published on has publisher
        confirmations enabled, a :py:class:`~tornado.concurrent.Future` is
        returned.

        :param bytes body: The message body
        :param properties: The AMQP properties to use for the reply message
        :type properties: dict or None
        :param exchange: The exchange to publish to. Defaults to the exchange
            of the message that is currently being processed by the Consumer.
        :type exchange: str or None
        :param reply_to: The routing key to send the reply to. Defaults to the
            reply_to property of the message that is currently being processed
            by the Consumer. If neither are set, a :exc:`ValueError` is
            raised.
        :type reply_to: str or None
        :param str connection: The connection to use. If it is not
            specified, the channel that the message was delivered on is used.
        :rtype: tornado.concurrent.Future or None
        :raises: ValueError

        .. versionadded:: 4.0.0

        """
        if reply_to is None and self.reply_to is None:
            raise ValueError('Missing reply_to routing key')
        properties = properties or {}
        if not properties.get('app_id'):
            properties['app_id'] = self.name
        if not properties.get('correlation_id') and self.message_id:
            properties['correlation_id'] = self.message_id
        if not properties.get('message_id'):
            properties['message_id'] = str(uuid.uuid4())
        if not properties.get('timestamp'):
            properties['timestamp'] = int(time.time())
        return self.publish_message(exchange or self.exchange, reply_to
                                    or self.reply_to, properties, body,
                                    connection)

    def send_exception_to_sentry(self, exc_info):
        """Send an exception to Sentry if enabled.

        :param tuple exc_info: exception information as returned from
            :func:`sys.exc_info`

        """
        self._process.send_exception_to_sentry(exc_info)

    def set_sentry_context(self, tag, value):
        """Set a context tag in Sentry for the given key and value.

        :param str tag: The context tag name
        :param str value: The context value

        """
        if self.sentry_client:
            self.logger.debug('Setting sentry context for %s to %s', tag,
                              value)
            self.sentry_client.tags_context({tag: value})

    def stats_add_duration(self, key, duration):
        """Add a duration to the per-message measurements

        .. note:: If this method is called when there is not a message being
            processed, a message will be logged at the ``warning`` level to
            indicate the value is being dropped. To suppress these warnings,
            set the :attr:`rejected.consumer.Consumer.IGNORE_OOB_STATS`
            attribute to `True`.

        .. versionadded:: 3.19.0

        :param str key: The key to add the timing to
        :param int|float duration: The timing value in seconds

        """
        if not self._measurement:
            if not self.IGNORE_OOB_STATS:
                self.logger.warning(
                    'stats_add_timing invoked outside execution')
            return
        self._measurement.add_duration(key, duration)

    def stats_incr(self, key, value=1):
        """Increment the specified key in the per-message measurements


        .. note:: If this method is called when there is not a message being
            processed, a message will be logged at the ``warning`` level to
            indicate the value is being dropped. To suppress these warnings,
            set the :attr:`rejected.consumer.Consumer.IGNORE_OOB_STATS`
            attribute to `True`.

        .. versionadded:: 3.13.0

        :param str key: The key to increment
        :param int value: The value to increment the key by

        """
        if not self._measurement:
            if not self.IGNORE_OOB_STATS:
                self.logger.warning('stats_incr invoked outside execution')
            return
        self._measurement.incr(key, value)

    def stats_set_tag(self, key, value=1):
        """Set the specified tag/value in the per-message measurements

        .. note:: If this method is called when there is not a message being
            processed, a message will be logged at the ``warning`` level to
            indicate the value is being dropped. To suppress these warnings,
            set the :attr:`rejected.consumer.Consumer.IGNORE_OOB_STATS`
            attribute to `True`.

        .. versionadded:: 3.13.0

        :param str key: The key to increment
        :param int value: The value to increment the key by

        """
        if not self._measurement:
            if not self.IGNORE_OOB_STATS:
                self.logger.warning('stats_set_tag invoked outside execution')
            return
        self._measurement.set_tag(key, value)

    def stats_set_value(self, key, value=1):
        """Set the specified key/value in the per-message measurements

        .. note:: If this method is called when there is not a message being
            processed, a message will be logged at the ``warning`` level to
            indicate the value is being dropped. To suppress these warnings,
            set the :attr:`rejected.consumer.Consumer.IGNORE_OOB_STATS`
            attribute to `True`.

        .. versionadded:: 3.13.0

        :param str key: The key to increment
        :param int value: The value to increment the key by

        """
        if not self._measurement:
            if not self.IGNORE_OOB_STATS:
                self.logger.warning(
                    'stats_set_value invoked outside execution')
            return
        self._measurement.set_value(key, value)

    @contextlib.contextmanager
    def stats_track_duration(self, key):
        """Time around a context and add to the the per-message measurements

        .. note:: If this method is called when there is not a message being
            processed, a message will be logged at the ``warning`` level to
            indicate the value is being dropped. To suppress these warnings,
            set the :attr:`rejected.consumer.Consumer.IGNORE_OOB_STATS`
            attribute to `True`.


    .. code-block:: python
       :caption: Example Usage

       class Test(consumer.Consumer):

           @gen.coroutine
           def process(self):
               with self.stats_track_duration('track-time'):
                   yield self._time_consuming_function()

        .. versionadded:: 3.19.0

        :param str key: The key for the timing to track

        """
        start_time = time.time()
        try:
            yield
        finally:
            self.stats_add_duration(key,
                                    max(start_time, time.time()) - start_time)

    def unset_sentry_context(self, tag):
        """Remove a context tag from sentry

        :param str tag: The context tag to remove

        """
        if self.sentry_client:
            self.sentry_client.tags.pop(tag, None)

    @gen.coroutine
    def yield_to_ioloop(self):
        """Function that will allow Rejected to process IOLoop events while
        in a tight-loop inside an asynchronous consumer.

    .. code-block:: python
       :caption: Example Usage

       class Consumer(consumer.Consumer):

           @gen.coroutine
           def process(self):
               for iteration in range(0, 1000000):
                   yield self.yield_to_ioloop()

        """
        try:
            yield self._yield_condition.wait(
                self._message.channel.connection.ioloop.time() + 0.001)
        except gen.TimeoutError:
            pass

    """Internal Methods"""

    @gen.coroutine
    def execute(self, message_in, measurement):
        """Process the message from RabbitMQ. To implement logic for processing
        a message, extend Consumer._process, not this method.

        This for internal use and should not be extended or used directly.

        :param message_in: The message to process
        :type message_in: :class:`rejected.data.Message`
        :param measurement: For collecting per-message instrumentation
        :type measurement: :class:`rejected.data.Measurement`
        :rtype: bool

        """
        self.logger.debug('Received: %r', message_in)
        self._clear()
        self._message = message_in
        self._measurement = measurement

        # If timestamp is set, record age of the message coming in
        if message_in.properties.timestamp:
            message_age = float(
                max(message_in.properties.timestamp, time.time()) -
                message_in.properties.timestamp)
            if message_age > 0:
                measurement.add_duration(self.message_age_key(), message_age)

        # Ensure there is a correlation ID
        self._correlation_id = message_in.properties.correlation_id or \
            message_in.properties.message_id or \
            str(uuid.uuid4())

        # Set the Correlation ID for the connection for logging
        self._connections[message_in.connection].correlation_id = \
            self._correlation_id

        if self.message_type:
            self.set_sentry_context('type', self.message_type)

        # Validate the message type if the child sets MESSAGE_TYPE
        if self._message_type:
            if isinstance(self._message_type, (tuple, list, set)):
                message_supported = self.message_type in self._message_type
            else:
                message_supported = self.message_type == self._message_type

            if not message_supported:
                self.logger.warning('Received unsupported message type: %s',
                                    self.message_type)
                # Should the message be dropped or returned to the broker?
                if self._drop_invalid:
                    if self._drop_exchange:
                        self._republish_dropped_message('invalid type')
                    raise gen.Return(data.MESSAGE_DROP)
                raise gen.Return(data.MESSAGE_EXCEPTION)

        # Check the number of ProcessingErrors and possibly drop the message
        if self._error_max_retry and _PROCESSING_EXCEPTIONS in self.headers:
            if self.headers[_PROCESSING_EXCEPTIONS] >= self._error_max_retry:
                self.logger.warning('Dropping message with %i deaths due to '
                                    'ERROR_MAX_RETRY',
                                    self.headers[_PROCESSING_EXCEPTIONS])
                if self._drop_exchange:
                    self._republish_dropped_message('max retries ({})'.format(
                        self.headers[_PROCESSING_EXCEPTIONS]))
                raise gen.Return(data.MESSAGE_DROP)

        # Prepare and process, catching exceptions
        try:
            result = self.prepare()
            if concurrent.is_future(result):
                yield result
            if not self._finished:
                result = self.process()
                if concurrent.is_future(result):
                    yield result
                    self.logger.debug('Post yield of future process')

        except errors.RabbitMQException as error:
            self.logger.critical('RabbitMQException while processing %s: %s',
                                 message_in.delivery_tag, error)
            self._measurement.set_tag('exception', error.__class__.__name__)
            raise gen.Return(data.RABBITMQ_EXCEPTION)

        except ConfigurationException as error:
            self._log_exception(
                'Exception processing delivery %s: %s',
                message_in.delivery_tag,
                error,
                exc_info=sys.exc_info())
            self._measurement.set_tag('exception', error.__class__.__name__)
            if error.metric:
                self._measurement.set_tag('error', error.metric)
            raise gen.Return(data.CONFIGURATION_EXCEPTION)

        except ConsumerException as error:
            self.logger.error('ConsumerException processing delivery %s: %s',
                              message_in.delivery_tag, error)
            self._measurement.set_tag('exception', error.__class__.__name__)
            if error.metric:
                self._measurement.set_tag('error', error.metric)
            raise gen.Return(data.CONSUMER_EXCEPTION)

        except MessageException as error:
            self.logger.info('MessageException processing delivery %s: %s',
                             message_in.delivery_tag, error)
            self._measurement.set_tag('exception', error.__class__.__name__)
            if error.metric:
                self._measurement.set_tag('error', error.metric)
            raise gen.Return(data.MESSAGE_EXCEPTION)

        except ProcessingException as error:
            self.logger.warning(
                'ProcessingException processing delivery %s: %s',
                message_in.delivery_tag, error)
            self._measurement.set_tag('exception', error.__class__.__name__)
            if error.metric:
                self._measurement.set_tag('error', error.metric)
            self._republish_processing_error(error.metric
                                             or error.__class__.__name__)
            raise gen.Return(data.PROCESSING_EXCEPTION)

        except NotImplementedError as error:
            self._log_exception('NotImplementedError processing delivery'
                                ' %s: %s', message_in.delivery_tag, error)
            self._measurement.set_tag('exception', 'NotImplementedError')
            raise gen.Return(data.UNHANDLED_EXCEPTION)

        except Exception as error:
            self._log_exception(
                'Exception processing delivery %s: %s',
                message_in.delivery_tag,
                error,
                exc_info=sys.exc_info())
            self._measurement.set_tag('exception', 'UnhandledException')
            raise gen.Return(data.UNHANDLED_EXCEPTION)

        finally:
            if not self._finished:
                self.finish()

        # Clean up any pending futures
        for name in self._connections.keys():
            self._connections[name].clear_confirmation_futures()

        self.logger.debug('Post finish')
        raise gen.Return(data.MESSAGE_ACK)

    def remove_connection(self, name):
        """Remove the connection from the available connections for
        publishing.

        This for internal use and should not be extended or used directly.

        :param str name: The connection name

        """
        del self._connections[name]

    def set_connection(self, connection):
        """Assign the connection to the Consumer so that it may be used
        when requested.

        This for internal use and should not be extended or used directly.

        :param connection: The connection to assign
        :type connection  :class:`~rejected.process.Connection`

        """
        self._connections[connection.name] = connection

    @utils.MessageProperty
    def _channel(self):
        """Return the channel of the message that is currently being processed.

        :rtype: :class:`pika.channel.Channel`

        """
        return self._message.channel

    def _clear(self):
        """Resets all assigned data for the current message."""
        self._finished = False
        self._message = None
        self._message_body = None

    @staticmethod
    def _get_pika_properties(properties_in):
        """Return a :class:`pika.spec.BasicProperties` object for a
        :class:`rejected.data.Properties` object.

        :param dict properties_in: Properties to convert
        :rtype: :class:`pika.spec.BasicProperties`

        """
        properties = pika.BasicProperties()
        for key in properties_in or {}:
            if properties_in.get(key) is not None:
                setattr(properties, key, properties_in.get(key))
        return properties

    def _log_exception(self, msg_format, *args, **kwargs):
        """Customize the logging of uncaught exceptions.

        :param str msg_format: format of msg to log with ``self.logger.error``
        :param args: positional arguments to pass to ``self.logger.error``
        :param kwargs: keyword args to pass into ``self.logger.error``
        :keyword bool send_to_sentry: if omitted or *truthy*, this keyword
            will send the captured exception to Sentry (if enabled).

        This for internal use and should not be extended or used directly.

        By default, this method will log the message using
        :meth:`logging.Logger.error` and send the exception to Sentry.
        If an exception is currently active, then the traceback will be
        logged at the debug level.

        """
        self.logger.error(msg_format, *args)
        exc_info = kwargs.get('exc_info', sys.exc_info())
        if all(exc_info):
            exc_type, exc_value, tb = exc_info
            exc_name = exc_type.__name__
            self.logger.exception(
                'Processor handled %s: %s',
                exc_name,
                exc_value,
                exc_info=exc_info)
        self._process.send_exception_to_sentry(exc_info)

    def _publisher_confirmation_future(self, name, exchange, routing_key,
                                       properties):
        """Return a future a publisher confirmation result that enables
        consumers to block on the confirmation of a published message.

        Two internal dicts are used for keeping track of state.
        Consumer._delivery_tags is a dict of connection names that keeps
        the last delivery tag expectation and is used to correlate the future
        with the delivery tag that is expected to be confirmed from RabbitMQ.

        This for internal use and should not be extended or used directly.

        :param str name: The connection name for the future
        :param str exchange: The exchange the message was published to
        :param str routing_key: The routing key that was used
        :param properties: The AMQP message properties for the delivery
        :type properties: pika.spec.Basic.Properties
        :rtype: concurrent.Future.

        """
        if self._connections[name].publisher_confirmations:
            future = concurrent.Future()
            self._connections[name].add_confirmation_future(
                exchange, routing_key, properties, future)
            return future

    def _publish_connection(self, name=None):
        """Return the connection to publish. If the name is not specified,
        the connection associated with the current message is returned.

        :param str name:
        :rtype: rejected.process.Connection

        """
        try:
            conn = self._connections[name or self._message.connection]
        except KeyError:
            raise ValueError('Channel {} not found'.format(name))
        if not conn.is_connected or conn.channel.is_closed:
            raise errors.RabbitMQException(conn.name, 599, 'NOT_CONNECTED')
        return conn

    def _republish_dropped_message(self, reason):
        """Republish the original message that was received it is being dropped
        by the consumer.

        This for internal use and should not be extended or used directly.

        :param str reason: The reason the message was dropped

        """
        self.logger.debug('Republishing due to ProcessingException')
        properties = dict(self._message.properties) or {}
        if 'headers' not in properties or not properties['headers']:
            properties['headers'] = {}
        properties['headers']['X-Dropped-By'] = self.name
        properties['headers']['X-Dropped-Reason'] = reason
        properties['headers']['X-Dropped-Timestamp'] = \
            datetime.datetime.utcnow().isoformat()
        properties['headers']['X-Original-Exchange'] = self._message.exchange

        self._message.channel.basic_publish(
            self._drop_exchange, self._message.routing_key, self._message.body,
            pika.BasicProperties(**properties))

    def _republish_processing_error(self, error):
        """Republish the original message that was received because a
        :exc:`~rejected.consumer.ProcessingException` was raised.

        This for internal use and should not be extended or used directly.

        Add a header that keeps track of how many times this has happened
        for this message.

        :param str error: The string value for the exception

        """
        self.logger.debug('Republishing due to ProcessingException')
        properties = dict(self._message.properties) or {}
        if 'headers' not in properties or not properties['headers']:
            properties['headers'] = {}

        if error:
            properties['headers']['X-Processing-Exception'] = error

        if _PROCESSING_EXCEPTIONS not in properties['headers']:
            properties['headers'][_PROCESSING_EXCEPTIONS] = 1
        else:
            try:
                properties['headers'][_PROCESSING_EXCEPTIONS] += 1
            except TypeError:
                properties['headers'][_PROCESSING_EXCEPTIONS] = 1

        self._message.channel.basic_publish(self._error_exchange,
                                            self._message.routing_key,
                                            self._message.body,
                                            pika.BasicProperties(**properties))


class SmartConsumer(Consumer):
    """Base class to ease the implementation of strongly typed message
    consumers that validate and automatically decode and deserialize the
    inbound message body based upon the message properties. Additionally,
    should one of the supported ``content_encoding`` types (``gzip`` or
    ``bzip2``) be specified in the message's property, it will automatically
    be decoded.

    When publishing a message, the message can be automatically serialized
    and encoded. If the ``content_type`` property is specified, the consumer
    will attempt to automatically serialize the message body. If the
    ``content_encoding`` property is specified using a supported encoding
    (``gzip`` or ``bzip2``), it will automatically be encoded as well.

    *Supported MIME types for automatic serialization and deserialization are:*

     - application/json
     - application/msgpack
     - application/pickle
     - application/x-pickle
     - application/x-plist
     - application/x-vnd.python.pickle
     - application/vnd.python.pickle
     - text/csv
     - text/html (with beautifulsoup4 installed)
     - text/xml (with beautifulsoup4 installed)
     - text/yaml
     - text/x-yaml

    In any of the consumer base classes, if the ``MESSAGE_TYPE`` attribute is
    set, the ``type`` property of incoming messages will be validated against
    when a message is received, checking for string equality against the
    ``MESSAGE_TYPE`` attribute. If they are not matched, the consumer will not
    process the message and will drop the message without an exception if the
    ``DROP_INVALID_MESSAGES`` attribute is set to ``True``. If it is ``False``,
    a :py:class:`ConsumerException` is raised.

    .. note:: Since 3.17, :class:`~rejected.consumer.SmartConsumer` and
        :class:`~rejected.consumer.SmartPublishingConsumer` have been combined
        into the same class.

    """
    _IGNORE_TYPES = {'application/octet-stream', 'text/plain'}

    _CODEC_MAP = {
        'bzip2': 'bz2',
        'gzip': 'zlib',
        'zlib': 'zlib'
    }

    _SERIALIZATION_MAP = flatdict.FlatDict({
        'application': {
            'json': {
                'module': 'json',
                'load': 'loads',
                'dump': 'dumps',
                'encoding': True
            },
            'msgpack': {
                'module': 'umsgpack',
                'load': 'unpackb',
                'dump': 'packb',
                'binary': True,
                'enabled': False
            },
            'pickle': {
                'module': 'pickle',
                'load': 'loads',
                'dump': 'dumps',
                'binary': True
            },
            'x-pickle': {
                'module': 'pickle',
                'load': 'loads',
                'dump': 'dumps',
                'binary': True
            },
            'x-plist': {
                'module': 'plistlib',
                'load': 'loads' if _PYTHON3 else 'readPlistFromString',
                'dump': 'dumps' if _PYTHON3 else 'writePlistToString',
                'binary': True
            },
            'vnd.python.pickle': {
                'module': 'pickle',
                'load': 'loads',
                'dump': 'dumps',
                'binary': True
            },
            'x-vnd.python.pickle': {
                'module': 'pickle',
                'load': 'loads',
                'dump': 'dumps',
                'binary': True
            }
        },
        'text': {
            'csv': {
                'module': 'csv',
                'load': '_load_csv',
                'dump': '_dump_csv',
                'common': False
            },
            'html': {
                'module': 'bs4',
                'load': '_load_html',
                'dump': '_dump_bs4',
                'common': False,
                'enabled': False
            },
            'xml': {
                'module': 'bs4',
                'load': '_load_xml',
                'dump': '_dump_bs4',
                'common': False,
                'enabled': False
            },
            'yaml': {
                'module': 'yaml',
                'load': 'load',
                'dump': 'dump'
            }
        }
    }, delimiter='/')

    def __init__(self, *args, **kwargs):
        """Creates a new instance of the
        :class:`~rejected.consumer.SmartConsumer` class. To perform
        initialization tasks, extend
        :meth:`~rejected.consumer.SmartConsumer.initialize` or ensure you
        :meth:`super` this method first.

        """
        super(SmartConsumer, self).__init__(*args, **kwargs)
        installed = pkg_resources.AvailableDistributions()
        for key, pkg in {'application/msgpack': 'u-msgpack-python',
                         'text/html': ('beautifulsoup4', 'lxml'),
                         'text/xml': ('beautifulsoup4', 'lxml')}.items():
            if isinstance(pkg, tuple):
                self._SERIALIZATION_MAP[key]['enabled'] = \
                    all([p in installed for p in pkg])
            else:
                self._SERIALIZATION_MAP[key]['enabled'] = pkg in installed
            self.logger.debug(
                '%s is %s in serialization map', key,
                'enabled' if self._SERIALIZATION_MAP[key]['enabled'] else
                'disabled')

    def publish_message(self,
                        exchange,
                        routing_key,
                        properties,
                        body,
                        no_serialization=False,
                        no_encoding=False,
                        channel=None,
                        connection=None):
        """Publish a message to RabbitMQ on the same channel the original
        message was received on.

        By default, if you pass a non-string object to the body and the
        properties have a supported ``content_type`` set, the body will be
        auto-serialized in the specified ``content_type``.

        If the properties do not have a timestamp set, it will be set to the
        current time.

        If you specify a ``content_encoding`` in the properties and the
        encoding is supported, the body will be auto-encoded.

        Both of these behaviors can be disabled by setting
        ``no_serialization`` or ``no_encoding`` to ``True``.

        If you pass an unsupported content-type or content-encoding when using
        the auto-serialization and auto-encoding features, a :exc:`ValueError`
        will be raised.

        .. versionchanged:: 4.0.0
           The method returns a :py:class:`~tornado.concurrent.Future` if
           `publisher confirmations <https://www.rabbitmq.com/confirms.html>`_
           are enabled on for the connection. In addition, The ``channel``
           parameter is deprecated and will be removed in a future release.

        :param str exchange: The exchange to publish to
        :param str routing_key: The routing key to publish with
        :param dict properties: The message properties
        :param mixed body: The message body to publish
        :param bool no_serialization: Turn off auto-serialization of the body
        :param bool no_encoding: Turn off auto-encoding of the body
        :param str channel: **Deprecated in 4.0.0** Specify the connection
            parameter instead.
        :param str connection: The connection to use. If it is not
            specified, the channel that the message was delivered on is used.
        :rtype: tornado.concurrent.Future or None
        :raises: ValueError

        """
        # Auto-serialize the content if needed
        is_string = (isinstance(body, str) or isinstance(body, bytes)
                     or isinstance(body, unicode))
        if not no_serialization and not is_string and \
                properties.get('content_type'):
            body = self._serialize(
                body, headers.parse_content_type(properties['content_type']))

        # Auto-encode the message body if needed
        if not no_encoding and \
                properties.get('content_encoding') in self._CODEC_MAP.keys():
            body = self._compress(
                body, self._CODEC_MAP[properties['content_encoding']])

        return super(SmartConsumer, self).publish_message(
            exchange, routing_key, properties, body, channel or connection)

    @property
    def body(self):
        """Return the message body, unencoded if needed,
        deserialized if possible.

        :rtype: any

        """
        if self._message_body:  # If already set, return it
            return self._message_body
        self._message_body = self._maybe_decompress_body()
        self._message_body = self._maybe_deserialize_body()
        return self._message_body

    def _compress(self, value, module_name):
        """Compress the value passed in using the named compression module.

        :param bytes value: The uncompressed value
        :rtype: bytes

        """
        self.logger.debug('Decompressing with %s', module_name)
        if not isinstance(value, bytes):
            value = value.encode('utf-8')
        return self._maybe_import(module_name).compress(value)

    @staticmethod
    def _dump_bs4(value):
        """Return a BeautifulSoup object as a string

        :param bs4.BeautifulSoup value: The object to return a string from
        :rtype: str

        """
        return str(value)

    def _dump_csv(self, rows):
        """Take a list of dicts and return it as a CSV value. The

        .. versionchanged:: 4.0.0

        :param list rows: A list of lists to return as a CSV
        :rtype: str

        """
        self.logger.debug('Writing %r', rows)
        csv = self._maybe_import('csv')
        buff = io.StringIO() if _PYTHON3 else io.BytesIO()
        writer = csv.DictWriter(
            buff,
            sorted(set([k for r in rows for k in r.keys()])),
            dialect='excel')
        writer.writeheader()
        writer.writerows(rows)
        value = buff.getvalue()
        buff.close()
        return value

    def _load_bs4(self, value, markup):
        """Load an HTML or XML string into a :class:`bs4.BeautifulSoup`
        instance.

        :param str value: The HTML or XML string
        :param str markup: One of ``html`` or ``xml``
        :rtype: bs4.BeautifulSoup
        :raises: ConsumerException

        """
        return self._maybe_import('bs4').BeautifulSoup(
            value, 'lxml' if markup == 'html' else 'lxml-xml')

    def _load_csv(self, value):
        """Return a class:`csv.DictReader` instance for value passed in.

        :param str value: The CSV value
        :rtype: csv.DictReader

        """
        csv = self._maybe_import('csv')
        buff = io.StringIO() if _PYTHON3 else io.BytesIO()
        buff.write(value)
        buff.seek(0)
        dialect = csv.Sniffer().sniff(buff.read(1024))
        buff.seek(0)
        return csv.DictReader(buff, dialect=dialect)

    def _load_html(self, value):
        """Load a HTML string into a :class:`bs4.BeautifulSoup` instance.

        :param str value: The HTML string
        :rtype: bs4.BeautifulSoup

        """
        return self._load_bs4(value, 'html')

    def _load_xml(self, value):
        """Load a XML string into a :class:`bs4.BeautifulSoup` instance.

        :param str value: The XML string
        :rtype: bs4.BeautifulSoup

        """
        return self._load_bs4(value, 'xml')

    def _maybe_decode(self, value, encoding='utf-8'):
        """If a bytes object is passed in, in the Python 3 environment,
        decode it using the specified encoding to turn it to a str instance.

        :param mixed value: The value to possibly decode
        :param str encoding: The encoding to use
        :rtype: str

        """
        if _PYTHON3 and isinstance(value, bytes):
            try:
                return value.decode(encoding)
            except Exception as err:
                self.logger.exception('Error decoding value: %s', err)
                raise MessageException(
                    str(err), 'decoding-{}'.format(encoding))
        return value

    def _maybe_decompress_body(self):
        """Attempt to decompress the message body passed in using the named
        compression module, if specified.

        :rtype: bytes

        """
        if self.content_encoding:
            if self.content_encoding in self._CODEC_MAP.keys():
                module_name = self._CODEC_MAP[self.content_encoding]
                self.logger.debug('Decompressing with %s', module_name)
                module = self._maybe_import(module_name)
                return module.decompress(self._message.body)
            self.logger.debug('Unsupported content-encoding: %s',
                              self.content_encoding)
        return self._message.body

    def _maybe_deserialize_body(self):
        """Attempt to deserialize the message body based upon the content-type.

        :rtype: mixed

        """
        if not self.content_type:
            return self._message_body
        ct = headers.parse_content_type(self.content_type)
        key = '{}/{}'.format(ct.content_type, ct.content_subtype)
        if key not in self._SERIALIZATION_MAP:
            if key not in self._IGNORE_TYPES:
                self.logger.debug('Unsupported content-type: %s',
                                  self.content_type)
            return self._message_body
        elif not self._SERIALIZATION_MAP[key].get('enabled', True):
            self.logger.debug('%s is not enabled in the serialization map',
                              key)
            return self._message_body
        value = self._message_body
        if not self._SERIALIZATION_MAP[key].get('binary'):
            value = self._maybe_decode(
                self._message_body, ct.parameters.get('charset', 'utf-8'))
        return self._maybe_invoke_serialization(value, 'load', key)

    def _maybe_import(self, module):
        if not hasattr(sys.modules[__name__], module):
            self.logger.debug('Importing %s', module)
            setattr(sys.modules[__name__], module,
                    importlib.import_module(module))
        return sys.modules[module]

    def _maybe_invoke_serialization(self, value, method_type, key):
        if self._SERIALIZATION_MAP[key].get('common', True):
            self.logger.debug('Invoking %s %s using %s', method_type, key,
                              self._SERIALIZATION_MAP[key]['module'])
            method = getattr(
                self._maybe_import(self._SERIALIZATION_MAP[key]['module']),
                self._SERIALIZATION_MAP[key][method_type])
        else:
            method = getattr(self, self._SERIALIZATION_MAP[key][method_type])
        self.logger.debug('Invoking %r for %s %s', method, method_type, key)
        try:
            return method(value)
        except Exception as err:
            self.logger.exception('Error %sing body: %s', method_type, err)
            raise MessageException(
                str(err), 'serialization-{}'.format(method_type))

    def _serialize(self, value, ct):
        """Auto-serialization of the value based upon the content-type value.

        :param any value: The value to serialize
        :param ietfparse.: The content type to serialize
        :rtype: str
        :raises: ValueError

        """
        key = '{}/{}'.format(ct.content_type, ct.content_subtype)
        if key not in self._SERIALIZATION_MAP:
            raise ValueError('Unsupported content-type: {}'.format(key))
        elif not self._SERIALIZATION_MAP[key].get('enabled', True):
            self.logger.debug('%s is not enabled in the serialization map',
                              key)
            raise ValueError('Disabled content-type: {}'.format(key))
        return self._maybe_invoke_serialization(
            self._maybe_decode(value, ct.parameters.get('charset', 'utf-8')),
            'dump', key)


class ConfigurationException(errors.RejectedException):
    """Raised when :py:meth:`~rejected.consumer.Consumer.require_setting` is
    invoked and the specified setting was not configured. When raised, the
    consumer will shutdown.

    .. versionadded:: 4.0.0

    """


class ConsumerException(errors.RejectedException):
    """May be called when processing a message to indicate a problem that the
    Consumer may be experiencing that should cause it to stop.

    .. versionchanged:: 3.19.0

    :param str value: An optional value used in string representation
    :param str metric: An optional value for auto-instrumentation of exceptions

    """


class MessageException(errors.RejectedException):
    """Invoke when a message should be rejected and not re-queued, but not due
    to a processing error that should cause the consumer to stop.

    .. versionchanged:: 3.19.0

    :param str value: An optional value used in string representation
    :param str metric: An optional value for auto-instrumentation of exceptions

    """


class ProcessingException(errors.RejectedException):
    """Invoke when a message should be rejected and not re-queued, but not due
    to a processing error that should cause the consumer to stop. This should
    be used for when you want to reject a message which will be republished to
    a retry queue, without anything being stated about the exception.

    .. versionchanged:: 3.19.0

    :param str value: An optional value used in string representation
    :param str metric: An optional value for auto-instrumentation of exceptions

    """
