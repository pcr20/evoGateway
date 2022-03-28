# -*- coding: utf-8 -*-
#!/usr/bin/python
# evohome Listener/Sender
# Copyright (c) 2020 SMAR info@smar.co.uk
#
# Tested with Python 3.6.8 Requires:
# - pyserial (python -m pip install pyserial)
# - paho (pip install paho-mqtt)
#
# Simple evohome 'listener' and 'sender', for listening in and sending messages between evohome devices using an arudino + CC1101 radio receiver
# (other hardware options also possible - see credits below).
# Messages are interpreted and then posted to an mqtt broker if an MQTT broker is defined in the configuration. Similary, sending commands over the
# radio network are initiated via an mqtt 'send' topic, and 'send' status updates posted back to an mqtt topic.
#
# CREDITS:
# Code here is significntly based on the Domitcz source, specifically the EvohomeRadio.cpp file, by
# fulltalgoRythm - https://github.com/domoticz/domoticz/blob/development/hardware/EvohomeRadio.cpp
# Also see http://www.automatedhome.co.uk/vbulletin/showthread.php?5085-My-HGI80-equivalent-Domoticz-setup-without-HGI80
# for info and discussions on homebrew hardware options.
#
# Details on the evohome protocol can be found here: https://github.com/Evsdd/The-Evohome-Protocol/wiki
#
# The arduino nano I am using is running a firmware modded by ghoti57 available
# from https://github.com/ghoti57/evofw2, who had forked it from
# codeaholics, https://github.com/Evsdd, who in turn had forked it  from
# fulltalgoRythm's orignal firmware, https://github.com/fullTalgoRythm/EvohomeWirelessFW.
#
# OpenTherm protocol decoding taken from https://github.com/Evsdd/The-Evohome-Protocol/wiki/3220:-OpenTherm-Message
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.


import os,sys,uos


#import paho.mqtt.client as mqtt
from evo_gateway.mqtt import MQTTClient_threaded
import re
#import serial

import time#, datetime
#import signal
import json
import re
from collections import namedtuple, deque
#from enum import Enum, IntEnum
import os.path

if  os.path.isdir(sys.argv[0]):
    os.chdir(os.path.dirname(sys.argv[0]))


import evo_gateway
from evo_gateway.config import LOG_FILE
from evo_gateway.config import EVENTS_FILE
from evo_gateway.config import VERSION
from evo_gateway.config import DEVICES_FILE
from evo_gateway.config import LOG_DROPPED_PACKETS
from evo_gateway.config import DROP_DUPLICATE_MESSAGES
from evo_gateway.config import THIS_GATEWAY_ID
from evo_gateway.config import THIS_GATEWAY_NAME
from evo_gateway.config import MAX_HISTORY_STACK_LENGTH

from evo_gateway.config import MQTT_SERVER
from evo_gateway.config import MQTT_CLIENTID

import evo_gateway.globalcfg as gcfg #for eventfile and logfile

from evo_gateway.general import rotate_files
from evo_gateway.general import display_and_log
from evo_gateway.general import log
from evo_gateway.general import init_com_ports

from evo_gateway.mqtt import initialise_mqtt_client

from evo_gateway.app import get_message_from_data
from evo_gateway.app import process_received_message

# --- Main
rotate_files(LOG_FILE)
rotate_files(EVENTS_FILE)
gcfg.logfile = open(LOG_FILE, "a")

gcfg.eventfile = open(EVENTS_FILE,"a")


#signal.signal(signal.SIGINT, sig_handler)    # Trap CTL-C etc

# display_and_log("","\n")
display_and_log("","evohome Listener/Sender Gateway version " + VERSION )

serial_ports = init_com_ports() # global var serial_ports also changed in reset_com_ports()
if len(serial_ports) == 0:
  print("Serial port(s) parameters not found. Exiting...")
  sys.exit()

gcfg.logfile.write("")
gcfg.logfile.write("-----------------------------------------------------------\n")

if os.path.isfile(DEVICES_FILE):
  with open(DEVICES_FILE, 'r') as fp:
    gcfg.devices = json.load(fp)             # Get a list of known devices, ideally with their zone details etc
else:
  gcfg.devices = {}
# Add this server/gateway as a device, but using dummy zone ID for now
gcfg.devices[THIS_GATEWAY_ID] = { "name" : THIS_GATEWAY_NAME, "zoneId": 240, "zoneMaster": True }

zones = {}                            # Create a seperate collection of Zones, so that we can look up zone names quickly
gcfg.send_queue = []
send_queue_size_displayed = 0         # Used to track if we've shown the queue size recently or not

for d in gcfg.devices:
  if gcfg.devices[d]['zoneMaster']:
    zones[gcfg.devices[d]["zoneId"]] = gcfg.devices[d]["name"]
  # generate the mqtt topic for the device (using Homie convention)

display_and_log('','')
display_and_log('','-----------------------------------------------------------')
display_and_log('',"Devices loaded from '" + DEVICES_FILE + "' file:")
for key in sorted(gcfg.devices):
  zm = " [Master]" if gcfg.devices[key]['zoneMaster'] else ""
  display_and_log('','   ' + key + " - " + '{0: <22}'.format(gcfg.devices[key]['name']) + " - Zone " + '{0: <3}'.format(gcfg.devices[key]["zoneId"]) + zm )

