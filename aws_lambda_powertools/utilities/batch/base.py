# -*- coding: utf-8 -*-

"""
Batch processing utilities
"""
import asyncio
import copy
import inspect
import logging
import os
import sys
from abc import ABC, abstractmethod
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Tuple,
    Type,
    Union,
    overload,
)

from aws_lambda_powertools.middleware_factory import lambda_handler_decorator
from aws_lambda_powertools.shared import constants
from aws_lambda_powertools.utilities.batch.exceptions import (
    BatchProcessingError,
    ExceptionInfo,
)
from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import (
    DynamoDBRecord,
)
from aws_lambda_powertools.utilities.data_classes.kinesis_stream_event import (
    KinesisStreamRecord,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

logger = logging.getLogger(__name__)


class EventType(Enum):
    SQS = "SQS"
    KinesisDataStreams = "KinesisDataStreams"
    DynamoDBStreams = "DynamoDBStreams"


#
# type specifics
#
has_pydantic = "pydantic" in sys.modules

# For IntelliSense and Mypy to work, we need to account for possible SQS, Kinesis and DynamoDB subclasses
# We need them as subclasses as we must access their message ID or sequence number metadata via dot notation
if has_pydantic:
    from aws_lambda_powertools.utilities.parser.models import DynamoDBStreamRecordModel
    from aws_lambda_powertools.utilities.parser.models import (
        KinesisDataStreamRecord as KinesisDataStreamRecordModel,
    )
    from aws_lambda_powertools.utilities.parser.models import SqsRecordModel

    BatchTypeModels = Optional[
        Union[Type[SqsRecordModel], Type[DynamoDBStreamRecordModel], Type[KinesisDataStreamRecordModel]]
    ]

# When using processor with default arguments, records will carry EventSourceDataClassTypes
# and depending on what EventType it's passed it'll correctly map to the right record
# When using Pydantic Models, it'll accept any subclass from SQS, DynamoDB and Kinesis
EventSourceDataClassTypes = Union[SQSRecord, KinesisStreamRecord, DynamoDBRecord]
BatchEventTypes = Union[EventSourceDataClassTypes, "BatchTypeModels"]
SuccessResponse = Tuple[str, Any, BatchEventTypes]
FailureResponse = Tuple[str, str, BatchEventTypes]


class BasePartialProcessor(ABC):
    """
    Abstract class for batch processors.
    """

    lambda_context: LambdaContext

    def __init__(self):
        self.success_messages: List[BatchEventTypes] = []
        self.fail_messages: List[BatchEventTypes] = []
        self.exceptions: List[ExceptionInfo] = []

    @abstractmethod
    def _prepare(self):
        """
        Prepare context manager.
        """
        raise NotImplementedError()

    @abstractmethod
    def _clean(self):
        """
        Clear context manager.
        """
        raise NotImplementedError()

    @abstractmethod
    def _process_record(self, record: dict):
        """
        Process record with handler.
        """
        raise NotImplementedError()

    def process(self) -> List[Tuple]:
        """
        Call instance's handler for each record.
        """
        return [self._process_record(record) for record in self.records]

    @abstractmethod
    async def _async_process_record(self, record: dict):
        """
        Async process record with handler.
        """
        raise NotImplementedError()

    def async_process(self) -> List[Tuple]:
        """
        Async call instance's handler for each record.

        Note
        ----

        We keep the outer function synchronous to prevent making Lambda handler async, so to not impact
        customers' existing middlewares. Instead, we create an async closure to handle asynchrony.

        We also handle edge cases like Lambda container thaw by getting an existing or creating an event loop.

        See: https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtime-environment.html#runtimes-lifecycle-shutdown
        """

        async def async_process_closure():
            return list(await asyncio.gather(*[self._async_process_record(record) for record in self.records]))

        # WARNING
        # Do not use "asyncio.run(async_process())" due to Lambda container thaws/freeze, otherwise we might get "Event Loop is closed" # noqa: E501
        # Instead, get_event_loop() can also create one if a previous was erroneously closed
        # Mangum library does this as well. It's battle tested with other popular async-only frameworks like FastAPI
        # https://github.com/jordaneremieff/mangum/discussions/256#discussioncomment-2638946
        # https://github.com/jordaneremieff/mangum/blob/b85cd4a97f8ddd56094ccc540ca7156c76081745/mangum/protocols/http.py#L44

        # Let's prime the coroutine and decide
        # whether we create an event loop (Lambda) or schedule it as usual (non-Lambda)
        coro = async_process_closure()
        if os.getenv(constants.LAMBDA_TASK_ROOT_ENV):
            loop = asyncio.get_event_loop()  # NOTE: this might return an error starting in Python 3.12 in a few years
            task_instance = loop.create_task(coro)
            return loop.run_until_complete(task_instance)

        # Non-Lambda environment, run coroutine as usual
        return asyncio.run(coro)

    def __enter__(self):
        self._prepare()
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self._clean()

    def __call__(self, records: List[dict], handler: Callable, lambda_context: Optional[LambdaContext] = None):
        """
        Set instance attributes before execution

        Parameters
        ----------
        records: List[dict]
            List with objects to be processed.
        handler: Callable
            Callable to process "records" entries.
        """
        self.records = records
        self.handler = handler

        # NOTE: If a record handler has `lambda_context` parameter in its function signature, we inject it.
        # This is the earliest we can inspect for signature to prevent impacting performance.
        #
        #   Mechanism:
        #
        #   1. When using the `@batch_processor` decorator, this happens automatically.
        #   2. When using the context manager, customers have to include `lambda_context` param.
        #
        #   Scenario: Injects Lambda context
        #
        #   def record_handler(record, lambda_context): ... # noqa: E800
        #   with processor(records=batch, handler=record_handler, lambda_context=context): ... # noqa: E800
        #
        #   Scenario: Does NOT inject Lambda context (default)
        #
        #   def record_handler(record): pass # noqa: E800
        #   with processor(records=batch, handler=record_handler): ... # noqa: E800
        #
        if lambda_context is None:
            self._handler_accepts_lambda_context = False
        else:
            self.lambda_context = lambda_context
            self._handler_accepts_lambda_context = "lambda_context" in inspect.signature(self.handler).parameters

        return self

    def success_handler(self, record, result: Any) -> SuccessResponse:
        """
        Keeps track of batch records that were processed successfully

        Parameters
        ----------
        record: Any
            record that succeeded processing
        result: Any
            result from record handler

        Returns
        -------
        SuccessResponse
            "success", result, original record
        """
        entry = ("success", result, record)
        self.success_messages.append(record)
        return entry

    def failure_handler(self, record, exception: ExceptionInfo) -> FailureResponse:
        """
        Keeps track of batch records that failed processing

        Parameters
        ----------
        record: Any
            record that failed processing
        exception: ExceptionInfo
            Exception information containing type, value, and traceback (sys.exc_info())

        Returns
        -------
        FailureResponse
            "fail", exceptions args, original record
        """
        exception_string = f"{exception[0]}:{exception[1]}"
        entry = ("fail", exception_string, record)
        logger.debug(f"Record processing exception: {exception_string}")
        self.exceptions.append(exception)
        self.fail_messages.append(record)
        return entry


class BasePartialBatchProcessor(BasePartialProcessor):  # noqa
    DEFAULT_RESPONSE: Dict[str, List[Optional[dict]]] = {"batchItemFailures": []}

    def __init__(self, event_type: EventType, model: Optional["BatchTypeModels"] = None):
        """Process batch and partially report failed items

        Parameters
        ----------
        event_type: EventType
            Whether this is a SQS, DynamoDB Streams, or Kinesis Data Stream event
        model: Optional["BatchTypeModels"]
            Parser's data model using either SqsRecordModel, DynamoDBStreamRecordModel, KinesisDataStreamRecord

        Exceptions
        ----------
        BatchProcessingError
            Raised when the entire batch has failed processing
        """
        self.event_type = event_type
        self.model = model
        self.batch_response = copy.deepcopy(self.DEFAULT_RESPONSE)
        self._COLLECTOR_MAPPING = {
            EventType.SQS: self._collect_sqs_failures,
            EventType.KinesisDataStreams: self._collect_kinesis_failures,
            EventType.DynamoDBStreams: self._collect_dynamodb_failures,
        }
        self._DATA_CLASS_MAPPING = {
            EventType.SQS: SQSRecord,
            EventType.KinesisDataStreams: KinesisStreamRecord,
            EventType.DynamoDBStreams: DynamoDBRecord,
        }

        super().__init__()

    def response(self):
        """Batch items that failed processing, if any"""
        return self.batch_response

    def _prepare(self):
        """
        Remove results from previous execution.
        """
        self.success_messages.clear()
        self.fail_messages.clear()
        self.exceptions.clear()
        self.batch_response = copy.deepcopy(self.DEFAULT_RESPONSE)

    def _clean(self):
        """
        Report messages to be deleted in case of partial failure.
        """

        if not self._has_messages_to_report():
            return

        if self._entire_batch_failed():
            raise BatchProcessingError(
                msg=f"All records failed processing. {len(self.exceptions)} individual errors logged "
                f"separately below.",
                child_exceptions=self.exceptions,
            )

        messages = self._get_messages_to_report()
        self.batch_response = {"batchItemFailures": messages}

    def _has_messages_to_report(self) -> bool:
        if self.fail_messages:
            return True

        logger.debug(f"All {len(self.success_messages)} records successfully processed")
        return False

    def _entire_batch_failed(self) -> bool:
        return len(self.exceptions) == len(self.records)

    def _get_messages_to_report(self) -> List[Dict[str, str]]:
        """
        Format messages to use in batch deletion
        """
        return self._COLLECTOR_MAPPING[self.event_type]()

    # Event Source Data Classes follow python idioms for fields
    # while Parser/Pydantic follows the event field names to the latter
    def _collect_sqs_failures(self):
        failures = []
        for msg in self.fail_messages:
            msg_id = msg.messageId if self.model else msg.message_id
            failures.append({"itemIdentifier": msg_id})
        return failures

    def _collect_kinesis_failures(self):
        failures = []
        for msg in self.fail_messages:
            msg_id = msg.kinesis.sequenceNumber if self.model else msg.kinesis.sequence_number
            failures.append({"itemIdentifier": msg_id})
        return failures

    def _collect_dynamodb_failures(self):
        failures = []
        for msg in self.fail_messages:
            msg_id = msg.dynamodb.SequenceNumber if self.model else msg.dynamodb.sequence_number
            failures.append({"itemIdentifier": msg_id})
        return failures

    @overload
    def _to_batch_type(self, record: dict, event_type: EventType, model: "BatchTypeModels") -> "BatchTypeModels":
        ...  # pragma: no cover

    @overload
    def _to_batch_type(self, record: dict, event_type: EventType) -> EventSourceDataClassTypes:
        ...  # pragma: no cover

    def _to_batch_type(self, record: dict, event_type: EventType, model: Optional["BatchTypeModels"] = None):
        if model is not None:
            return model.parse_obj(record)
        return self._DATA_CLASS_MAPPING[event_type](record)


class BatchProcessor(BasePartialBatchProcessor):  # Keep old name for compatibility
    """Process native partial responses from SQS, Kinesis Data Streams, and DynamoDB.

    Example
    -------

    ## Process batch triggered by SQS

    ```python
    import json

    from aws_lambda_powertools import Logger, Tracer
    from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, batch_processor
    from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


    processor = BatchProcessor(event_type=EventType.SQS)
    tracer = Tracer()
    logger = Logger()


    @tracer.capture_method
    def record_handler(record: SQSRecord):
        payload: str = record.body
        if payload:
            item: dict = json.loads(payload)
        ...

    @logger.inject_lambda_context
    @tracer.capture_lambda_handler
    @batch_processor(record_handler=record_handler, processor=processor)
    def lambda_handler(event, context: LambdaContext):
        return processor.response()
    ```

    ## Process batch triggered by Kinesis Data Streams

    ```python
    import json

    from aws_lambda_powertools import Logger, Tracer
    from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, batch_processor
    from aws_lambda_powertools.utilities.data_classes.kinesis_stream_event import KinesisStreamRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


    processor = BatchProcessor(event_type=EventType.KinesisDataStreams)
    tracer = Tracer()
    logger = Logger()


    @tracer.capture_method
    def record_handler(record: KinesisStreamRecord):
        logger.info(record.kinesis.data_as_text)
        payload: dict = record.kinesis.data_as_json()
        ...

    @logger.inject_lambda_context
    @tracer.capture_lambda_handler
    @batch_processor(record_handler=record_handler, processor=processor)
    def lambda_handler(event, context: LambdaContext):
        return processor.response()
    ```

    ## Process batch triggered by DynamoDB Data Streams

    ```python
    import json

    from aws_lambda_powertools import Logger, Tracer
    from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, batch_processor
    from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


    processor = BatchProcessor(event_type=EventType.DynamoDBStreams)
    tracer = Tracer()
    logger = Logger()


    @tracer.capture_method
    def record_handler(record: DynamoDBRecord):
        logger.info(record.dynamodb.new_image)
        payload: dict = json.loads(record.dynamodb.new_image.get("item"))
        # alternatively:
        # changes: Dict[str, Any] = record.dynamodb.new_image  # noqa: E800
        # payload = change.get("Message") -> "<payload>"
        ...

    @logger.inject_lambda_context
    @tracer.capture_lambda_handler
    def lambda_handler(event, context: LambdaContext):
        batch = event["Records"]
        with processor(records=batch, processor=processor):
            processed_messages = processor.process() # kick off processing, return list[tuple]

        return processor.response()
    ```


    Raises
    ------
    BatchProcessingError
        When all batch records fail processing

    Limitations
    -----------
    * Async record handler not supported, use AsyncBatchProcessor instead.
    """

    async def _async_process_record(self, record: dict):
        raise NotImplementedError()

    def _process_record(self, record: dict) -> Union[SuccessResponse, FailureResponse]:
        """
        Process a record with instance's handler

        Parameters
        ----------
        record: dict
            A batch record to be processed.
        """
        data = self._to_batch_type(record=record, event_type=self.event_type, model=self.model)
        try:
            if self._handler_accepts_lambda_context:
                result = self.handler(record=data, lambda_context=self.lambda_context)
            else:
                result = self.handler(record=data)

            return self.success_handler(record=record, result=result)
        except Exception:
            return self.failure_handler(record=data, exception=sys.exc_info())


@lambda_handler_decorator
def batch_processor(
    handler: Callable, event: Dict, context: LambdaContext, record_handler: Callable, processor: BatchProcessor
):
    """
    Middleware to handle batch event processing

    Parameters
    ----------
    handler: Callable
        Lambda's handler
    event: Dict
        Lambda's Event
    context: LambdaContext
        Lambda's Context
    record_handler: Callable
        Callable or corutine to process each record from the batch
    processor: BatchProcessor
        Batch Processor to handle partial failure cases

    Examples
    --------
    **Processes Lambda's event with a BasePartialProcessor**

        >>> from aws_lambda_powertools.utilities.batch import batch_processor, BatchProcessor
        >>>
        >>> def record_handler(record):
        >>>     return record["body"]
        >>>
        >>> @batch_processor(record_handler=record_handler, processor=BatchProcessor())
        >>> def handler(event, context):
        >>>     return {"StatusCode": 200}

    Limitations
    -----------
    * Async batch processors. Use `async_batch_processor` instead.
    """
    records = event["Records"]

    with processor(records, record_handler, lambda_context=context):
        processor.process()

    return handler(event, context)


class AsyncBatchProcessor(BasePartialBatchProcessor):
    """Process native partial responses from SQS, Kinesis Data Streams, and DynamoDB asynchronously.

    Example
    -------

    ## Process batch triggered by SQS

    ```python
    import json

    from aws_lambda_powertools import Logger, Tracer
    from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, batch_processor
    from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


    processor = BatchProcessor(event_type=EventType.SQS)
    tracer = Tracer()
    logger = Logger()


    @tracer.capture_method
    async def record_handler(record: SQSRecord):
        payload: str = record.body
        if payload:
            item: dict = json.loads(payload)
        ...

    @logger.inject_lambda_context
    @tracer.capture_lambda_handler
    @batch_processor(record_handler=record_handler, processor=processor)
    def lambda_handler(event, context: LambdaContext):
        return processor.response()
    ```

    ## Process batch triggered by Kinesis Data Streams

    ```python
    import json

    from aws_lambda_powertools import Logger, Tracer
    from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, batch_processor
    from aws_lambda_powertools.utilities.data_classes.kinesis_stream_event import KinesisStreamRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


    processor = BatchProcessor(event_type=EventType.KinesisDataStreams)
    tracer = Tracer()
    logger = Logger()


    @tracer.capture_method
    async def record_handler(record: KinesisStreamRecord):
        logger.info(record.kinesis.data_as_text)
        payload: dict = record.kinesis.data_as_json()
        ...

    @logger.inject_lambda_context
    @tracer.capture_lambda_handler
    @batch_processor(record_handler=record_handler, processor=processor)
    def lambda_handler(event, context: LambdaContext):
        return processor.response()
    ```

    ## Process batch triggered by DynamoDB Data Streams

    ```python
    import json

    from aws_lambda_powertools import Logger, Tracer
    from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, batch_processor
    from aws_lambda_powertools.utilities.data_classes.dynamo_db_stream_event import DynamoDBRecord
    from aws_lambda_powertools.utilities.typing import LambdaContext


    processor = BatchProcessor(event_type=EventType.DynamoDBStreams)
    tracer = Tracer()
    logger = Logger()


    @tracer.capture_method
    async def record_handler(record: DynamoDBRecord):
        logger.info(record.dynamodb.new_image)
        payload: dict = json.loads(record.dynamodb.new_image.get("item"))
        # alternatively:
        # changes: Dict[str, Any] = record.dynamodb.new_image  # noqa: E800
        # payload = change.get("Message") -> "<payload>"
        ...

    @logger.inject_lambda_context
    @tracer.capture_lambda_handler
    def lambda_handler(event, context: LambdaContext):
        batch = event["Records"]
        with processor(records=batch, processor=processor):
            processed_messages = processor.process() # kick off processing, return list[tuple]

        return processor.response()
    ```


    Raises
    ------
    BatchProcessingError
        When all batch records fail processing

    Limitations
    -----------
    * Sync record handler not supported, use BatchProcessor instead.
    """

    def _process_record(self, record: dict):
        raise NotImplementedError()

    async def _async_process_record(self, record: dict) -> Union[SuccessResponse, FailureResponse]:
        """
        Process a record with instance's handler

        Parameters
        ----------
        record: dict
            A batch record to be processed.
        """
        data = self._to_batch_type(record=record, event_type=self.event_type, model=self.model)
        try:
            if self._handler_accepts_lambda_context:
                result = await self.handler(record=data, lambda_context=self.lambda_context)
            else:
                result = await self.handler(record=data)

            return self.success_handler(record=record, result=result)
        except Exception:
            return self.failure_handler(record=data, exception=sys.exc_info())


@lambda_handler_decorator
def async_batch_processor(
    handler: Callable,
    event: Dict,
    context: LambdaContext,
    record_handler: Callable[..., Awaitable[Any]],
    processor: AsyncBatchProcessor,
):
    """
    Middleware to handle batch event processing
    Parameters
    ----------
    handler: Callable
        Lambda's handler
    event: Dict
        Lambda's Event
    context: LambdaContext
        Lambda's Context
    record_handler: Callable[..., Awaitable[Any]]
        Callable to process each record from the batch
    processor: AsyncBatchProcessor
        Batch Processor to handle partial failure cases
    Examples
    --------
    **Processes Lambda's event with a BasePartialProcessor**
        >>> from aws_lambda_powertools.utilities.batch import async_batch_processor, AsyncBatchProcessor
        >>>
        >>> async def async_record_handler(record):
        >>>     payload: str = record.body
        >>>     return payload
        >>>
        >>> processor = AsyncBatchProcessor(event_type=EventType.SQS)
        >>>
        >>> @async_batch_processor(record_handler=async_record_handler, processor=processor)
        >>> async def lambda_handler(event, context: LambdaContext):
        >>>     return processor.response()

    Limitations
    -----------
    * Sync batch processors. Use `batch_processor` instead.
    """
    records = event["Records"]

    with processor(records, record_handler, lambda_context=context):
        processor.async_process()

    return handler(event, context)
