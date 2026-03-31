import json
import logging
import asyncio
import paho.mqtt.client as mqtt
from logging.handlers import RotatingFileHandler


logger = logging.getLogger(__name__)

handler = RotatingFileHandler(
    "inverterMQTTSync.log",
    maxBytes=1_000_000,   # 1 MB
    backupCount=5         # keep 5 old log files
)

handler.setFormatter("%(asctime)s [%(levelname)-8s] %(message)s")
logger.addHandler(handler)

class InverterMQTTSync:
    """Class to read inverter data from Modbus and publish to MQTT in a format compatible with Home Assistant discovery."""
    def __init__(self, serial_number: str, hw: str, sw: str, mqtt_client: mqtt.Client, schema: dict):
        
        self.serial_number = serial_number
        self.hw = hw
        self.sw = sw
        
        self.mqtt = mqtt_client
        self.schema = schema

        # Append last 6 characters of serial_number to device_id for uniqueness
        self.device_id = schema["device_id"] + str(serial_number)[-6:]
        
        self.registers = schema["registers"]
        #add sn, hw, sw to schema for HA device registry
        self.schema["sn"] = serial_number
        self.schema["hw"] = hw
        self.schema["sw"] = sw        
        
        self.discovery_prefix = "homeassistant"
        self.state_topic = f"{self.discovery_prefix}/device/{self.device_id}/state"

        self.components_topic = schema.get("components_topic", f"{self.device_id}/state")

    # -----------------------------
    # Publish Home Assistant discovery
    # -----------------------------
    async def publish_discovery(self):
        #uid = f"{self.device_id}_{reg['desc']}"
        dev_uid = f"{self.device_id}"
        topic = f"{self.discovery_prefix}/device/{dev_uid}/config"
        payload = {
                "dev": {
                    "ids": dev_uid,
                    "name": self.schema.get("name", "AIO Solar Inverter"),
                    "mf": self.schema.get("mf", "Unknown"),
                    "mdl": self.schema.get("mdl", "Unknown"),
                    "sn": self.schema.get("sn", "Unknown"),
                    "hw": self.schema.get("hw", "Unknown"),
                    "sw": self.schema.get("sw", "Unknown")
                },
                "origin":{
                    "name": "Inverter Pi",
                    "sw": "1.0",
                    "url": "http://192.168.0.184"
                },
                "components":{},
                "state_topic": self.state_topic,
                "qos":2                
        }
        components = {}
        for reg in self.registers:
            if reg.get("ha_device_class", None) is None:
                logger.debug(f"Register {reg['desc']} has no ha_device_class defined; skipping HA discovery for this register.")
            component ={
                    f"{self.device_id}_{reg['desc']}":{
                        "p": "sensor",
                        "unique_id": f"{self.device_id}_{reg['desc']}",
                        "device_class": reg['ha_device_class'],
                        "unit_of_measurement": reg.get('unit', ""),
                        "state_topic": f"{self.components_topic}/{reg['memory_area']}",
                        "state_class": reg['ha_state_class'],
                        "value_template": f"{{{{ value_json.{reg['desc']} }}}}",
                        "name": reg['ha_name']
                    }
                }
            components.update(component)

        payload['components'] = components
        
        if not self.mqtt.is_connected():            
            logger.error("MQTT disconnected — attempting async-safe reconnect...")
            await asyncio.to_thread(self.mqtt.reconnect)

        message = self.mqtt.publish(topic, json.dumps(payload), retain=True, qos=2)
        print("Waiting for MQTT publish to complete...")
        #message.wait_for_publish()
        print(message)
        print(f"Published HA discovery for {reg['desc']}")
    
    # -----------------------------
    # Publish Home Assistant discovery
    # -----------------------------
    
    async def clear_discovery(self):
        for reg in self.registers:
            uid = f"{self.device_id}_{reg['desc']}"
            topic = f"{self.discovery_prefix}/device/{uid}/config"

            payload = {
            }

            self.mqtt.publish(topic, json.dumps(payload), retain=False)
            logger.debug(f"Cleared HA discovery for {reg['desc']}")

    # -----------------------------
    # Read all registers and publish JSON state
    # -----------------------------
    # async def poll_and_publish(self):
    #     state = {}

    #     for reg in self.registers:
    #         addr = int(reg["address"], 16)
    #         length = reg["length"]

    #         raw = await self.modbus.read_registers(addr, count=length, raw=True)
    #         if raw is None:
    #             logger.warning(f"Failed to read {reg['address']}")
    #             continue

    #         value = self.convert_value(reg, raw)
    #         state[reg["desc"]] = value

    #     # Publish JSON state
    #     self.mqtt.publish(self.state_topic, json.dumps(state), retain=True)
    #     logger.info("Published inverter state")
    async def add_to_registers(self, registers: list):
        """Add register data to self.registers list. This allows us to keep the register data in memory so we can publish it to MQTT without having to read from Modbus again."""
        for reg in registers:
            addr = int(reg["Address"], 16)
            length = reg["Length"]

            value = reg.get("Value", None)
            if value is None:
                #logger.error(f"add_to_registers: Failed to read {reg['Address']}")
                continue
            else:
                reg_to_modify = next((r for r in self.registers if r["address"] == addr), None)
                if reg_to_modify is not None:
                    reg_to_modify["value"] = value
                    reg_to_modify["length"] = length
                    reg_to_modify["newdata"] = True
                    logger.debug(f"Added value {value} to register {reg_to_modify['desc']} at address {reg_to_modify['address']}")

    # -----------------------------
    # Read all registers and publish JSON state
    # -----------------------------
    # async def publish(self):
    #     state = {}

    #     for reg in self.registers:
    #         addr = int(reg["address"], 16)
    #         length = reg["length"]

    #         value = reg.get("value", None)
    #         if value is None:
    #             logger.warning(f"Failed to read {reg['address']}")
    #             continue


    #     # Publish JSON state
    #     self.mqtt.publish(self.state_topic, json.dumps(state), retain=True)
    #     logger.info("Published inverter state")

    async def publish_registers(self, addresses: list, memory_area: str, registermap):
        """Publish only registers whose address appears in `addresses`.

        `addresses` may contain hex strings (e.g. "100", "F03C") or integers.
        Comparison is case-insensitive and ignores a leading `0x` if present.
        """

        publish_topic = f"{self.components_topic}/{memory_area}"
        print(f"Publishing to topic {publish_topic} for memory area {memory_area} with addresses: {addresses}")
        if not addresses:
            logger.debug("No addresses provided to publish_registers(); nothing to publish.")
            return

        # Normalize provided addresses to uppercase hex-without-0x (no leading zeros)
        payload = {}
        for addr in addresses:
            reg = next((r for r in self.registers if r["address"] == addr), None)
            mapped = next((r for r in registermap if r["Address"] == addr), None)
            if reg and mapped:
                value = mapped.get("Value", None)
                if value is None:
                    logger.debug(f"publish_registers: Empty value for {reg['address']}")
                    continue
            else:
                logger.debug(f"publish_registers: No register found for address {addr}")
                continue
            
            payload[reg["desc"]] = value

        if not payload:
            logger.debug("No register values available to publish after filtering.")
            return
        
        
        # Publish JSON state
        if not self.mqtt.is_connected():            
            logger.warning("MQTT disconnected — attempting async-safe reconnect...")
            await asyncio.to_thread(self.mqtt.reconnect)
            
        message = self.mqtt.publish(publish_topic, json.dumps(payload, default=str), retain=False, qos=2)
        #message.wait_for_publish()
        #print("message------")
        #print(f"Publishing to {publish_topic}: {json.dumps(payload, default=str)}")
        #print(message)
        #print("message------")
        #logger.info(message)
        logger.info(f"MQTT publish result: {message.rc}")
        logger.info("Published filtered inverter state for %d registers", len(payload))