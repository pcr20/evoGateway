# This file is executed on every boot (including wake-boot from deepsleep) Paul
#import esp
#import webrepl
#esp.osdebug(None)
#webrepl.start()
import ntptime
import sys

time.sleep(0.5)
try: 
    ntptime.settime()
    print("ntptime.settime() success")
except OSError as e:
    sys.print_exception(e)
    time.sleep(2.0)
    try: 
        ntptime.settime()
        print("ntptime.settime() success")
    except OSError as e:
        sys.print_exception(e)

import evogateway


