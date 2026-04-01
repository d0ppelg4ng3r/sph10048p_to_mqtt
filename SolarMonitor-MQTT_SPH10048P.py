#!/usr/bin/python

# Tristan - 19th March 2026 - 
#
# Used to gather data from SunGoldPower 10KW (SRNE Rebranded) charge controllers via modbus over USB and 
# publish the data to an MQTT broker.
# The data is published in JSON format with the register address as the key and the value as the value.

import time
import json
import asyncio
import threading
import csv
import os
import atexit
import argparse
import logging
import time
from datetime import datetime
from itertools import count
from pymodbus.pdu.exceptionresponse import ExceptionResponse
from pymodbus.client import AsyncModbusSerialClient
from paho.mqtt import client as mqtt_client
from pymodbus.pdu import ModbusPDU
from async_task_scheduler import AsyncTaskScheduler
from decimal import Decimal
from logging.handlers import RotatingFileHandler

#Custom imports
import modbusconversions as modbusConv
from InverterMQTTSync import InverterMQTTSync

parser = argparse.ArgumentParser()
parser.add_argument("--debug", action="store_true")
args = parser.parse_args()

class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",     # Cyan
        logging.INFO: "\033[32m",      # Green
        logging.WARNING: "\033[33m",   # Yellow
        logging.ERROR: "\033[31m",     # Red
        logging.CRITICAL: "\033[41m",  # Red background
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        message = super().format(record)
        return f"{color}{message}{self.RESET}"

logger = logging.getLogger(__name__)

level=logging.DEBUG if args.debug else logging.INFO
logger.setLevel(level)

handler = RotatingFileHandler(
    "app.log",
    maxBytes=1_000_000,   # 1 MB
    backupCount=5         # keep 5 old log files
)

handler.setFormatter(ColorFormatter("%(asctime)s [%(levelname)-8s] %(message)s"))

logger.addHandler(handler)

handler = logging.StreamHandler()
handler.setFormatter(ColorFormatter("%(asctime)s [%(levelname)-8s] %(message)s"))

logger.addHandler(handler)

logger.debug("Debug message: This will only be shown if --debug flag is set.")
logger.info("INFO: This is an info message.")
logger.warning("WARN: This is a warning message.")
logger.error("ERR: This is an error message.")
logger.critical("CRIT: This is a critical message.")

#Log formatting settings
DESC_WIDTH = 20

#logger helpers
def fmt_desc(desc, width=None):
    if width is None:
        width = DESC_WIDTH
    return f"{desc[:width]:<{width}}"

def load_secrets(path="secrets.json"):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.debug(f"Secrets file {path} not found, using defaults / env vars")
        return {}
    except Exception as e:
        logger.debug(f"Failed to load secrets from {path}: {e}")
        return {}


secrets = load_secrets()



PUBLISH_INTERVAL = 1             # Number of seconds to wait in between publishing the latest data to the console/MQTT broker.
GET_P00_INTERVAL = 120        # Number of seconds to wait in between realtime requests to the charge controller.
GET_P01_INTERVAL = 2             # Seconds between P01 reads (less-frequent static/daily data)
GET_P02_INTERVAL = 300            # Seconds between P02 reads (very infrequent)
JSON_OR_TEXT = "TEXT"             # Output format, either "TEXT" or "JSON".
ENABLE_MQTT = True                   # Set to false to disable MQTT publishing and just print the data to the console.
# Load MQTT credentials from environment variables first, then from secrets.json, then fall back to defaults.
MQTT_PORT = int(os.getenv("MQTT_PORT", secrets.get("MQTT_PORT", 1883)))
MQTT_SERVER_ADDR = os.getenv("MQTT_SERVER_ADDR", secrets.get("MQTT_SERVER_ADDR", "192.168.2.50"))
MQTT_USER = os.getenv("MQTT_USER", secrets.get("MQTT_USER", "CHANGE_ME!!!"))
MQTT_PASS = os.getenv("MQTT_PASS", secrets.get("MQTT_PASS", "CHANGE_ME!!!"))
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", secrets.get("MQTT_CLIENT_ID", "RaspberryPiSolar"))
MQTT_TOPIC_NAME = os.getenv("MQTT_TOPIC_NAME", secrets.get("MQTT_TOPIC_NAME", "CC1"))
#MQTT Client
MQTT_CLIENT = None

InverterMQTTObj = None


#Modbus Settings
MODBUS_PORT = '/dev/ttyUSB0' #Change if using a different serial port on the raspberry pi or other device.
MODBUS_BAUDRATE = 9600
MODBUS_STOPBITS = 1
MODBUS_BYTESIZE = 8
MODBUS_PARITY = 'N'
MODBUS_TIMEOUT = 5
MODBUS_UNIT = 1 # Modbus unit ID of the charge controller, usually 1 but can be 255 in some cases. Check your charge controller manual to be sure.
MODBUS_ADDR_OFFSET = 0 #Change depending on whether the charge controller is in 0x00 or 0x01 mode. This only changes the starting address of the registers, not the register values themselves. See the charge controller manual for more details.
RS485DEBUG_MODE = True # If true, will run through a test communication with the charge controller.
DELAY_BETWEEN_MESSAGES = 0.05 # Short delay to avoid overwhelming the charge controller with requests.

