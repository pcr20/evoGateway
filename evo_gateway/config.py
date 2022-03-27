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
import configparser
import re

import time#, datetime
import json
import re
from collections import namedtuple, deque
import os.path


#---------------------------------------------------------------------------------------------------
VERSION         = "2.1.0"
CONFIG_FILE     = "evogateway.cfg"

# --- Configs/Default
def getConfig(config,section,name,default):
    if config.has_option(section,name):
        return config.get(section,name)
    else:
        return default


# Get any configuration overrides that may be defined in  CONFIG_FILE
# If override not specified, then use the defaults here

config = configparser.RawConfigParser()
config.read(CONFIG_FILE)

# Use json config for multiple com ports if available
COM_PORTS         = getConfig(config,"Serial Port", "COM_PORTS", None)
if COM_PORTS and type(COM_PORTS) == str:
  COM_PORTS = json.loads(COM_PORTS.replace("'", "\""))

# otherwise default to single port
if COM_PORTS is None:
  COM_PORT          = getConfig(config,"Serial Port","COM_PORT","/dev/ttyUSB0")
  COM_BAUD          = int(getConfig(config,"Serial Port","COM_BAUD",115200))
  COM_RETRY_LIMIT   = int(getConfig(config,"Serial Port","COM_RETRY_LIMIT",10))
  COM_TX_PIN = int(getConfig(config, "Serial Port", "COM_TX_PIN", 17))
  COM_RX_PIN = int(getConfig(config, "Serial Port", "COM_RX_PIN", 16))
  COM_PORTS = {COM_PORT: {"baud" : COM_BAUD, "retry_limit": COM_RETRY_LIMIT, "is_send_port": True, "tx_pin": COM_TX_PIN, "rx_pin": COM_RX_PIN}}

EVENTS_FILE       = getConfig(config,"Files", "EVENTS_FILE", "events.log")
LOG_FILE          = getConfig(config,"Files", "LOG_FILE", "evogateway.log")
DEVICES_FILE      = getConfig(config,"Files", "DEVICES_FILE", "devices.json")
NEW_DEVICES_FILE  = getConfig(config,"Files", "NEW_DEVICES_FILE", "devices_new.json")

LOG_DROPPED_PACKETS = getConfig(config,"Other", "LOG_DROPPED_PACKETS", "False").lower() == "true"
DROP_DUPLICATE_MESSAGES = getConfig(config,"Other", "DROP_DUPLICATE_MESSAGES", "True").lower() == "true"

MQTT_SERVER       = getConfig(config,"MQTT", "MQTT_SERVER", "")                  # Leave blank to disable MQTT publishing. Messages will still be saved in the various files
MQTT_SUB_TOPIC    = getConfig(config,"MQTT", "MQTT_SUB_TOPIC", "")               # Note to exclude any trailing '/'
MQTT_PUB_TOPIC    = getConfig(config,"MQTT", "MQTT_PUB_TOPIC", "")
MQTT_USER         = getConfig(config,"MQTT", "MQTT_USER", "")
MQTT_PW           = getConfig(config,"MQTT", "MQTT_PW", "")
MQTT_CLIENTID     = getConfig(config,"MQTT", "MQTT_SERVER", "evoGateway")

CONTROLLER_ID     = getConfig(config,"SENDER", "CONTROLLER_ID", "01:139901")
THIS_GATEWAY_ID   = getConfig(config,"SENDER", "THIS_GATEWAY_ID","18:318170")
THIS_GATEWAY_NAME = getConfig(config,"SENDER", "THIS_GATEWAY_NAME","EvoGateway")
THIS_GATEWAY_TYPE_ID = THIS_GATEWAY_ID.split(":")[0]

COMMAND_RESEND_TIMEOUT_SECS = float(getConfig(config,"SENDER", "COMMAND_RESEND_TIMEOUT_SECS", 60.0))
COMMAND_RESEND_ATTEMPTS= int(getConfig(config,"SENDER", "COMMAND_RESEND_ATTEMPTS", 3))    # A value of zero also disables waiting for sent command acknowledgements
AUTO_RESET_PORTS_ON_FAILURE = getConfig(config,"SENDER", "AUTO_RESET_PORTS_ON_FAILURE", "False").lower() == "true"