display_and_log('','-----------------------------------------------------------')
display_and_log('','')
display_and_log('','Listening...')

gcfg.logfile.flush()

# init MQTT
if MQTT_SERVER:
  mqtt_client = MQTTClient_threaded(MQTT_CLIENTID, MQTT_SERVER)
  initialise_mqtt_client(mqtt_client)
else:
  mqtt_client = None

prev_data_had_errors = False


data_row_stack = deque()
gcfg.last_sent_command = None
ports_open = any(port["connection"] for port_id, port in serial_ports.items()) # ports_open var also updated in fn close_com_ports

# Main loop
while ports_open:
  try:
    for port_id, port in serial_ports.items():
      serial_port = port["connection"]
      if serial_port:

        # Check if last command needs to be resent
        if gcfg.last_sent_command and gcfg.last_sent_command.wait_for_ack and not gcfg.last_sent_command.send_failed and not gcfg.last_sent_command.send_acknowledged:
          check_previous_command_sent(gcfg.last_sent_command)

        # Process any unsent commands waiting to be sent only if we don't have any pending last_sent_command
        if gcfg.send_queue and "is_send_port" in port["parameters"] and port["parameters"]["is_send_port"]:
          if not gcfg.last_sent_command or not gcfg.last_sent_command.wait_for_ack or gcfg.last_sent_command.send_acknowledged or gcfg.last_sent_command.send_failed:
            new_command = gcfg.send_queue.pop()
            new_command.serial_port = serial_port
            gcfg.last_sent_command = process_send_command(new_command)
            if gcfg.send_queue and len(gcfg.send_queue) != send_queue_size_displayed:
              display_and_log("DEBUG","{} command(s) queued for sending to controller".format(len(gcfg.send_queue)))
          elif len(gcfg.send_queue) != send_queue_size_displayed:
            # print("last_send_command: wait for ack: {}".format(gcfg.last_sent_command.wait_for_ack))
            display_and_log("DEBUG","{} command(s) queued and held, pending acknowledgement of '{}' command previously sent".format(len(send_queue), gcfg.last_sent_command.command_name))
          send_queue_size_displayed = len(gcfg.send_queue)

        # Now check for incoming...

        if serial_port.any() > 0:
          data_row = str(serial_port.readline().strip(), "utf-8")
          if data_row:
            msg = get_message_from_data(data_row)
            stack_entry = msg.get_raw_msg_with_ts() if msg else None
            is_duplicate = stack_entry and stack_entry in data_row_stack
            # Make sure it is not a duplicate message (e.g. received through additional listener/gateway devices)
            if msg and not (DROP_DUPLICATE_MESSAGES and is_duplicate) :
              msg = process_received_message(msg)
              if msg:
                # Check if the received message is acknowledgement of previously sent command
                if gcfg.last_sent_command and msg.source == gcfg.last_sent_command.destination and msg.destination == THIS_GATEWAY_ID:
                  # display_and_log("Previously sent command '{}' acknowledged".format(gcfg.last_sent_command.command_name), msg.source)
                  mqtt_publish("","command_sent_failed",False,"{}/failed".format(SENT_COMMAND_TOPIC))
                  mqtt_publish("", "", True, "{}/ack".format(SENT_COMMAND_TOPIC))
                  gcfg.last_sent_command.send_acknowledged = True
                  #gcfg.last_sent_command.send_acknowledged_dtm = datetime.datetime.now()
                  gcfg.last_sent_command.send_acknowledged_dtm = time.gmtime()
                  display_and_log("COMMAND_OUT","{} {} Command ACKNOWLEDGED".format(gcfg.last_sent_command.command_name.upper(),
                    gcfg.last_sent_command.arg_desc if gcfg.last_sent_command.arg_desc != "[]" else ":"))
                prev_data_had_errors = False
              else:
                  if not prev_data_had_errors and LOG_DROPPED_PACKETS:
                      prev_data_had_errors = True
                      display_and_log("ERROR","--- Message dropped: packet error from hardware/firmware")
                  log("{: <17}{} {}".format("", "^" if is_duplicate else " " , data_row))
              gcfg.logfile.flush()
              data_row_stack.append(stack_entry)
              if len(data_row_stack) > MAX_HISTORY_STACK_LENGTH:
                data_row_stack.popleft()
            else: # Log msg anyway
              log("{: <17}{} {}".format("ERR: INVALID MSG" if not msg else "", "^" if is_duplicate else " " , data_row))

      time.sleep(0.01)
    ports_open = any(port["connection"] for port_id, port in list(serial_ports.items()))
  except KeyboardInterrupt:
    for port_id, port in serial_ports.items():
      if port["connection"]:
        print("Closing port '{}'".format(port["connection"]))
        port["connection"].deinit()
  except Exception as e:
    display_and_log("ERROR "+__file__, "Other exception occured")
    sys.print_exception(e)

    # comConnected = False

if mqtt_client:
  mqtt_client.loop_stop()
print("Session ended\n")
