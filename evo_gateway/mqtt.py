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


from umqtt.simple import MQTTClient
import re

import time#, datetime

import json
import re
from evo_gateway.config import SYSTEM_MSG_TAG

from evo_gateway.config import MQTT_USER
from evo_gateway.config import MQTT_PW
from evo_gateway.config import MQTT_SERVER
from evo_gateway.config import MQTT_SUB_TOPIC
from evo_gateway.config import MQTT_PUB_TOPIC

from evo_gateway.config import SYS_CONFIG_COMMAND
from evo_gateway.config import COMMAND_RESEND_ATTEMPTS
from evo_gateway.config import AUTO_RESET_PORTS_ON_FAILURE
from evo_gateway.config import RESET_COM_PORTS
from evo_gateway.config import CANCEL_SEND_COMMANDS
import evo_gateway.globalcfg as gcfg

from evo_gateway.general import display_and_log
from evo_gateway.general import log

import _thread

class MQTTClient_threaded(MQTTClient):
    is_connected = False
    thread_id=None
    on_log=None
    on_message = None
    on_connect = None

    def __init__(self,*args, **kwargs):
        super().__init__(*args, **kwargs)
    def _mainloop(self):
        while True:
            self.wait_msg()
    def loop_start(self):
        self.thread_id=_thread.start_new_thread(self._mainloop, tuple())
    def loop_stop(self):
        self.thread_id.exit()
    def set_callback(self,callbackfunction):
        self.on_message=callbackfunction
        super().set_callback(callbackfunction)

# --- MQTT Functions -
def initialise_mqtt_client(mqtt_client):
  ''' Initalise the mqtt client object '''
  if not MQTT_SERVER:
    display_and_log (SYSTEM_MSG_TAG,"No MQTT broker specified. MQTT will be ignored")
    return

  if MQTT_USER:
    mqtt_client.username_pw_set(MQTT_USER, MQTT_PW)
  try:
    mqtt_client.on_connect = mqtt_on_connect
    mqtt_client.set_callback(mqtt_on_message)
    mqtt_client.on_log = mqtt_on_log
    mqtt_client.is_connected = False # Custom attribute so that we can track connection status

    display_and_log (SYSTEM_MSG_TAG,"Connecting to mqtt broker '%s'" % MQTT_SERVER)
    #r=mqtt_client.connect(MQTT_SERVER, port=1883, keepalive=60, bind_address="")
    r = mqtt_client.connect()
    mqtt_on_connect(mqtt_client,"","",r)
    mqtt_client.loop_start()

  except Exception as e:
    display_and_log ("ERROR", "'{}' on line {} [Command {}, data: '{}', port: {}]".format(str(e), "sys.exc_info not implemented", mqtt_client, "data", "port_tag"))
    sys.print_exception(e)
    #print(traceback.format_exc())
    return None


def mqtt_on_connect(mqtt_client, userdata, flags, rc):
  ''' mqtt connection event processing '''
  if rc == 0:
      mqtt_client.is_connected = True #set flag
      display_and_log (SYSTEM_MSG_TAG,"MQTT connection established with broker")
      try:
        display_and_log (SYSTEM_MSG_TAG,"Subscribing to mqtt topic '%s'" % MQTT_SUB_TOPIC)
        mqtt_client.subscribe(MQTT_SUB_TOPIC)
      except Exception as e:
        display_and_log ("ERROR", "'{}' on line {} [Command {}, data: '{}', port: {}]".format(str(e), "sys.exc_info not implemented", mqtt_client, userdata, "port_tag"))
        #print(traceback.format_exc())
        sys.print_exception(e)
        return None
  else:
      mqtt_client.is_connected = False
      display_and_log (SYSTEM_MSG_TAG,"MQTT connection failed (code {})".format(rc))
      if DEBUG:
          display_and_log(SYSTEM_MSG_TAG, "[DEBUG] mqtt userdata: {}, flags: {}, client: {}".format(userdata, flags, mqtt_client))


def mqtt_on_log(client, obj, level, string):
    ''' mqtt log event received '''
    if DEBUG:
        display_and_log(SYSTEM_MSG_TAG, "[DEBUG] MQTT log message received. Client: {}, obj: {}, level: {}".format(client, obj, level))
    display_and_log(SYSTEM_MSG_TAG, "[DEBUG] MQTT log msg: {}".format(string))


