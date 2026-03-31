import struct

from decimal import Decimal


def modbus_16bit_to_float(raw):
    # raw is a 16-bit unsigned register value (0–65535)
    signed = struct.unpack(">h", raw.to_bytes(2, "big"))[0]
    return float(signed)

def modbus_32bit_to_float_be(reg_hi, reg_lo):
    """Convert two 16-bit modbus registers to a 32-bit IEEE754 float. Device uses big-endian format."""
    raw = (reg_hi << 16) | reg_lo
    return struct.unpack(">f", raw.to_bytes(4, "big"))[0]

def modbus_to_float_le(reg_lo, reg_hi):
    """Convert two 16-bit modbus registers to a 32-bit IEEE754 float. Device uses little-endian format."""
    raw = (reg_lo << 16) | reg_hi
    return struct.unpack(">f", raw.to_bytes(4, "big"))[0]

def modbus_16bit_to_decimal(raw):
    signed = struct.unpack(">h", raw.to_bytes(2, "big"))[0]
    return Decimal(signed)


def modbus_32bit_float_to_decimal_be(reg_hi, reg_lo):
    raw = (reg_hi << 16) | reg_lo
    value = struct.unpack(">f", raw.to_bytes(4, "big"))[0]
    return Decimal(str(value))

def modbus_32bit_float_to_decimal_le(reg_lo, reg_hi):
    raw = (reg_lo << 16) | reg_hi
    value = struct.unpack(">f", raw.to_bytes(4, "big"))[0]
    return Decimal(str(value))