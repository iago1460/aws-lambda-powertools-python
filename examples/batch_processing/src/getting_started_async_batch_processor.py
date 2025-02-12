import httpx  # external dependency

from aws_lambda_powertools.utilities.batch import (
    AsyncBatchProcessor,
    EventType,
    async_batch_processor,
)
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord
from aws_lambda_powertools.utilities.typing import LambdaContext

processor = AsyncBatchProcessor(event_type=EventType.SQS)


async def async_record_handler(record: SQSRecord):
    # Yield control back to the event loop to schedule other tasks
    # while you await from a response from httpbin.org
    async with httpx.AsyncClient() as client:
        ret = await client.get("https://httpbin.org/get")

    return ret.status_code


@async_batch_processor(record_handler=async_record_handler, processor=processor)
def lambda_handler(event, context: LambdaContext):
    return processor.response()
