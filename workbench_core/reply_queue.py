"""Bounded per-platform/account/customer reply jobs with a quiet window."""
import asyncio
import time
from collections import deque


class ReplyQueue:
    def __init__(self,callback,*,quiet_seconds=2.5,concurrency=2,max_pending=256):
        self.callback=callback;self.quiet=quiet_seconds;self.limit=max_pending
        self.semaphore=asyncio.Semaphore(concurrency);self.slots={};self.closed=False
        self.failures=deque(maxlen=100)

    def submit(self,key,payload):
        if self.closed:return False
        if not isinstance(key,tuple) or len(key)!=3:raise ValueError('platform_account_conversation_key_required')
        slot=self.slots.get(key)
        if slot:
            slot.update(payload=payload,changed=time.monotonic(),version=slot['version']+1)
            return True
        if len(self.slots)>=self.limit:return False
        slot={'payload':payload,'changed':time.monotonic(),'version':1}
        self.slots[key]=slot;slot['task']=asyncio.create_task(self._run(key,slot))
        return True

    async def _run(self,key,slot):
        try:
            while not self.closed:
                await asyncio.sleep(max(0,self.quiet-(time.monotonic()-slot['changed'])))
                if time.monotonic()-slot['changed']<self.quiet:continue
                version=slot['version']
                async with self.semaphore:
                    if version!=slot['version']:continue
                    await self.callback(key,slot['payload'])
                if version==slot['version']:break
        except Exception as exc:
            self.failures.append({'key':key,'error_type':type(exc).__name__})
        finally:
            if self.slots.get(key) is slot:self.slots.pop(key,None)

    async def close(self):
        self.closed=True
        tasks=[s['task'] for s in self.slots.values()]
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.slots.clear()