modbusLock = threading.Lock() # Used to ensure thread safety when accessing the modbus client.
memoryLock = threading.Lock() # Used to ensure thread safety when accessing the emulated register arrays.

InverterRegisters = {}  #Used to store the latest received values from the Inverter.

RegisterMap = {}    #Used to store the descriptions of the registers from the JSON file.

registers_productInformationArea = []   # Used to store all registers from the product information area (0x0A - 0x49)
registers_DCDataArea = []   # Used to store all registers from the DC data area (0x100 - 0x11B)
registers_InverterDataArea = [] # Used to store all registers from the inverter data area (0x200 - 0x245)
registers_DeviceControlArea = []    # Used to store all registers from the device control area (0xDF00 - 0xDF0D)
registers_SettingsBatteryArea = []  # Used to store all registers from the battery settings area (0xE000 - 0xE04D)
registers_SettingsFactoryArea = [] # Used to store all registers from the factory settings area (0xE100 - 0xE145)
registers_SettingsUserArea = []    # Used to store all registers from the user settings area (0xE200 - 0xE221)
registers_SettingsGridArea = []    # Used to store all registers from the settings grid area (0xE400 - 0xE43B)
registers_PowerStatsArea = []    # Used to store all registers from the power stats area (0xF000 - 0xF053)
registers_FaultsArea = []    # Used to store all registers from the faults area (0xF800 - 0xFA11)


async def connectModbus():
    global a
    global modbus
    a = await modbus.connect()

    # Retry connection to charge controller if it fails.
    while(not a):
        logger.error("Failed to connect to the Charge Controller, retrying in 5 seconds...")
        time.sleep(5)
        a = await modbus.connect()

#Used to reconnect on controller timeout.
async def reconnectModbus():
   global a
   global modbus
   modbus = AsyncModbusSerialClient(port=MODBUS_PORT, baudrate=MODBUS_BAUDRATE, stopbits=MODBUS_STOPBITS, bytesize=MODBUS_BYTESIZE, parity=MODBUS_PARITY, timeout=MODBUS_TIMEOUT)
   a = await modbus.connect()
    #Retry connection to charge controller if it fails.
   while(not a):
       logger.error("Failed to reconnect to the Charge Controller, retrying in 5 seconds...")
       time.sleep(5)
       a = await modbus.connect()



async def messageDelay():
    await asyncio.sleep(DELAY_BETWEEN_MESSAGES)

async def testModbusConnection():
    global modbus
    logger.info("Attempting to read from the charge controller to test the RS485 connection...")
    # Attempt to read a register from the charge controller to test the connection.
    try:
        modbusData = await modbus.read_holding_registers((int("E200", 16)), count=1, device_id=MODBUS_UNIT)
        logger.debug(f"Successfully read from charge controller at address {hex(0xE200)}: {modbusData.registers}")
    except Exception as e:
        logger.error(f"Failed to connect to the charge controller via Modbus. Error: {e}")

async def readModbusThreadSafe(address, count, device_id) -> ModbusPDU:
    """Helper function to read from modbus in a thread-safe way."""
    global modbus
    
    try:
        with modbusLock:
            await messageDelay() # Short delay to avoid overwhelming the charge controller with requests.           
            response = await modbus.read_holding_registers(address, count=count, device_id=device_id)
            response.address = address # Add the starting address to the response object for reference later when we want to add the data to the InverterRegisters dictionary.
            logger.debug(f"Read from Modbus at address {hex(address)[2:].upper()} count {count}: {response.registers} | Response type: {type(response)}")
            logger.debug(f"Response: {response}")
            #quit() # Temporary quit for debugging, remove this after verifying the response type and values are correct.
            logger.debug(f"ReadThreadSafe from Modbus at address {hex(address)[2:].upper()} count {count}: {response.registers}")
            #return response.registers
            logger.debug(f"Read from Modbus at address {hex(address)[2:].upper()} count {count}: {response.registers} | Response type: {type(response)}")
            if isinstance(response, ExceptionResponse):
                logger.error(f"Modbus exception at {hex(address)[2:].upper()}: {response.exception_code} | count={count}")
            return response
    except Exception as e:
        logger.error(f"Error reading from Modbus at address {hex(address)[2:].upper()}: {e}")
        return None

async def checkRangeforReservedRegisters(startAddress, endAddress) -> list:
    """Helper function to check a range of registers for reserved registers. Returns a list of addresses that are reserved."""
    global RegisterMap
    reservedAddresses = []
    for address in range(startAddress, endAddress+1):
        register = next((r for r in RegisterMap if r["Address"] == hex(address)[2:].upper()), None)
        if register and register.get("Reserved"):
            reservedAddresses.append(address)
    return reservedAddresses

