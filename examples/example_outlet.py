import asyncio
from xknx import XKNX, Switch

async def main():
    xknx = XKNX()
    await xknx.start()
    switch = Switch(xknx,
                    name='TestOutlet',
                    group_address='1/1/11')
    switch.set_on()
    await asyncio.sleep(2)
    switch.set_off()
    await xknx.stop()

# pylint: disable=invalid-name
loop = asyncio.get_event_loop()
loop.run_until_complete(main())
loop.close()