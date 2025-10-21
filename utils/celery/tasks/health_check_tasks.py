import httpx
import aiohttp
import asyncio
from utils.celery.celery_config import celery_app
from celery import shared_task,Task
from datetime import datetime
from time import time
from utils.redis.hc_track import HcTrack

from logpkg.log_kcld import LogKCld, log_to_file
from utils.redis.hc_failure_tracker import HcFailureTracker
from utils.redis.redis_interface import RedisInterface

logger = LogKCld()

class AsyncTask(Task):
    def __call__(self, *args, **kwargs):
        # Run the asyncio loop within the Celery task
        return asyncio.run(self.run(*args, **kwargs))


@celery_app.task(base=AsyncTask)
@log_to_file(logger)
async def health_check_task(cluster_name:str, max_concurrency=100):
    ht = HcTrack()
    rd=RedisInterface()
    async def health_check(url, session):
        ct = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        status=''
        try:
            #current_time:float=time()
            logger.info(f"Checking: {url}")
            async with session.get(url, timeout=5,allow_redirects=False,verify_ssl=False) as response:
                print(response.status)
                if response.status == 200:
                    status='healthy'
                    ht.track_consecutive_failures(url, status, cluster_name)
                else:
                    status='unhealthy'
                    ht.track_consecutive_failures(url,status,cluster_name)
                logger.info(f"current time: {ct}, url: {url}, status: {status},status_code: {response.status}")

                return {'current time': ct, 'url': url, 'status': status, 'status_code': response.status}
        except Exception as exc:
            ht.track_consecutive_failures(url, status, cluster_name)
            logger.info(f"current time: {ct}, url: {url}, status: 600,status_code: unhealthy error: {str(exc)}")
            return {'url': url, 'status': 'unhealthy', 'status_code': None, 'error': str(exc)}

    async def health_check_limited(url, session, semaphore):
        async with semaphore:
            return await health_check(url, session)

    async def check_all_urls():
        semaphore = asyncio.Semaphore(max_concurrency)
        results = []
        async with aiohttp.ClientSession() as session:
            tasks = [health_check_limited(url, session, semaphore) for url in rd.get_url_cluster(cluster_name)]
            for task in asyncio.as_completed(tasks):
                results.append(await task)
        return results

    # Run the async function with concurrency control
    return await check_all_urls()