MAX_LOG_HISTORY   = int(getConfig(config,"SENDER", "MAX_LOG_HISTORY", 3))
MIN_ROW_LENGTH    = int(getConfig(config,"MISC", "MIN_ROW_LENGTH", 160))

MAX_HISTORY_STACK_LENGTH = 10

EMPTY_DEVICE_ID   = "--:------"

SYS_CONFIG_COMMAND = "sys_config"
RESET_COM_PORTS   = "reset_com_ports"
CANCEL_SEND_COMMANDS ="cancel_commands"

SYSTEM_MSG_TAG = "*"
#----------------------------------------
class TwoWayDict(dict):
    def __len__(self):
        return super().__len__() / 2
    def __setitem__(self, key, value):
        super().__setitem__( key, value)
        super().__setitem__( value, key)

DEVICE_TYPE = TwoWayDict()
DEVICE_TYPE["01"] = "CTL"  # Main evohome touchscreen controller
DEVICE_TYPE["02"] = "UFH"  # Underfloor controller, HCC80R or HCE80
DEVICE_TYPE["03"] = "STAT" # Wireless thermostat -  HCW82
DEVICE_TYPE["04"] = "TRV"  # Radiator TRVs, e.g. HR92
DEVICE_TYPE["07"] = "DHW"  # Hotwater wireless Sender
DEVICE_TYPE["10"] = "OTB"  # OpenTherm Bridge
DEVICE_TYPE["18"] = "CUL"  # This fake HGI80
DEVICE_TYPE["19"] = "CUL"  # Also fake HGI80 - used by evofw2?
DEVICE_TYPE["13"] = "BDR"  # BDR relays
DEVICE_TYPE["30"] = "GWAY" # Mobile Gateway such as RGS100
DEVICE_TYPE["34"] = "STAT" # Wireless round thermostats T87RF2033 or part of Y87RF2024 

OPENTHERM_MSG_TYPES = {
      0: "Read-Data",       # 0b.000....
     16: "Write-Data",      # 0b.001....
     32: "Invalid-Data",    # 0b.010....
     48: "-reserved-",      # 0b.011....
     64: "Read-Ack",        # 0b.100....
     80: "Write-Ack",       # 0b.101....
     96: "Data-Invalid",    # 0b.110....
    112: "Unknown-DataId",  # 0b.111....
}

CONTROLLER_MODES = {0: "Auto", 1: "Heating Off", 2: "Eco-Auto", 3: "Away", 4: "Day Off", 7:"Custom"} # 0=auto, 1= off, 2=eco, 4 = day off, 7 = custom