def mqtt_on_message(topic, msg):
  ''' mqtt message received on subscribed topic '''
  # print(msg.payload)
  #print(msg)

  try:
    json_data = json.loads(str(msg, "utf-8"))
    #print(json_data)
    log("{: <18} {}".format("MQTT_SUB", json_data))

    if SYS_CONFIG_COMMAND in json_data:
      if json_data[SYS_CONFIG_COMMAND] in RESET_COM_PORTS:
       new_command = get_reset_serialports_command()
       new_command.instruction = json.dumps(json_data)
      elif json_data[SYS_CONFIG_COMMAND] == CANCEL_SEND_COMMANDS:
        gcfg.send_queue = []
        gcfg.last_sent_command = None
        display_and_log(SYSTEM_MSG_TAG, "Cancelled all queued outbound commands")
        return
      else:
        display_and_log(SYSTEM_MSG_TAG, "System configuration command '{}' not recognised".format(json_data[SYS_CONFIG_COMMAND]))
        return
    else:
      new_command = get_command_from_mqtt_json(json_data)

    gcfg.send_queue.append(new_command)
  except Exception as e:
    log("{: <18} {} msg: {}".format("MQTT_SUB", e,msg))
    return


def get_command_from_mqtt_json(json_data):
  ''' Extract command from the mqtt json payload '''

  command_name = json_data["command"] if "command" in json_data else None
  command_code = json_data["command_code"] if "command_code" in json_data else None
  if command_code:
    if type(command_code) is int:
      command_code = hex(command_code)
    command_code = command_code.upper().replace("0X","")
  if command_name or command_code:
      args = json_data["arguments"] if "arguments" in json_data else ""
      send_mode = json_data["send_mode"] if "send_mode" in json_data else None
  
  new_command = Command(command_code=command_code, command_name=command_name, args=args, send_mode=send_mode, instruction=json.dumps(json_data))    
  new_command.wait_for_ack = json_data["wait_for_ack"] if "wait_for_ack" in json_data else COMMAND_RESEND_ATTEMPTS > 0
  new_command.reset_ports_on_fail = json_data["reset_ports_on_fail"] if "reset_ports_on_fail" in json_data else AUTO_RESET_PORTS_ON_FAILURE

  return new_command


def mqtt_publish(device, command, msg, topic=None, auto_ts=True):
  if not mqtt_client:
    return

  if not mqtt_client.is_connected:
    display_and_log(SYSTEM_MSG_TAG,"[WARN] MQTT publish failed as client is not connected to broker")
    return

  try:
      if not topic:
        topic = "{}/{}/{}".format(MQTT_PUB_TOPIC, to_snake(device), command.strip())
      #timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%XZ")
      t = time.gmtime()
      timestamp = "{:04d}-{:02d}-{:02d}T{:02d}:{:02d}:{:02d}Z".format(t[0], t[1], t[2], t[3], t[4], t[5])
      mqtt_client.publish(topic, msg, 0, True)
      if auto_ts:
        mqtt_client.publish("{}{}".format(topic,"_ts"), timestamp, 0, True)
      # print("published to mqtt topic {}: {}".format(topic, msg))
  except Exception as e:
      print(str(e))
      pass


def mqtt_init_homeassistant():
    # WIP....
    # Treat each zone as a HA 'device' with unique_id = zone number, and device name = zone name
    # HA component structure:
    # 1. Heating/DHW zone:
    #   - hvac 
    #     |- action_topic: 'heating' or 'off' (possibly 'idle') (heat demand > 0?)
    #     |- modes: current evohome schedule mode; allowed HA options "auto", "off", "heat"
    #     |- current_temperature_topic: evohome zone temperature
    #     |- temperature_command_topic: zone setpoint 
    #     |- temperature_state_topic: this monitors zone setpoint target as reported by the controller i.e. our setpoint_CTL temperatures
    #     |- away_mode_state_topic: as we can't set away mode in modes, may need to use this
    #     |- min_temp, max_temp, temp_step: min/max/step for the zone
    #     |- device, unique_id: use this for the evohome zone; only one hvac device allowed per unique_id
    #   - sensor (non relays, e.g. HR91 TRVs, Thermostats etc):
    #     |- zone level heat demand 
    #     |- <zone_individual_device>_temperature (e.g. TRV)
    #     |- <zone_individual_device>_heat_demand
    #     |- <zone_individual_device>_window_status
    #     |- <zone_individual_device>_battery
    #     |- <zone_individual_device>_setpoint_override
    # 2. BDR Relays, UFH controller:
    #     - sensor
    #     |- actuator_status
    #     |- actuator_status_ts
    #     |- heat_demand
    #     |- heat_demand_ts   
    # 3. Controller:
    #     - sensor
    #     |- command
    #     |- sent_command
    #     |- sent_command_ts
    #     |- sent_command_ack
    #     |- sent_command_ack_ts
    #     |- sent_command_failed
    #     |- sent_command_ack_ts
    #     |- send_command_last_retry_ts
    pass