async def getRangeWithSkippedAddresses(startAddress, addressesToSkip, numberOfRegisters) -> list:
    """Helper function to get a list of addresses in a range while skipping certain addresses. 
    This is used to optimize modbus requests by skipping reserved registers that may cause issues if we try to read them."""
    returnList = []
     # If there are reserved addresses in the range, we need to optimize requests and read as many registers as possible in one request while skipping the reserved registers. 
    # This is because if we try to read a reserved register, it may cause issues with the charge controller and prevent us from reading any registers in that range.
    # We will read from the starting address to the first reserved address, then from the first reserved address + 1 to the next reserved address, and so on until we have read all non-reserved registers in the range.
    currentStartAddress = startAddress
    currentStopAddress = 0
    lastAddressInRange = startAddress + numberOfRegisters - 1
    #make a list of requests to be sent to the charge controller based on the addresses to skip.
    # We will then send these requests sequentially and add the responses to the return list.
    requestList = []
    for skipAddress in addressesToSkip:
        logger.debug(f"Processing skip address {hex(skipAddress)[2:].upper()} ...")
        if skipAddress == currentStartAddress:
            logger.debug(f"Skipping reserved address {hex(skipAddress)[2:].upper()} at the start of the request range.")
            currentStartAddress += 1
            continue
        elif skipAddress > startAddress+numberOfRegisters:
            logger.debug(f"Skipping reserved address {hex(skipAddress)[2:].upper()} that is outside the current request range.")
            continue
        elif skipAddress < startAddress:
            logger.debug(f"Skipping reserved address {hex(skipAddress)[2:].upper()} that is less than the start address.")
            continue
        else:
            currentStopAddress = skipAddress - 1
            currentCount = currentStopAddress - currentStartAddress + 1
            requestList.append((currentStartAddress, currentCount))
            currentStartAddress = skipAddress + 1
            logger.debug(f"Next request will start at address {hex(currentStartAddress)[2:].upper()} after skipping reserved address {hex(skipAddress)[2:].upper()}")
            
    logger.debug(f"Generated request list: {[(hex(req[0])[2:].upper(), req[1]) for req in requestList]}")
    #check to see if the current start address is in skip addresses, if so increment until it isnt or we have gone through the whole range. This is to handle cases where there are multiple reserved addresses in a row at the start of the range which would cause the above loop to generate invalid requests with the current start address being a reserved address.
    if currentStartAddress in addressesToSkip:
        while currentStartAddress in addressesToSkip and currentStartAddress <= lastAddressInRange:
            logger.debug(f"Skipping reserved address {hex(currentStartAddress)[2:].upper()} at the start of the request range.")
            currentStartAddress += 1
            logger.debug(f"New start address is {hex(currentStartAddress)[2:].upper()}")

    if currentStartAddress <= lastAddressInRange:
        requestList.append((currentStartAddress, lastAddressInRange - currentStartAddress + 1))
    logger.debug(f"Generated request list without skipped addresses: {[(hex(req[0])[2:].upper(), req[1]) for req in requestList]}")
    for request in requestList:
        logger.debug(f"Sending request to Modbus for address {hex(request[0])[2:].upper()} count {request[1]} ...")
        returnList.append(await readModbusThreadSafe(request[0], count=request[1], device_id=MODBUS_UNIT)) 
    return returnList