# --- Classes
class Message():
  ''' Object to hold details of interpreted (received) message. '''
  def __init__(self, rawmsg, has_rssi = False ):    
    offset = 4 if has_rssi else 0                           # new ghoti57 fifo_hang branch has rssi

    self.rawmsg       = rawmsg.strip()
    
    self.rssi         = rawmsg[4:7]  if has_rssi else None    

    self.source_id    = rawmsg[11 + offset: 20 + offset]
    self.msg_type     = rawmsg[ 4 + offset:  6 + offset].strip()
    self.source       = rawmsg[11 + offset: 20 + offset]               # device 1 - This looks as if this is always the source; Note this is overwritten with name of +device
    self.source_type  = rawmsg[11 + offset: 13 + offset]               # the first 2 digits seem to be identifier for type of +device
    self.source_name  = self.source
    self.device2      = rawmsg[21 + offset: 30 + offset]               # device 2 - Requests (RQ), Responses (RP) and Write (W) seem to be sent to device 2 +only
    self.device2_type = rawmsg[21 + offset: 23 + offset]               # device 2 +type
    self.device2_name = self.device2
    self.device3      = rawmsg[31 + offset: 40 + offset]               # device 3 - Information (I) is to device 3. Broadcast messages have device 1 and 3 are the +same
    self.device3_type = rawmsg[31 + offset: 33 + offset]               # device 3 +type
    self.device3_name = self.device3

    if self.device2 == EMPTY_DEVICE_ID:
        self.destination = self.device3
        self.destination_type = self.device3_type
    else:
        self.destination = self.device2
        self.destination_type = self.device2_type
    self.destination_name = self.destination

    # There seem to be some OpenTherm messages without a source/device2; just device3. 
    # Could be firmware issue with source/dest swapped? For now, let's swap over and see...
    if self.source == EMPTY_DEVICE_ID and self.device3 != EMPTY_DEVICE_ID:
      self.source = self.device3
      self.source_type = self.device3_type
      self.device3 = rawmsg[11:20]
      self.devic3_type =rawmsg[11:13]

    self._initialise_device_names()

    self.command_code = rawmsg[41 + offset: 45 + offset].upper()       # command code +hex
    self.command_name = self.command_code                              # needs to be assigned outside, as we are doing all the processing outside of this class/struct
    try:
        self.payload_length = int(rawmsg[46 + offset: 49 + offset])    # Note this is not +HEX...
    except Exception as e:
        print ("Error instantiating Message class on line '{}':  {}. Raw msg: '{}'. length = {}".format(sys.exc_info()[-1].tb_lineno, str(e), rawmsg, +len(rawmsg)))
        self.payload_length = 0

    self.payload      = rawmsg[50 + offset:]
    self.port         = None
    self.failed_decrypt= "_ENC" in rawmsg or "_BAD" in rawmsg or "BAD_" in rawmsg or "ERR" in rawmsg


  def _initialise_device_names(self):
    ''' Substitute device IDs for names if we have them or identify broadcast '''
    try:
        if self.source_type == DEVICE_TYPE['CTL'] and self.is_broadcast():
            self.destination_name = "CONTROLLER"
            self.source_name = "CONTROLLER"
        elif DEVICE_TYPE[self.source_type] and self.source in devices and devices[self.source]['name']:
            self.source_name = "{} {}".format(DEVICE_TYPE[self.source_type], devices[self.source]['name'])      # Get the device's actual name if we have it
        else:
            print("Could not find device type '{}' or name for device '{}'".format(self.source_type, self.source))
        if self.destination_name != "CONTROLLER" and self.destination in devices and devices[self.destination]['name']:
            if self.destination_type == DEVICE_TYPE['CTL']:
                self.destination_name = "CONTROLLER"
            else:
                device_name = devices[self.destination]['name'] if self.destination in devices else self.destination
                self.destination_name = "{} {}".format(DEVICE_TYPE[self.destination_type], devices[self.destination]['name'])      # Get the device's actual name if we have it
    except Exception as e:
        print ("Error initalising device names in Message class instantiation, on line '{}': {}. Raw msg: '{}'. length = {}".format(sys.exc_info()[-1].tb_lineno, str(e), self.rawmsg, len(self.rawmsg)))


  def is_broadcast(self):
    return self.source == self.destination


  def get_raw_msg_with_ts(self, strip_rssi=True):
    t = time.gmtime()
    if self.rssi: # remove the rssi before saving to stack - different controllers may receive the same message but with different signal strengths
      raw = "{}-{}-{} {}:{}:{}: {}".format(t[0], t[1], t[2], t[3], t[4], t[5], "--- {}".format(self.rawmsg[8:]))
      #raw = "{}: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %X"), "--- {}".format(self.rawmsg[8:]))
    else:
      raw = "{}-{}-{} {}:{}:{}: {}".format(t[0], t[1], t[2], t[3], t[4], t[5], self.rawmsg)
      #raw = "{}: {}".format(datetime.datetime.now().strftime("%Y-%m-%d %X"), self.rawmsg)
    return raw


class Command():
  ''' Object to hold details of command sent to evohome controller.'''
  def __init__(self, command_code=None, command_name=None, destination=None, args=None, serial_port=-1, send_mode="I", instruction=None):
    self.command_code = command_code
    self.command_name = command_name
    self.destination = destination
    self.args = args
    self.arg_desc = "[]"
    self.serial_port = serial_port
    self.send_mode = send_mode
    self.send_string = None
    self.send_dtm = None
    self.retry_dtm = None
    self.retries = 0
    self.send_failed = False
    self.wait_for_ack = False
    self.reset_ports_on_fail = True
    self.send_acknowledged = False
    self.send_acknowledged_dtm = None
    self.dev1 = None
    self.dev2 = None
    self.dev3 = None
    self.payload = ""
    self.command_instruction = instruction


  def payload_length(self):
    if self.payload:
      return int(len(self.payload)/2)
    else:
      return 0

