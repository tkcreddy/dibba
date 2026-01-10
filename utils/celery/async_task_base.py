"""
AsyncTask base class for Celery tasks that use asyncio.

This allows Celery tasks to use async/await for non-blocking I/O operations,
particularly useful for health checks that make multiple network calls.

Based on the user's original pattern: asyncio.run() to execute async code in Celery tasks.
"""
import asyncio
from celery import Task
from typing import Any
import threading


class AsyncTask(Task):
    """Base class for Celery tasks that need to run async code.
    
    This wraps the task execution in asyncio.run() to allow async/await syntax
    within Celery tasks. Handles cases where an event loop might already exist
    by running in a separate thread with its own event loop.
    
    Based on the user's original pattern: asyncio.run() but with thread fallback
    for cases where there's already a running event loop.
    """
    
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Execute the async task by running it in an asyncio event loop.
        
        Uses asyncio.run() which creates a new event loop (user's original pattern).
        If that fails due to an existing loop, runs in a new thread with its own loop.
        """
        try:
            # Try asyncio.run() first - creates new loop (user's original pattern)
            return asyncio.run(self.run(*args, **kwargs))
        except RuntimeError as e:
            # RuntimeError occurs when there's already an event loop running
            # This can happen if Celery worker has async context
            # Fallback: Run in a new thread with its own event loop
            if "asyncio.run() cannot be called from a running event loop" in str(e) or \
               "This event loop is already running" in str(e):
                result_container = {'result': None, 'exception': None}
                
                def run_in_thread():
                    """Run async function in this thread's event loop."""
                    try:
                        # Create a new event loop in this thread
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            result_container['result'] = loop.run_until_complete(self.run(*args, **kwargs))
                        finally:
                            loop.close()
                    except Exception as ex:
                        result_container['exception'] = ex
                
                # Run in a separate thread with its own event loop
                thread = threading.Thread(target=run_in_thread)
                thread.start()
                thread.join()
                
                if result_container['exception']:
                    raise result_container['exception']
                return result_container['result']
            else:
                # Some other RuntimeError - re-raise it
                raise