async def getDataRange(startAddress, endAddress) -> list:
    """Helper function to read a range of registers from modbus in a thread-safe way. 
    This is used for areas like the inverter data area that have a large number of registers that need to be read."""
    logger.debug(f"Getting data range from Modbus starting at address {hex(startAddress)[2:].upper()} and ending at address {hex(endAddress)[2:].upper()} ...")
    numberOfRegisterPerRequest = 14 # The maximum number of registers we can read in one request without overwhelming the charge controller. This is based on testing and may need to be adjusted depending on the specific charge controller model and the presence of reserved registers in the range.
    numLoops = (int(endAddress)-int(startAddress))//numberOfRegisterPerRequest
    finalRequestAddress = int(startAddress) + (numLoops*numberOfRegisterPerRequest)
    finalRequestCount = int(endAddress) - finalRequestAddress +1
    itemCount = 0
    returnList = []

    addressesToSkip = await checkRangeforReservedRegisters(startAddress, endAddress)
    logger.debug(f"Addresses to skip in this range: {[hex(addr)[2:].upper() for addr in addressesToSkip]}")
    for i in range(0,numLoops):
        itemCount += numberOfRegisterPerRequest
        loopStartAddress = startAddress+(i*numberOfRegisterPerRequest)        

        #Check for addresses to skip in the current request range and if there are any, optimize the requests to skip those addresses. 
        # If there are no reserved addresses in the current request range, just read the full range in one request.
        if len(addressesToSkip) > 0 and any(addr in range(loopStartAddress, loopStartAddress+numberOfRegisterPerRequest) for addr in addressesToSkip):
            logger.debug(f"Performing optimized requests to skip reserved addresses in the current request range | start: {hex(loopStartAddress)[2:].upper()}, end: {hex(loopStartAddress+numberOfRegisterPerRequest-1)[2:].upper()}")
            logger.debug(f"loopStartAddress={hex(loopStartAddress)[2:].upper()} | addressesToSkip={ [hex(addr)[2:].upper() for addr in addressesToSkip] } | numberOfRegisterPerRequest={numberOfRegisterPerRequest}")
            listOfResponses = await getRangeWithSkippedAddresses(loopStartAddress, addressesToSkip, numberOfRegisterPerRequest)
            for response in listOfResponses:
                returnList.append(response)
        else:        
            logger.debug(f"Performing normal request for current range since there are no reserved addresses in the current request range | start: {hex(loopStartAddress)[2:].upper()}, end: {hex(loopStartAddress+numberOfRegisterPerRequest-1)[2:].upper()}")
            returnList.append(await readModbusThreadSafe(startAddress+i*numberOfRegisterPerRequest, count=numberOfRegisterPerRequest, device_id=MODBUS_UNIT))

    #check if there are any reserved addresses in the final request range and if so optimize the requests to skip those addresses.
    lastRequestList = []
    if len(addressesToSkip) > 0 and any(addr in range(finalRequestAddress, finalRequestAddress+finalRequestCount) for addr in addressesToSkip):
        logger.debug(f"Final request range from {hex(finalRequestAddress)[2:].upper()} to {hex(finalRequestAddress+finalRequestCount-1)[2:].upper()} contains reserved addresses, optimizing requests to skip those addresses...")
        lastRequestList =await getRangeWithSkippedAddresses(finalRequestAddress, addressesToSkip, finalRequestCount)
        for response in lastRequestList:
            returnList.append(response)
    else:
        logger.debug(f"Final request from {hex(finalRequestAddress)[2:].upper()} to {hex(finalRequestAddress+finalRequestCount-1)[2:].upper()} ...")
        lastRequestList =await readModbusThreadSafe(finalRequestAddress, count=finalRequestCount, device_id=MODBUS_UNIT)
        returnList.append(lastRequestList)
        
    logger.debug(f"Final request result: {lastRequestList}")
    

    logger.debug(f"finalRequestAddress={finalRequestAddress} | {hex(finalRequestAddress)}, count={finalRequestCount} ...")
    return returnList

async def wardriveRange(startAddress, endAddress) -> list:
    """Helper function to read a range of registers from modbus in a thread-safe way, but read each register individually. 
    This is used for testing and debugging to see if there are any registers in the range not supported"""
    returnList = []
    for i in range(startAddress, endAddress+1):
        rx = await readModbusThreadSafe(i, count=1, device_id=MODBUS_UNIT)
        returnList.append(rx)
        #print(f"address={i} | {hex(i)}     value={returnList[-1] if len(returnList) > 0 else 'N/A'} ...")
    return returnList

async def addInverterRegisters(modbusResponseList):
    """Helper function to add the data from a modbus response to the InverterRegisters dictionary with the address as the key and the value as the value. 
    This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console."""
    global InverterRegisters
    with memoryLock:
        modbusResponse: ModbusPDU
        logger.debug(f"Adding data from modbus response to InverterRegisters dictionary...")
        logger.debug(f"Current number of responses in list: {len(modbusResponseList)}")
        responseCounter = 0
        for modbusResponse in modbusResponseList:
            if modbusResponse:
                logger.debug(f"Processing response {responseCounter+1}/{len(modbusResponseList)} with starting address {hex(modbusResponse.address)[2:].upper()} and registers: {modbusResponse.registers}")
                logger.debug(f"Response Object: {modbusResponse}")

            addressCounter = 0
            if modbusResponse.registers is not None and len(modbusResponse.registers > 0):
                for register in modbusResponse.registers:
                    logger.debug(f"Adding register with address {hex(modbusResponse.address+addressCounter)[2:].upper()} and value {register} to InverterRegisters")
                    InverterRegisters[hex(modbusResponse.address+addressCounter)[2:].upper()] = register
                    addressCounter += 1

                responseCounter += 1


