import worker.settings as settings
import logging as log
import asyncio
import aiohttp
import aredis
import boto3
import botocore.client
from functools import partial
from timeit import default_timer as timer
from worker.util import kafka_connect, parse_message, save_thumbnail_s3, \
    process_image, monitor_task_list
from worker.rate_limit import RateLimitedClientSession
from worker.stats_reporting import StatsManager


def poll_consumer(consumer, batch_size):
    """
    Poll the Kafka consumer for a batch of messages and parse them.
    :param consumer:
    :param batch_size: The number of events to return from the queue.
    :return:
    """
    batch = []
    # Consume messages until either batch_size has been reached or the max
    # wait time has occurred.
    max_wait_seconds = 3
    elapsed_time = 0
    last_msg_time = timer()
    msg_count = 0
    while msg_count < batch_size and elapsed_time < max_wait_seconds:
        message = consumer.consume(block=False)
        if message:
            parsed = parse_message(message)
            batch.append(parsed)
            last_msg_time = timer()
            msg_count += 1
        elapsed_time = timer() - last_msg_time
    return batch


async def consume(consumer, image_processor, terminate=False):
    """
    Listen for inbound image URLs and process them.
    :param consumer: A Kafka consumer listening to the inbound images topic.
    :param image_processor: A partial function that handles an image.
    :param terminate: Whether to terminate when there are no more messages.
    """
    total = 0
    # Limit concurrent execution of resize tasks
    semaphore = asyncio.BoundedSemaphore(settings.BATCH_SIZE)
    scheduled = []
    asyncio.create_task(monitor_task_list(scheduled))
    while True:
        num_tasks_running = sum([not t.done() for t in scheduled])
        num_to_schedule = settings.SCHEDULE_SIZE - num_tasks_running
        if num_to_schedule:
            start = timer()
            messages = poll_consumer(consumer, num_to_schedule)
            # Schedule resizing tasks
            if messages:
                log.info(f'Scheduling {len(messages)} additional tasks.')
                batch_size = len(messages)
                total += batch_size
                for msg in messages:
                    await semaphore.acquire()
                    t = asyncio.create_task(
                       image_processor(
                           url=msg['url'],
                           identifier=msg['uuid'],
                           source=msg['source']
                       )
                    )
                    scheduled.append(t)
                total_time = timer() - start
                log.info(f'event_processing_rate={batch_size/total_time}/s')
                consumer.commit_offsets()
            else:
                if terminate:
                    await asyncio.gather(*scheduled)
                    return
                await asyncio.sleep(30)


async def setup_consumer():
    """
    Set up all IO used by the consumer.
    """
    kafka_client = kafka_connect()
    s3 = boto3.client(
        's3',
        settings.AWS_DEFAULT_REGION,
        config=botocore.client.Config(max_pool_connections=settings.BATCH_SIZE)
    )
    inbound_images = kafka_client.topics['inbound_images']
    consumer = inbound_images.get_balanced_consumer(
        consumer_group='image_resizers',
        auto_commit_enable=True,
        zookeeper_connect=settings.ZOOKEEPER_HOST
    )

    redis_client = aredis.StrictRedis(host=settings.REDIS_HOST)
    aiosession = RateLimitedClientSession(
        aioclient=aiohttp.ClientSession(),
        redis=redis_client
    )
    stats = StatsManager(redis_client)
    image_processor = partial(
        process_image, session=aiosession,
        persister=partial(save_thumbnail_s3, s3_client=s3),
        stats=stats
    )
    return consume(consumer, image_processor)


async def listen():
    """
    Listen for image events forever.
    """
    consumer = await setup_consumer()
    await consumer

if __name__ == '__main__':
    log.basicConfig(level=log.INFO)
    asyncio.run(listen())
