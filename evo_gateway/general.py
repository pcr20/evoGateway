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

import re

import time, datetime

import json
import re
from collections import namedtuple, deque

import os.path

from .config import *

# --- General Functions
def sig_handler(signum, frame):              # Trap Ctl C
    print("{} Tidying up and exiting...".format(datetime.datetime.now().strftime("%Y-%m-%d %X")))
    # display_and_log("Tidying up and exiting...")
    logfile.close()
    for port_id, port in serial_ports.items():
      if port["connection"].is_open:
        print("Closing port '{}'".format(port["connection"].port))
        port["connection"].close()


def rotate_files(base_file_name):
  if os.path.isfile("{}.{}".format(base_file_name, MAX_LOG_HISTORY)):
    uos.remove(base_file_name + "." + str(MAX_LOG_HISTORY))

  i = MAX_LOG_HISTORY - 1
  while i >= 0:
    if i>1:
        org_file_ext = "." + str(i)
    else:
        org_file_ext =""
    if os.path.isfile(base_file_name + org_file_ext):
        uos.rename(base_file_name + org_file_ext, base_file_name + "." + str(i + 1))
    i -= 1

def to_snake(name):
  #simplying the implementation to just replacing spaces with underscores and making all lower case
  name=name.strip().replace("'","").replace(" ","_")
  #s1 = _first_cap_re.sub(r'\1_\2', name)
  #s2 = _all_cap_re.sub(r'\1_\2', s1).lower()
  #return s2.replace("__","_")
  return name.replace("__", "_").lower()


def to_camel_case(s):
  #simpliflying implementation to do nothing
  #return re.sub(r'(?!^) ([a-zA-Z])', lambda m: m.group(1).upper(), s)
  return s


def get_dtm_from_packed_hex(dtm_hex):
  dtm_mins = int(dtm_hex[0:2],16)
  dtm_hours = int(dtm_hex[2:4],16)
  dtm_day = int(dtm_hex[4:6],16)
  dtm_month = int(dtm_hex[6:8],16)
  dtm_year = int(dtm_hex[8:12],16)
  return datetime.datetime(year=dtm_year,month=dtm_month, day=dtm_day,hour=dtm_hours,minute=dtm_mins)


def display_data_row(msg, display_text, ref_zone=-1, suffix_text=""):
  destination = "BROADCAST MESSAGE" if msg.is_broadcast() else msg.destination_name
  if ref_zone > -1:
    zone_name = "@ {:<20}".format(zones[ref_zone]) if ref_zone in zones else "                      "
    display_row = "{:<2}| {:<22} -> {:<22} | {:>5} {:<25} [Zone {:<3}] {}".format(
        msg.msg_type, msg.source_name, destination, display_text, zone_name, ref_zone, suffix_text)
  else:
    display_row = "{:<2}| {:<22} -> {:<22} | {:>5} {}".format(msg.msg_type, msg.source_name, destination, display_text, suffix_text)
  display_and_log(msg.command_name, display_row, msg.port, msg.rssi)


def display_and_log(source="-", display_message="", port_tag=None, rssi=None):
  try:
    global eventfile
    if os.path.getsize(EVENTS_FILE) > 5000000:
        eventfile.close()
        rotate_files(EVENTS_FILE)
        eventfile = open(EVENTS_FILE,"a")
    port_rssi = "{}/{:3s}".format(port_tag, rssi if rssi else " - ") if port_tag else ""
    row = "{} |{:<5}| {:<20}| {}".format(datetime.datetime.now().strftime("%Y-%m-%d %X"), port_rssi, source, display_message)
    row = "{:<{min_row_width}}".format(row, min_row_width=MIN_ROW_LENGTH)
    print (row)
    # print   (datetime.datetime.now().strftime("%Y-%m-%d %X") + ": " + "{:<20}".format(str(source)) + ": " + str(display_message))
    eventfile.write(row + "\n")
    eventfile.flush()
  except Exception as e:
    print (str(e))



def log(logentry, port_tag="-"):
  global logfile
  if os.path.getsize(LOG_FILE) > 10000000:
        logfile.close()
        rotate_files(LOG_FILE)
        logfile = open(LOG_FILE,"a")

  logfile.write("{} |{}| {}\n".format(datetime.datetime.now().strftime("%Y-%m-%d %X"), port_tag, logentry.rstrip()))
  logfile.flush()


def parity(x):
    shiftamount = 1
    while x >> shiftamount:
        x ^= x >> shiftamount
        shiftamount <<= 1
    return x & 1


def convert_from_twos_comp(hex_val, divisor=100):
  """ Converts hex string of 2's complement """
  try:
    val = float(int(hex_val[0:2],16) << 8 | int(hex_val[2:4],16))/divisor
    return val
  except Exception as e:
    display_and_log("ERROR","Two's complement error {}. hex_val argument: {}".format(e, hex_val))


# Init com ports
def init_com_ports():
  serial_ports = {}
  if len(COM_PORTS) > 0:
    count = 1
    for port, params in COM_PORTS.items():
      limit = params["retry_limit"] if "retry_limit" in params else 3
      serial_port = None
      while (limit > 0) and serial_port is None:
        try:
          serial_port = serial.Serial(port)
          serial_port.baudrate = params["baud"] if "baud" in params else 115200
          serial_port.bytesize = 8
          serial_port.parity   = 'N'
          serial_port.stopbits = 1
          serial_port.timeout = 1

          break
        except Exception as e:
          if limit > 1:
              display_and_log("COM_PORT ERROR",repr(e) + ". Retrying in 5 seconds")
              time.sleep(5)
              limit -= 1
          else:
              display_and_log("COM_PORT ERROR","Error connecting to COM port {}. Giving up...".format(params["com_port"]))

      if serial_port is not None:
        serial_port.tag = count
        serial_ports[port] = {"connection": serial_port, "parameters" : params, "tag": count}
        display_and_log(SYSTEM_MSG_TAG,"{}: Connected to serial port {}".format(serial_ports[port]["tag"], port))
        count +=1
  return serial_ports

def reset_com_ports():
  if len(serial_ports) > 1:
    display_and_log(SYSTEM_MSG_TAG,"Resetting serial port connections")
  # if port is changed for a given serial_port, the serial_port is closed/reopened as per pySerial docs
  for port_id, port in serial_ports.items():
      if port["connection"]:
        display_and_log(SYSTEM_MSG_TAG,"Resetting port '{}'".format(port["connection"].port))
        # port["connection"].port = port["connection"].port
        port["connection"].close()
        time.sleep(2)
        port["connection"].open()
  display_and_log(SYSTEM_MSG_TAG,"Serial ports have been reset")