async def getP00():
    """This is the Inverter Identification area that contains information about the charge
      controller such as the model number, firmware version, etc."""
    logger.info("GET: P00 - data from the product information area (0x0A - 0x49) ...")
    global modbus
    global InverterMQTTObj
    global MQTT_CLIENT
    #Collect all the registers from the product information area. 
    # This includes things like the model number, firmware version, etc. that don't change often but are good to have for reference.
    startAddress = int("a", 16)
    endAddress = int("49", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    if registers:
        await addInverterRegisters(registers)
        await getInverterRegisterDescriptions()

        updatedRegisters = []
        for i in range(startAddress, endAddress+1):
            register = next((r for r in RegisterMap if r["Address"] == hex(i)[2:].upper()), None)
            if register:
                updatedRegisters.append(register)    
        registerAddressList = [hex(i)[2:].upper() for i in range(startAddress, endAddress+1)]
        
        if InverterMQTTObj is None and ENABLE_MQTT:
            logger.info("Creating InverterMQTTObj instance and publishing discovery message to MQTT...")
            snString = next((r for r in RegisterMap if r["Desc"] == "ProductSNStr"), {}).get("Value", "Unknown")
            app_version = next((r for r in RegisterMap if r["Desc"] == "AppVersion"), {}).get("Value", "Unknown")
            bootloader_version = next((r for r in RegisterMap if r["Desc"] == "BootloaderVersion"), {}).get("Value", "Unknown")
            controlpanel_version = next((r for r in RegisterMap if r["Desc"] == "ControlPanelVersion"), {}).get("Value", "Unknown")
            poweramp_version = next((r for r in RegisterMap if r["Desc"] == "PowerAmplifierVersion"), {}).get("Value", "Unknown")
            schema = None
            with open("inverterschema.json", "r") as f:
                schema = json.load(f)

            sw = str(app_version) + " " + str(bootloader_version)
            hw = str(controlpanel_version) + " " + str(poweramp_version)
            InverterMQTTObj = InverterMQTTSync(snString, hw, sw, mqtt_client=MQTT_CLIENT, schema=schema)  # Global variable to hold the InverterMQTTObj instance so we can access it from the publish function.
            logger.info("Publishing discovery message to MQTT...")
            await InverterMQTTObj.add_to_registers(updatedRegisters) # Add the register descriptions to the InverterMQTTObj instance so we can use them when publishing to MQTT.
            logger.info("Publishing discovery message to MQTT...")
            await InverterMQTTObj.publish_discovery() # Publish the discovery message to MQTT so Home Assistant can automatically discover the sensors.
            logger.info("Publishing initial P00 data to MQTT...")
            await InverterMQTTObj.publish_registers(registerAddressList, "p01", registermap=RegisterMap) # Publish the initial data from P00 to MQTT so we have the basic information about the charge controller available in Home Assistant right away.
        else:
            await InverterMQTTObj.add_to_registers(updatedRegisters) # Add the register descriptions to the InverterMQTTObj instance so we can use them when publishing to MQTT.
            await InverterMQTTObj.publish_registers(registerAddressList, memory_area="p01", registermap=RegisterMap) # Publish the initial data from P00 to MQTT so we have the basic information about the charge controller available in Home Assistant right away.
            logger.info("InverterMQTTObj instance already exists, published updated P00 data to MQTT...")

async def getP01():
    """This is the DC data area that contains information about the battery and load."""
    logger.info("GET: P01 - data from the DC data area (0x100 - 0x11B) ...")
    global modbus
    startAddress = int("100", 16)
    endAddress = int("11B", 16)    #there is a weird bug with the skip address
    #endAddress = int("10E", 16)    
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    if registers:
        #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
        # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
        await addInverterRegisters(registers)
        await getInverterRegisterDescriptions()



        if InverterMQTTObj is not None:
            updatedRegisters = []
            for i in range(startAddress, endAddress+1):
                register = next((r for r in RegisterMap if r["Address"] == hex(i)[2:].upper()), None)
                if register:
                    updatedRegisters.append(register)    
            registerAddressList = [hex(i)[2:].upper() for i in range(startAddress, endAddress+1)]
            logger.info("Publishing P01 data to MQTT...")
            await InverterMQTTObj.add_to_registers(updatedRegisters) # Add the register descriptions to the InverterMQTTObj instance so we can use them when publishing to MQTT.
            await InverterMQTTObj.publish_registers(registerAddressList, "p01", registermap=RegisterMap)


async def getP02():
    """
    This is the main data area that we will be using for the real-time data. It includes things like the load and panel data.
    """
    logger.info("GET: P02 - data from the inverter data area (0x200 - 0x245) ...")
    global modbus   
    #Collect all the registers from the inverter data area. This includes things like the load and panel data.
    startAddress = int("200", 16)
    endAddress = int("23f", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    if registers:
        #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
        # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
        await addInverterRegisters(registers)
        await getInverterRegisterDescriptions()

        if InverterMQTTObj is not None:
            updatedRegisters = []
            for i in range(startAddress, endAddress+1):
                register = next((r for r in RegisterMap if r["Address"] == hex(i)[2:].upper()), None)
                if register:
                    updatedRegisters.append(register)    
            registerAddressList = [hex(i)[2:].upper() for i in range(startAddress, endAddress+1)]
            logger.info("Publishing P01 data to MQTT...")
            await InverterMQTTObj.add_to_registers(updatedRegisters) # Add the register descriptions to the InverterMQTTObj instance so we can use them when publishing to MQTT.
            await InverterMQTTObj.publish_registers(registerAddressList, "p02", registermap=RegisterMap)
    

#async def getP03():
#    """This is the device control area that contains writeable registers that can be used to control the charge controller."""
#    global modbus
#    global registers_DeviceControlArea
#    p03_rx = await modbus.read_holding_registers(int("df00", 16), 14, unit=1)
#    registers_DeviceControlArea.append(p03_rx)

async def getP05():
    """This is the battery settings area that contains readable/writeable registers that can be used to set 
    battery parameters such as the battery type, capacity, etc."""
    logger.info("GET: P05 - data from the battery settings area (0xE000 - 0xE04D) ...")
    global modbus
    #Collect all the registers from the Battery settings area. This includes things like the load and panel data.
    startAddress = int("E000", 16)
    endAddress = int("E04D", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    await addInverterRegisters(registers)
 

async def getP06():
    """This is the factory settings area that contains writeable registers that can be used to set factory parameters such 
    as measurement correction coefficients, fault bit enable, and charge rates."""
    logger.info("GET: P06 - Factory settings area (0xE100 - 0xE145) ...")
    global modbus
    startAddress = int("E100", 16)
    endAddress = int("E145", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    await addInverterRegisters(registers)


async def getP07():
    """This is the user settings area that contains writeable registers that can be used to set user parameters such as the time and date."""
    logger.info("GET: P07 - User settings area (0xE200 - 0xE221) ...")
    global modbus
    startAddress = int("E200", 16)
    endAddress = int("E221", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    await addInverterRegisters(registers)


async def getP08():
    """This is the grid settings area that contains writeable registers that can be used to set grid parameters such as 
    the grid voltage and frequency."""
    logger.info("GET: P08 - data from the grid settings area (0xE400 - 0xE43B) ...")
    global modbus
    startAddress = int("E400", 16)
    endAddress = int("E43B", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    await addInverterRegisters(registers)


async def getP09():
    """This is the power stats area that contains information about the power generated and consumed."""
    logger.info("GET: P09 - data from the power stats area (0xF000 - 0xF053) ...")
    global modbus
    startAddress = int("F000", 16)
    endAddress = int("F053", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    await addInverterRegisters(registers)


async def getP10():
    """This is the faults area that contains information about the faults that have been thrown."""
    global modbus
    startAddress = int("F800", 16)
    endAddress = int("FA11", 16)
    registers = []
    registers.extend(await getDataRange(startAddress, endAddress))
    #add the data to the InverterRegisters dictionary with the address as the key and the value as the value. 
    # This will make it easier to access specific registers later when we want to publish the data to MQTT or print it to the console.
    await addInverterRegisters(registers)
    # with memoryLock:
    #     for i in range(0, len(registers), 1):
    #         InverterRegisters[hex(startAddress + i)[2:].upper()] = registers[i]

async def getInverterRegisterDescriptions():
    """Check the JSON file for register descriptions and convert the raw bytes to a proper value based on type."""
    global RegisterMap
    global InverterRegisters
    global DESC_WIDTH
        
    for address, raw in InverterRegisters.items():
        register = next((r for r in RegisterMap if r["Address"] == address), None)
        # If the register is marked as Reserved in the JSON file, skip it since it is not supported by the charge controller and may cause issues if we try to convert it.
        if register and register.get("Reserved"):
            continue
        if register:    
            #description = RegisterMap[address]["Desc"]
            length = register["Length"]
            signage = register["Sig/Unsig"]
            displayFormat = register["DispFormat"]
            units = register.get("Unit", "")

            needsConversion = True if register.get("Conversion", None) is not None else False            

            if length == 1 and signage == "Unsigned" and not needsConversion:
                register["Raw"] = raw
                register["Value"] = Decimal(str(InverterRegisters[address]))*Decimal(str(register["Mult"]))
                #Can print out message to terminal showing read (raw) and calculated values.
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']}{units} | raw: {raw}")

            elif length == 1 and signage == "Signed" and not needsConversion:
                register["Raw"] = raw
                register["Value"] = (modbusConv.modbus_16bit_to_decimal(raw)*Decimal(str(register["Mult"])))
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']}{units} | raw: {raw}")

            elif length == 2 and signage == "Signed":
                reg_hi = InverterRegisters[hex(int(address, 16))[2:].upper()]
                reg_lo = InverterRegisters[hex(int(address, 16)+1)[2:].upper()]
                register["Raw"] = (reg_hi << 16) | reg_lo
                register["Value"] = (modbusConv.modbus_32bit_float_to_decimal_be(reg_hi, reg_lo)*Decimal(str(register["Mult"])))
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']}{units} | raw: {register['Raw']} (reg_hi: {reg_hi}, reg_lo: {reg_lo})")

            elif length == 20 and displayFormat == "%s":
                # Convert from list of registers to string.
                stringValue = ""
                for addressR in range(int(address, 16), int(address, 16)+int(length)):
                    #print(f"Reading register {hex(addressR)[2:].upper()} for string conversion, value: {InverterRegisters[hex(addressR)[2:].upper()]}")
                    stringValue += chr(InverterRegisters[hex(addressR)[2:].upper()] & 0xFF)
                #comment out to silence message
                register["Value"] = stringValue
                logger.debug(f"{address}: '{register['Desc']}' and value:{stringValue}")

            elif length == 1 and register.get("Conversion") == "SELECT":
                value = str(raw & 0xFF)
                #store the numeric value
                register["Value"] = value
                select = register.get("Select", {})
                if value in select:
                    register["Readable"] = select[value]
                    logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']}{units} | raw: {raw} | readable:{register['Readable']}")
                else:
                    register["Readable"] = f"Unknown ({value})"
                    logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']} | raw: {raw} | readable:{register['Readable']} | No matching value in select {select}")
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']}{units} | raw: {raw}")

            elif register.get("Conversion") == "ERRLIST_4":
                errors = []
                for i in range(4):
                    errorCode = InverterRegisters[hex(int(address, 16)+i)[2:].upper()]
                    if errorCode != 0:
                        errors.append(f"Error code {errorCode} at address {hex(int(address, 16)+i)[2:].upper()}")
                register["Value"] = errors
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']} | raw: {hex(raw)[2:].upper()} , {hex(InverterRegisters[hex(int(address, 16)+1)[2:].upper()])} , {hex(InverterRegisters[hex(int(address, 16)+2)[2:].upper()])} , {hex(InverterRegisters[hex(int(address, 16)+3)[2:].upper()])}")

            elif register.get("Conversion") == "SYSDT":
                year = int((raw & 0xFF00) >> 8)
                month = int(raw & 0xFF)
                day = int(InverterRegisters[hex(int(address, 16)+1)[2:].upper()] & 0xFF00) >> 8
                hour = int(InverterRegisters[hex(int(address, 16)+1)[2:].upper()] & 0xFF)
                minute = int(InverterRegisters[hex(int(address, 16)+2)[2:].upper()] & 0xFF00) >> 8
                second = int(InverterRegisters[hex(int(address, 16)+2)[2:].upper()] & 0xFF)
                dateString = f"{year}/{month}/{day} {hour}:{minute}:{second}"
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{dateString} raw bytes: {hex(raw)} , {hex(InverterRegisters[hex(int(address, 16)+1)[2:].upper()])}")

            elif register.get("Conversion") == "MFGDATE":
                year = raw & 0xFF00
                month = raw & 0xFF
                day = InverterRegisters[hex(int(address, 16)+1)[2:].upper()] & 0xFF00
                hour = InverterRegisters[hex(int(address, 16)+1)[2:].upper()] & 0xFF
                dateString = f"{year}/{month}/{day} {hour}:00"
                #comment out to silence message
                logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{dateString} raw bytes: {hex(raw)} , {hex(InverterRegisters[hex(int(address, 16)+1)[2:].upper()])}")

            elif register.get("Conversion") == "LIST":
                valueList = []
                counter = 0
                for i in range(length):
                    valueList.append(Decimal(str(InverterRegisters[hex(int(address, 16)+i)[2:].upper()]))*Decimal(str(register.get("Mult", 1))))

                register["Value"] = valueList
                valueStringList = []
                for value in valueList:
                    #comment out to silence message
                    logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' (Day {counter}) and value:{value}{units}")
                    counter -= 1
                    valueStringList.append(str(value))
                valueStringList.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

                with open(f"{register['Desc']}.csv", "a", newline="") as f:
                    writer = csv.writer(f)
                    #writer.writerow(["Address", "Description", "Day", "Value", "Units"])  # header
                    counter = len(valueList)  # or whatever your starting day is
                    #export values
                    writer.writerow([valueStringList])  # write the value and timestamp to the CSV file

            elif register.get("Conversion") == "MULTBATT":
                logger.debug(f"Processing register at address {address} with raw value {raw} that requires MULTBATT conversion...")
                multAddress = register.get("MULTADDR", None)
                if multAddress:
                    multValue = InverterRegisters.get(multAddress, None)
                    if multValue is not None:                        
                        register["Raw"] = raw
                        register["Value"] = Decimal(str(InverterRegisters[address]))*(Decimal(str(multValue))/12)*Decimal(str(register["Mult"]))
                        logger.debug(f"{address}: '{fmt_desc(register['Desc'])}' and value:{register['Value']}{units} | raw: {raw} | Mult value from address {multAddress}: {multValue}")
                    else:
                        logger.error(f"Multiplier address {multAddress} not found in InverterRegisters for register at address {address}. Cannot apply multiplier.")
            else:
                logger.warning(f"{address}: '{fmt_desc(register['Desc'])}' | raw:{raw}  length:{register['Length']}. No conversion applied since the type is unknown for this register.")


# When program exits, close the modbus connection.
def exit_handler(client):
        print("\nClosing Modbus Connection...")
        modbus.close()
        client.disconnect()


def connectMQTT():
    """Connect to MQTT broker using module-level MQTT_* settings.

    Returns a connected `paho.mqtt.client.Client` with `loop_start()` called,
    or `None` on failure.
    """
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("Connected to MQTT Broker!")
        else:
            logger.error("Failed to connect, return code %d", rc)

    client = mqtt_client.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt_client.MQTTv311)

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    client.on_connect = on_connect

    try:
        client.connect(MQTT_SERVER_ADDR, MQTT_PORT)
    except Exception as e:
        logger.error("Failed to connect to MQTT broker %s:%s — %s", MQTT_SERVER_ADDR, MQTT_PORT, e)
        return None
    client.loop_start()
    return client

# Account for negative temperatures.
def getRealTemp(temp):
    if(temp/int(128) > 1):
        return -(temp%128)
    return temp

async def main():
    global RegisterMap
    global MQTT_CLIENT
    #Load register map
    with open("register_map.json", "r") as f:
        RegisterMap = json.load(f)

    # Create ModbusClient instance and connect
    # MAX3232 Breakout board connects to the raspberry pi serial pins and to the charge controller RS232 port. See pictures for details.
    global modbus
    global registers_productInformationArea
    modbus = AsyncModbusSerialClient(port=MODBUS_PORT, baudrate=MODBUS_BAUDRATE, stopbits=MODBUS_STOPBITS, bytesize=MODBUS_BYTESIZE, parity=MODBUS_PARITY, timeout=MODBUS_TIMEOUT)

    

    # connect modbus (async)
    await connectModbus()

    # Connect to MQTT broker if enabled
    if ENABLE_MQTT:
        MQTT_CLIENT = connectMQTT()
        if MQTT_CLIENT is not None:
            atexit.register(exit_handler, MQTT_CLIENT)
    

    if RS485DEBUG_MODE:
        logger.debug("RS485 DEBUG MODE ENABLED: Running a single loop to test communication.")
        #Start scheduler to run the getP00 task once to test the connection to the charge controller. If this works, then we know the RS485 communication is working and we can exit since this is just a test mode.
        #await testModbusConnection()
        #await wardriveRange(int("E400", 16), int("E43B", 16))
        count = 2
        while(True):
            if count > 0:
                count -= 1
                logger.debug("Getting P00")
                await getP00()  #Product Information area

            logger.debug("Getting P01")
            await getP01()  #DC Data Area

            logger.debug("Getting P02")
            await getP02()  #Inverter Data Area
            
            time.sleep(5)  # Add a delay between loops to avoid overwhelming the charge controller with requests in debug mode. Adjust as needed based on testing.
        
        logger.debug("Getting P00")
        await getP00()  #Product Information area

        logger.debug("Getting P01")
        await getP01()  #DC Data Area

        # logger.debug("Getting P02")
        # await getP02()  #Inverter Data Area

        #logger.debug("Getting P03")
        #await getP03()  #Device Control Area (writeable registers, not needed for just reading data)

        # logger.debug("Getting P05")
        # await getP05()  #Battery Settings Area

        # logger.debug("Getting P06")
        # await getP06()  #Factory Settings Area

        # logger.debug("Getting P07")
        # await getP07()  #User Settings Area

        # logger.debug("Getting P08")
        # await getP08()  #Grid Settings Area

        # logger.debug("Getting P09")
        # await getP09()  #Power Stats Area

        #logger.debug("Getting P10")
        #await getP10()
        #logger.debug("Dumping all received register values to InverterRegisters.txt for review...")
        # with open("InverterRegisters.txt", "w") as f:
        #     json.dump(InverterRegisters, f, indent=4)
        #logger.debug("Parsing register descriptions...")
        #await getInverterRegisterDescriptions()
        # logger.debug("Dumping all received register values with descriptions to InverterRegistersWithDescriptions.txt for review...")
        # with open("InverterRegistersWithDescriptions.txt", "w") as f:
        #     json.dump(RegisterMap, f, indent=4)
        logger.debug("RS485 Debug Mode Enabled: Exiting now since this is just a test mode.")
    else:
        sched = AsyncTaskScheduler()
        # schedule reads at different intervals
        sched.add_task(getP00, interval=GET_P00_INTERVAL, name='getP00', run_immediately=True)
        sched.add_task(getP01, interval=GET_P01_INTERVAL, name='getP01', run_immediately=True)
        # sched.add_task(getP02, interval=GET_P02_INTERVAL, name='getP02', run_immediately=True)
        # # schedule publisher (prints + optional MQTT) at realtime interval
        # sched.add_task(_publish_current, interval=PUBLISH_INTERVAL, name='publisher', args=(client,), run_immediately=True)

        # start the async task scheduler
        await sched.start()
    
        # Run until cancelled (Ctrl+C)
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await sched.stop()
    exit(0)

if __name__ == '__main__':
    asyncio.run(main())
