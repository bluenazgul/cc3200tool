#
# cc3200tool - work with TI's CC3200 SimpleLink (TM) filesystem.
# Copyright (C) 2016-2020 Allterco Robotics
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#

import sys
import os
import time
import argparse
import struct
import math
import logging
from contextlib import contextmanager
from pkgutil import get_data
from collections import namedtuple
import json

import serial

log = logging.getLogger()
logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)-15s -- %(message)s")

CC3200_BAUD = 921600

# erasing blocks is time consuming and depends on flash type
# so separate timeout value is used
ERASE_TIMEOUT = 120

OPCODE_START_UPLOAD = b'\x21'
OPCODE_FINISH_UPLOAD = b'\x22'
OPCODE_GET_LAST_STATUS = b'\x23'
OPCODE_FILE_CHUNK = b'\x24'
OPCODE_GET_STORAGE_LIST = b'\x27'
OPCODE_FORMAT_FLASH = b'\x28'
OPCODE_GET_FILE_INFO = b'\x2A'
OPCODE_READ_FILE_CHUNK = b'\x2B'
OPCODE_RAW_STORAGE_READ = b'\x2C'
OPCODE_RAW_STORAGE_WRITE = b'\x2D'
OPCODE_ERASE_FILE = b'\x2E'
OPCODE_GET_VERSION_INFO = b'\x2F'
OPCODE_RAW_STORAGE_ERASE = b'\x30'
OPCODE_GET_STORAGE_INFO = b'\x31'
OPCODE_EXEC_FROM_RAM = b'\x32'
OPCODE_SWITCH_2_APPS = b'\x33'

STORAGE_ID_SRAM = 0x0
STORAGE_ID_SFLASH = 0x2

FLASH_BLOCK_SIZES = [0x100, 0x400, 0x1000, 0x4000, 0x10000]

SLFS_SIZE_MAP = {
    "512": 512,
    "1M": 1024,
    "2M": 2 * 1024,
    "4M": 4 * 1024,
    "8M": 8 * 1024,
    "16M": 16 * 1024,
}

SLFS_BLOCK_SIZE = 4096

# defines from cc3200-sdk/simplelink/include/fs.h
SLFS_FILE_OPEN_FLAG_COMMIT = 0x1              # /* MIRROR - for fail safe */
SLFS_FILE_OPEN_FLAG_SECURE = 0x2              # /* SECURE */
SLFS_FILE_OPEN_FLAG_NO_SIGNATURE_TEST = 0x4   # /* Relevant to secure file only  */
SLFS_FILE_OPEN_FLAG_STATIC = 0x8              # /* Relevant to secure file only */
SLFS_FILE_OPEN_FLAG_VENDOR = 0x10             # /* Relevant to secure file only */
SLFS_FILE_PUBLIC_WRITE = 0x20                 # /* Relevant to secure file only, the file can be opened for write without Token */
SLFS_FILE_PUBLIC_READ = 0x40                  # /* Relevant to secure file only, the file can be opened for read without Token  */

SLFS_MODE_OPEN_READ = 0
SLFS_MODE_OPEN_WRITE = 1
SLFS_MODE_OPEN_CREATE = 2
SLFS_MODE_OPEN_WRITE_CREATE_IF_NOT_EXIST = 3


def hexify(s):
    return " ".join([hex(x) for x in s])


Pincfg = namedtuple('Pincfg', ['invert', 'pin'])


def pinarg(extra=None):
    choices = ['dtr', 'rts', 'none']
    if extra:
        choices.extend(extra)

    def _parse(apin):
        invert = False
        if apin.startswith('~'):
            invert = True
            apin = apin[1:]
        if apin not in choices:
            raise argparse.ArgumentTypeError(f"{apin} not one of {choices}")
        return Pincfg(invert, apin)

    return _parse


def auto_int(x):
    return int(x, 0)

class PathType(object):
    def __init__(self, exists=True, type='file', dash_ok=True):
        '''exists:
                True: a path that does exist
                False: a path that does not exist, in a valid parent directory
                None: don't care
           type: file, dir, symlink, None, or a function returning True for valid paths
                None: don't care
           dash_ok: whether to allow "-" as stdin/stdout'''

        assert exists in (True, False, None)
        assert type in ('file','dir','symlink',None) or hasattr(type,'__call__')

        self._exists = exists
        self._type = type
        self._dash_ok = dash_ok

    def __call__(self, string):
        if string=='-':
            # the special argument "-" means sys.std{in,out}
            if self._type == 'dir':
                raise CC3200Error('standard input/output (-) not allowed as directory path')
            elif self._type == 'symlink':
                raise CC3200Error('standard input/output (-) not allowed as symlink path')
            elif not self._dash_ok:
                raise CC3200Error('standard input/output (-) not allowed')
        else:
            e = os.path.exists(string)
            if self._exists==True:
                if not e:
                    raise CC3200Error("path does not exist: '%s'" % string)

                if self._type is None:
                    pass
                elif self._type=='file':
                    if not os.path.isfile(string):
                        raise CC3200Error("path is not a file: '%s'" % string)
                elif self._type=='symlink':
                    if not os.path.symlink(string):
                        raise CC3200Error("path is not a symlink: '%s'" % string)
                elif self._type=='dir':
                    if not os.path.isdir(string):
                        raise CC3200Error("path is not a directory: '%s'" % string)
                elif not self._type(string):
                    raise CC3200Error("path not valid: '%s'" % string)
            else:
                if self._exists==False and e:
                    raise CC3200Error("path exists: '%s'" % string)

                p = os.path.dirname(os.path.normpath(string)) or '.'
                if not os.path.isdir(p):
                    raise CC3200Error("parent path is not a directory: '%s'" % p)
                elif not os.path.exists(p):
                    raise CC3200Error("parent directory does not exist: '%s'" % p)

        return string


# TODO: replace argparse.FileType('rb') with manual file handling
parser = argparse.ArgumentParser(description='Serial flash utility for CC3200')

parser.add_argument(
        "-p", "--port", type=str, default="/dev/ttyUSB0",
        help="The serial port to use")
parser.add_argument(
        "-if", "--image_file", type=str, default=None,
        help="Use a image file instead of serial link (read)")
parser.add_argument(
        "-of", "--output_file", type=str, default=None,
        help="Use a image file instead of serial link (write)")
parser.add_argument(
        "--reset", type=pinarg(['prompt']), default="none",
        help="dtr, rts, none or prompt, optinally prefixed by ~ to invert")
parser.add_argument(
        "--sop2", type=pinarg(), default="none",
        help="dtr, rts or none, optinally prefixed by ~ to invert")
parser.add_argument(
        "--erase_timeout", type=auto_int, default=ERASE_TIMEOUT,
        help="Specify block erase timeout for all operations which involve block erasing")
parser.add_argument(
        "--reboot-to-app", action="store_true",
        help="When finished, reboot to the application")
parser.add_argument(
        "-d", "--device", type=str, default="cc3200",
        help="Device to select cc3200/cc32xx (to decide which offsets to use)")

subparsers = parser.add_subparsers(dest="cmd")

parser_format_flash = subparsers.add_parser(
        "format_flash", help="Format the flash memory")
parser_format_flash.add_argument(
        "-s", "--size", choices=list(SLFS_SIZE_MAP.keys()), default="1M")

parser_erase_file = subparsers.add_parser(
        "erase_file", help="Erase a file from the SL filesystem")
parser_erase_file.add_argument(
        "filename", help="file on the target to be removed")

parser_write_file = subparsers.add_parser(
        "write_file", help="Upload a file on the SL filesystem")
parser_write_file.add_argument(
        "local_file", type=argparse.FileType('rb'),
        help="file on the local file system")
parser_write_file.add_argument(
        "cc_filename", help="file name to write on the target")
parser_write_file.add_argument(
        "--signature", type=argparse.FileType('rb'),
        help="file which contains the 256 bytes of signature for secured files")
parser_write_file.add_argument(
        "--file-size", type=auto_int, default=0,
        help="allows allocating more space than needed for this upload")
parser_write_file.add_argument(
        "--commit-flag", action="store_true",
        help="enables fail safe MIRROR feature")
parser_write_file.add_argument(
        "--file-id", type=auto_int, default=-1, help="if filename not available you can read a file by its id (image_file only!)")

parser_read_file = subparsers.add_parser(
        "read_file", help="read a file from the SL filesystem")
parser_read_file.add_argument(
        "cc_filename", help="file to read from the target")
parser_read_file.add_argument(
        "local_file", type=argparse.FileType('wb'),
        help="local path to store the file contents in")
parser_read_file.add_argument(
        "--file-id", type=auto_int, default=-1, help="if filename not available you can read a file by its id")

parser_write_flash = subparsers.add_parser(
        "write_flash", help="Write a Gang image on the flash")
parser_write_flash.add_argument(
        "image_file", type=argparse.FileType('rb'),
        help="gang image file prepared with Uniflash")
parser_write_flash.add_argument(
        "--no-erase", type=bool, default=False,
        help="do not perform an erase before write (for blank chips)")

parser_read_flash = subparsers.add_parser(
        "read_flash", help="Read SFFS contents into the file")
parser_read_flash.add_argument(
        "dump_file", type=argparse.FileType('w+b'),
        help="path to store the SFFS dump")
parser_read_flash.add_argument(
        "--offset", type=auto_int, default=0,
        help="starting offset (default is 0)")
parser_read_flash.add_argument(
        "--size", type=auto_int, default=-1,
        help="dump size (default is complete SFFS)")


parser_list_filesystem = subparsers.add_parser(
        "list_filesystem",
        help="List SFFS contents and statistics (blocks total/used, inter-file gaps, etc)")
parser_list_filesystem.add_argument(
        "--json-output", action="store_true",
        help="output in JSON format to stdout")
parser_list_filesystem.add_argument(
        "--inactive", action="store_true",
        help="output inactive FAT copy")
parser_list_filesystem.add_argument(
        "--extended", action="store_true",
        help="Read file header and show size in bytes")

parser_read_all_files = subparsers.add_parser(
        "read_all_files",
        help="Reads all files into a subfolder structure")
parser_read_all_files.add_argument(
        "local_dir", type=PathType(exists=True, type='dir'),
        help="local path to store the files in")
parser_read_all_files.add_argument(
        "--by-file-id", action="store_true",
        help="Read unknown filenames by its id")

parser_write_all_files = subparsers.add_parser(
        "write_all_files",
        help="Writes all files from a subfolder structure")
parser_write_all_files.add_argument(
        "local_dir", type=PathType(exists=True, type='dir'),
        help="local path to read the files from")
parser_write_all_files.add_argument(
        "--simulate", action="store_false",
        help="List all files to be written and skip writing them")

def dll_data(fname):
    return get_data('cc3200tool', os.path.join('dll', fname))


class CC3200Error(Exception):
    pass


class CC3x00VersionInfo(object):
    def __init__(self, bootloader, nwp, mac, phy, chip_type):
        self.bootloader = bootloader
        self.nwp = nwp
        self.mac = mac
        self.phy = phy
        self.chip_type = chip_type

    @property
    def is_cc3200(self):
        return (self.chip_type[0] & 0x10) != 0

    @classmethod
    def from_packet(cls, data):
        bootloader = tuple(data[0:4])
        nwp = tuple(data[4:8])
        mac = tuple(data[8:12])
        phy = tuple(data[12:16])
        chip_type = tuple(data[16:20])
        return cls(bootloader, nwp, mac, phy, chip_type)

    def __repr__(self):
        return "CC3x00VersionInfo({}, {}, {}, {}, {})".format(
            self.bootloader, self.nwp, self.mac, self.phy, self.chip_type)


class CC3x00StorageList(object):
    FLASH_BIT = 0x02
    SFLASH_BIT = 0x04
    SRAM_BIT = 0x80

    def __init__(self, value):
        self.value = value

    @property
    def flash(self):
        return (self.value & self.FLASH_BIT) != 0

    @property
    def sflash(self):
        return (self.value & self.SFLASH_BIT) != 0

    @property
    def sram(self):
        return (self.value & self.SRAM_BIT) != 0

    def __repr__(self):
        return "{}({})".format(self.__class__.__name__, hex(self.value))


class CC3x00StorageInfo(object):
    def __init__(self, block_size, block_count):
        self.block_size = block_size
        self.block_count = block_count

    @classmethod
    def from_packet(cls, data):
        bsize, bcount = struct.unpack(">HH", data[:4])
        return cls(bsize, bcount)

    def __repr__(self):
        return "{}(block_size={}, block_count={})".format(
            self.__class__.__name__, self.block_size, self.block_count)


class CC3x00Status(object):
    def __init__(self, value):
        self.value = value

    @property
    def is_ok(self):
        return self.value == 0x40

    @classmethod
    def from_packet(cls, packet):
        return cls(packet[3])


class CC3x00FileInfo(object):
    def __init__(self, exists, size=0):
        self.exists = exists
        self.size = size

    @classmethod
    def from_packet(cls, data):
        exists = data[0] == 0x01
        size = struct.unpack(">I", data[4:8])[0]
        return cls(exists, size)


class CC3x00SffsStatsFileEntry(object):
    def __init__(self, index, start_block, size_blocks, mirrored, flags, fname, header=None):
        self.index = index
        self.start_block = start_block
        self.size_blocks = size_blocks
        self.mirrored = mirrored
        self.flags = flags
        self.fname = fname

        self.total_blocks = self.size_blocks
        if self.mirrored:
            self.total_blocks = self.total_blocks * 2
            
        self.header = header
        self.magic = None
        self.size = 0
        if header != None:
            self.read_header(header)
            
    def read_header(self, header):
        self.header = header
        self.size = header[2]<<16 | header[1]<<8 | header[0]<<0 
        self.magic = bytearray(header[3:])
        
    def get_magic(self):
        ##fileheader[6:7] 4c 53
        return ''.join('{:02x}'.format(x) for x in self.magic)
        


class CC3x00SffsHole(object):
    def __init__(self, start_block, size_blocks):
        self.start_block = start_block
        self.size_blocks = size_blocks


class CC3x00SffsHeader(object):
    SFFS_HEADER_SIGNATURE = 0x534c

    def __init__(self, fat_index, fat_bytes, storage_info):
        self.is_valid = False
        self.storage_info = storage_info

        if len(fat_bytes) != storage_info.block_size:
            raise CC3200Error("incorrect FAT size")

        """
        perform just a basic parsing for now, a caller will select a more
        relevant fat and then call get_sffs_stats() in order to initiate
        complete parsing
        """

        fat_commit_revision, header_sign = struct.unpack("<HH", fat_bytes[:4])

        if fat_commit_revision == 0xffff or header_sign == 0xffff:
            # empty FAT
            return

        if header_sign != self.SFFS_HEADER_SIGNATURE:
            log.warning("broken FAT: (invalid header signature: 0x%08x, 0x%08x)",
                        fat_commit_revision, header_sign)
            return

        self.fat_bytes = fat_bytes
        self.fat_commit_revision = fat_commit_revision
        log.info("[%d] detected a valid FAT revision: %d", fat_index, self.fat_commit_revision)
        self.is_valid = True


class CC3x00SffsInfo(object):
    SFFS_FAT_FILE_NAME_ARRAY_CC3200_OFFSET = 0x200
    SFFS_FAT_FILE_NAME_ARRAY_CC32XX_OFFSET = 0x3C0

    def __init__(self, fat_header, storage_info, meta2, device):
        self.fat_commit_revision = fat_header.fat_commit_revision

        self.block_size = storage_info.block_size
        self.block_count = storage_info.block_count

        occupied_block_snippets = []

        self.used_blocks = 5  # FAT table size, as per documentation
        occupied_block_snippets.append((0, 5))

        self.files = []
        
        file_name_array_offset = self.SFFS_FAT_FILE_NAME_ARRAY_CC3200_OFFSET
        if device == "cc32xx":
            file_name_array_offset = self.SFFS_FAT_FILE_NAME_ARRAY_CC32XX_OFFSET

        """
        TI's doc: "Total number of files is limited to 128 files, including
        system and configuration files"
        """
        for i in range(128):
            # scan the complete FAT table (as it appears to be)
            meta = fat_header.fat_bytes[(i + 1) * 4:(i + 2) * 4]

            if meta == b"\xff\xff\xff\xff" or meta == struct.pack("BBBB", 0xff, i, 0xff, 0x7f):
                # empty entry in the middle of the FAT table
                continue

            index, size_blocks, start_block_lsb, flags_sb_msb = struct.unpack("BBBB", meta)
            if index != i:
                raise CC3200Error("incorrect FAT entry (index %d != %d)" % (index, i))

            """
            It's not completely clear, what all of these flags do mean, and
            where does the boundary between 'start block MSB' and 'flags'
            exactly lie.

            According to observations:
            - 0x8 seems to be set to '1' for all the files except for
                  /sys/mcuimg.bin (looks like this is the mark of the
                  user's app image for the CC3200's ROM bootloader)
            - 0x4 seems to be a negated flag of the mirrored/commit option

            - 4 LSB bits should be exactly enough to address the SFFS
                max size of 16 MB using 4K blocks
            """

            flags = flags_sb_msb >> 4
            start_block_msb = flags_sb_msb & 0xf

            mirrored = (flags & 0x4) == 0
            start_block = (start_block_msb << 8) + start_block_lsb

            meta2_e = meta2[i * 4 : (i + 1) * 4]
            fname_offset, fname_len = struct.unpack("<HH", meta2_e)
            fo_abs = file_name_array_offset + fname_offset
            fname = meta2[fo_abs:fo_abs + fname_len]

            entry = CC3x00SffsStatsFileEntry(i, start_block, size_blocks,
                                             mirrored, flags, fname.decode('ascii'))
            self.files.append(entry)

            occupied_block_snippets.append((start_block, entry.total_blocks))
            self.used_blocks = self.used_blocks + entry.total_blocks

        # in order to track the trailing "hole", like uniflash does
        occupied_block_snippets.append((self.block_count, 0))

        self.holes = []
        occupied_block_snippets.sort(key=lambda e: e[0])
        prev_end_block = 0
        for snippet in occupied_block_snippets:
            if snippet[0] < prev_end_block:
                for f in self.files:
                    log.info("[%d] block %d..%d fname=%s" %
                             (f.index, f.start_block, f.start_block + f.total_blocks, f.fname))
                raise CC3200Error("broken FAT: overlapping entry at block %d (prev end was %d)" %
                                  (snippet[0], prev_end_block))
            if snippet[0] > prev_end_block:
                hole = CC3x00SffsHole(prev_end_block, snippet[0] - prev_end_block - 1)
                self.holes.append(hole)
            prev_end_block = snippet[0] + snippet[1]

    def print_sffs_info(self, extended=False):
        log.info("Serial Flash block size:\t%d bytes", self.block_size)
        log.info("Serial Flash capacity:\t%d blocks", self.block_count)
        log.info("")
        
        if extended:
            log.info("\tfile\tstart\tsize\tsize\tfail\tflags\ttotal\tmagic\t\tfilename")
            log.info("\tindex\tblock\t[BLKs]\t[bytes]\tsafe\t\t[BLKs]")
            log.info("-------------------------------------------------------------------------------------------------")
            log.info("\tN/A\t0\t5\tN/A\tN/A\t5\tN/A\tN/A\t\tFATFS")
        else:
            log.info("\tfile\tstart\tsize\tfail\tflags\ttotal\tfilename")
            log.info("\tindex\tblock\t[BLKs]\tsafe\t[BLKs]")
            log.info("----------------------------------------------------------------------------")
            log.info("\tN/A\t0\t5\tN/A\tN/A\t5\tFATFS")
            
        for f in self.files:
            if extended:
                log.info("\t%d\t%d\t%d\t%d\t%s\t0x%x\t%d\t%s\t%s" %
                        (f.index, f.start_block, f.size_blocks, f.size,
                        f.mirrored and "yes" or "no",
                        f.flags, f.total_blocks, f.get_magic(), f.fname))
            else:
                log.info("\t%d\t%d\t%d\t%s\t0x%x\t%d\t%s" %
                        (f.index, f.start_block, f.size_blocks,
                        f.mirrored and "yes" or "no",
                        f.flags, f.total_blocks, f.fname))

        log.info("")
        log.info("   Flash usage")
        log.info("-------------------------")
        log.info("used space:\t%d blocks", self.used_blocks)
        log.info("free space:\t%d blocks",
                 self.block_count - self.used_blocks)

        for h in self.holes:
            log.info("memory hole:\t[%d-%d]", h.start_block,
                     h.start_block + h.size_blocks)

    def print_sffs_info_short(self):
        log.info("FAT r%d, num files: %d, used/free blocks: %d/%d",
                 self.fat_commit_revision, len(self.files), self.used_blocks,
                 self.block_count - self.used_blocks)

    def print_sffs_info_json(self):
        print(json.dumps(self, cls=CustomJsonEncoder))


class CustomJsonEncoder(json.JSONEncoder):
    def default(self, o):
        return o.__dict__


class CC3200Connection(object):
    SFFS_FAT_METADATA2_CC3200_OFFSET = 0x774
    SFFS_FAT_METADATA2_CC32XX_OFFSET = 0x2000
    SFFS_FAT_METADATA2_LENGTH = 0x1000
    SFFS_FAT_PART_OFFSET = 0x1000
    SFFS_FAT_FILE_HEADER_SIZE = 0x8

    TIMEOUT = 5
    DEFAULT_SLFS_SIZE = "1M"

    def __init__(self, port, reset=None, sop2=None, erase_timeout=ERASE_TIMEOUT, device=None, image_file=None, output_file=None):
        self.port = port
        if not self.port is None:
            port.timeout = self.TIMEOUT
        self._device = device
        self._reset = reset
        self._sop2 = sop2
        self._erase_timeout = erase_timeout
        self._image_file = None
        self._output_file = None
        
        self.vinfo = None
        self.vinfo_apps = None
        
        if not image_file is None:
            self._image_file = open(image_file, 'rb')
        if not output_file is None:
            self._output_file = open(output_file, 'w+b')

    def copy_input_file_to_output_file(self):
        if not self._image_file is None or not self._output_file is None:
            self._image_file.seek(0)
            data = self._image_file.read()
            self._output_file.seek(0)
            self._output_file.write(data)
    
    @contextmanager
    def _serial_timeout(self, timeout=None):
        if timeout is None:
            yield self.port
            return
        if timeout == self.port.timeout:
            yield self.port
            return
        orig_timeout, self.port.timeout = self.port.timeout, timeout
        yield self.port
        self.port.timeout = orig_timeout

    def _set_sop2(self, level):
        if self._sop2.pin == "none":
            return

        toset = level ^ self._sop2.invert
        if self._sop2.pin == 'dtr':
            self.port.dtr = toset
        if self._sop2.pin == 'rts':
            self.port.rts = toset

    def _do_reset(self, sop2):
        self._set_sop2(sop2)

        if self._reset.pin == "none":
            return

        if self._reset.pin == "prompt":
            print("Reset the device with SOP2 {}asserted and press Enter".format(
                '' if sop2 else 'de'
            ))
            input()
            return

        in_reset = True ^ self._reset.invert
        if self._reset.pin == 'dtr':
            self.port.dtr = in_reset
            time.sleep(.1)
            self.port.dtr = not in_reset

        if self._reset.pin == 'rts':
            self.port.rts = in_reset
            time.sleep(.1)
            self.port.rts = not in_reset

    def _read_ack(self, timeout=None):
        ack_bytes = []
        with self._serial_timeout(timeout) as port:
            while True:
                b = port.read(1)
                if not b:
                    log.error("timed out while waiting for ack")
                    return False
                ack_bytes.append(b)
                if len(ack_bytes) > 2:
                    ack_bytes.pop(0)
                if ack_bytes == [b'\x00', b'\xCC']:
                    return True

    def _read_packet(self, timeout=None):
        with self._serial_timeout(timeout) as port:
            header = port.read(3)
            if len(header) != 3:
                raise CC3200Error("read_packed timed out on header")
            len_bytes = header[:2]
            csum_byte = header[2]

        data_len = struct.unpack(">H", len_bytes)[0] - 2
        with self._serial_timeout(timeout):
            data = self.port.read(data_len)

        if (len(data) != data_len):
            raise CC3200Error("did not get entire response")

        ccsum = sum(data)
        ccsum = ccsum & 0xff
        if ccsum != csum_byte:
            raise CC3200Error("rx csum failed")

        self._send_ack()
        return data

    def _send_packet(self, data, timeout=None):
        assert len(data)
        checksum = sum(data)
        len_blob = struct.pack(">H", len(data) + 2)
        csum = struct.pack("B", checksum & 0xff)
        self.port.write(len_blob + csum + data)
        if not self._read_ack(timeout):
            raise CC3200Error(
                    f"No ack for packet opcode=0x{data[0]:02x}")

    def _send_ack(self):
        self.port.write(b'\x00\xCC')

    def _get_last_status(self):
        self._send_packet(OPCODE_GET_LAST_STATUS)
        
        if not self.port is None:
            status = self._read_packet()
            log.debug("get last status got %s", hexify(status))
            return CC3x00Status(status[0])

        return CC3x00Status(0)

    def _do_break(self, timeout):
        self.port.send_break(.2)
        return self._read_ack(timeout)

    def _try_breaking(self, tries=5, timeout=2):
        for _ in range(tries):
            if self._do_break(timeout):
                break
        else:
            raise CC3200Error("Did not get ACK on break condition")

    def _get_version(self):
        self._send_packet(OPCODE_GET_VERSION_INFO)

        if not self.port is None:
            version_data = self._read_packet()
            if len(version_data) != 28:
                raise CC3200Error(f"Version info should be 28 bytes, got {len(version_data)}")
            return CC3x00VersionInfo.from_packet(version_data)

        return CC3x00VersionInfo((0,4,1,2), (0,0,0,0), (0,0,0,0), (0,0,0,0), (16,0,0,0))

    def _get_storage_list(self):
        log.info("Getting storage list...")
        if not self.port is None:
            self._send_packet(OPCODE_GET_STORAGE_LIST)
            with self._serial_timeout(.5):
                slist_byte = self.port.read(1)
                if len(slist_byte) != 1:
                    raise CC3200Error("Did not receive storage list byte")
            return CC3x00StorageList(slist_byte[0])

        return CC3x00StorageList(15)

    def _get_storage_info(self, storage_id=STORAGE_ID_SRAM):
        log.info("Getting storage info...")
        if not self.port is None:
            self._send_packet(OPCODE_GET_STORAGE_INFO +
                              struct.pack(">I", storage_id))
            sinfo = self._read_packet()
            if len(sinfo) < 4:
                raise CC3200Error(f"getting storage info got {len(sinfo)} bytes")
            log.info("storage #%d info bytes: %s", storage_id, ", "
                     .join([hex(x) for x in sinfo]))
            return CC3x00StorageInfo.from_packet(sinfo)

        return CC3x00StorageInfo(SLFS_BLOCK_SIZE, 1024) #TODO: as parameter

    def _erase_blocks(self, start, count, storage_id=STORAGE_ID_SRAM):
        command = OPCODE_RAW_STORAGE_ERASE + \
            struct.pack(">III", storage_id, start, count)
        self._send_packet(command, timeout=self._erase_timeout)

    def _send_chunk(self, offset, data, storage_id=STORAGE_ID_SRAM):
        if not self.port is None:
            command = OPCODE_RAW_STORAGE_WRITE + \
                struct.pack(">III", storage_id, offset, len(data))
            self._send_packet(command + data)
            return
        self._output_file.seek(offset)
        self._output_file.write(data)

    def _raw_write(self, offset, data, storage_id=STORAGE_ID_SRAM):
        slist = self._get_storage_list()
        if storage_id == STORAGE_ID_SFLASH and not slist.sflash:
            raise CC3200Error("no serial flash?!")
        if storage_id == STORAGE_ID_SRAM and not slist.sram:
            raise CC3200Error("no sram?!")

        chunk_size = 4080
        sent = 0
        while sent < len(data):
            chunk = data[sent:sent + chunk_size]
            self._send_chunk(offset + sent, chunk, storage_id)
            sent += len(chunk)

    def _raw_write_file(self, offset, filename, storage_id=STORAGE_ID_SRAM):
        with open(filename, 'r') as f:
            data = f.read()
            return self._raw_write(offset, data, storage_id)

    def _read_chunk(self, offset, size, storage_id=STORAGE_ID_SRAM):
        if not self.port is None:
            # log.info("Reading chunk at 0x%x size 0x%x..." % (offset, size))
            command = OPCODE_RAW_STORAGE_READ + \
                struct.pack(">III", storage_id, offset, size)
            self._send_packet(command)
            data = self._read_packet()
            if len(data) != size:
                raise CC3200Error("invalid received size: %d vs %d" % (len(data), size))
            return data
        
        self._image_file.seek(offset)
        data = self._image_file.read(size)
        return data

    def _raw_read(self, offset, size, storage_id=STORAGE_ID_SRAM, sinfo=None):
        slist = self._get_storage_list()
        if storage_id == STORAGE_ID_SFLASH and not slist.sflash:
            raise CC3200Error("no serial flash?!")
        if storage_id == STORAGE_ID_SRAM and not slist.sram:
            raise CC3200Error("no sram?!")

        if not sinfo:
            sinfo = self._get_storage_info(storage_id)
        storage_size = sinfo.block_count * sinfo.block_size

        if offset > storage_size:
            raise CC3200Error("offset %d is bigger than available mem %d" %
                              (offset, storage_size))

        if size < 1:
            size = storage_size - offset
            log.info("Setting raw read size to maximum: %d", size)
        elif size + offset > storage_size:
            raise CC3200Error("size %d + offset %d is bigger than available mem %d" %
                              (size, offset, storage_size))

        log.info("Reading raw storage #%d start 0x%x, size 0x%x..." %
                 (storage_id, offset, size))

        # XXX 4096 works faster, but 256 was sniffed from the uniflash
        chunk_size = 4096
        rx_data = b''
        while size - len(rx_data) > 0:
            rx_data += self._read_chunk(offset + len(rx_data),
                                        min(chunk_size, size - len(rx_data)),
                                        storage_id)
            sys.stderr.write('.')
            sys.stderr.flush()
        sys.stderr.write("\n")
        return rx_data

    def _exec_from_ram(self):
        self._send_packet(OPCODE_EXEC_FROM_RAM)

    def _get_file_info(self, filename, file_id=-1):
        if not self.port is None and file_id == -1:
            command = OPCODE_GET_FILE_INFO \
                + struct.pack(">I", len(filename)) \
                + filename.encode()
            self._send_packet(command)
            finfo = self._read_packet()
            if len(finfo) < 5:
                raise CC3200Error()
            return CC3x00FileInfo.from_packet(finfo)
        
        fat_info = self.get_fat_info(inactive=False)
        finfo = CC3x00FileInfo(exists=False, size=0)
        for file in fat_info.files:
            if file_id == -1:
                if file.fname == filename:
                    finfo = CC3x00FileInfo(exists=True, size=file.size_blocks*SLFS_BLOCK_SIZE)
                    break
            elif file.index == file_id:
                finfo = CC3x00FileInfo(exists=True, size=file.size_blocks*SLFS_BLOCK_SIZE)
                break
        return finfo
    

    def _open_file_for_write(self, filename, file_len, fs_flags=None):
        for bsize_idx, bsize in enumerate(FLASH_BLOCK_SIZES):
            if (bsize * 255) >= file_len:
                blocks = int(math.ceil(float(file_len) / bsize))
                break
        else:
            raise CC3200Error("file is too big")

        fs_access = SLFS_MODE_OPEN_WRITE_CREATE_IF_NOT_EXIST
        flags = (((fs_access & 0x0f) << 12) |
                 ((bsize_idx & 0x0f) << 8) |
                 (blocks & 0xff))

        if fs_flags is not None:
            flags |= (fs_flags & 0xff) << 16

        return self._open_file(filename, flags)

    def _open_file_for_read(self, filename):
        return self._open_file(filename, 0)

    def _open_file(self, filename, slfs_flags):
        command = OPCODE_START_UPLOAD + struct.pack(">II", slfs_flags, 0) + \
            filename.encode() + b'\x00\x00'
        self._send_packet(command)

        token = self.port.read(4)
        if not len(token) == 4:
            raise CC3200Error("open")

    def _close_file(self, signature=None):
        if signature is None:
            signature = b'\x46' * 256
        if len(signature) != 256:
            raise CC3200Error("bad signature length")
        command = OPCODE_FINISH_UPLOAD
        command += b'\x00' * 63
        command += signature
        command += b'\x00'
        self._send_packet(command)
        s = self._get_last_status()
        if not s.is_ok:
            raise CC3200Error("closing file failed")

    def connect(self):
        log.info("Connecting to target...")
        self.port.flushInput()
        self._do_reset(True)
        self._try_breaking(tries=5, timeout=2)
        log.info("Connected, reading version...")
        self.vinfo = self._get_version()

    def reboot_to_app(self):
        log.info("Rebooting to application")
        self._do_reset(False)

    def switch_to_nwp_bootloader(self):
        log.info("Switching to NWP bootloader...")
        vinfo = self._get_version()
        if not vinfo.is_cc3200:
            log.debug("This looks like the NWP already")
            return

        if vinfo.bootloader[1] < 3:
            raise CC3200Error("Unsupported device")

        if vinfo.bootloader[1] == 3:
            # cesanta upload and exec rbtl3101_132.dll for this version
            # then do the UART switch
            raise CC3200Error("Not yet supported device (bootloader=3)")

        self.switch_uart_to_apps()

        if vinfo.bootloader[1] == 3:
            # should upload rbtl3100.dll
            raise CC3200Error("Not yet supported device (NWP bootloader=3)")

        if vinfo.bootloader[1] >= 4:
            log.info("Uploading rbtl3100s.dll...")
            self._raw_write(0, dll_data('rbtl3100s.dll'))
            self._exec_from_ram()

        if not self._read_ack():
            raise CC3200Error("got no ACK after exec from ram")

    def switch_uart_to_apps(self):
        # ~ 1 sec delay by the APPS MCU
        log.info("Switching UART to APPS...")
        command = OPCODE_SWITCH_2_APPS + struct.pack(">I", 26666667)
        self._send_packet(command)
        log.info("Resetting communications ...")
        time.sleep(1)
        self._try_breaking()
        self.vinfo_apps = self._get_version()

    def format_slfs(self, size=None):
        if size is None:
            size = self.DEFAULT_SLFS_SIZE

        if size not in SLFS_SIZE_MAP:
            raise CC3200Error("invalid SLFS size")

        size = SLFS_SIZE_MAP[size]

        log.info("Formatting flash with size=%s", size)
        command = OPCODE_FORMAT_FLASH \
            + struct.pack(">IIIII", 2, size//4, 0, 0, 2)

        self._send_packet(command)

        s = self._get_last_status()
        if not s.is_ok:
            raise CC3200Error("Format failed")

    def erase_file(self, filename, force=False):
        if not force:
            finfo = self._get_file_info(filename)
            if not finfo.exists:
                log.warn("File '%s' does not exist, won't erase", filename)
                return

        log.info("Erasing file %s...", filename)
        command = OPCODE_ERASE_FILE + struct.pack(">I", 0) + \
            filename.encode() + b'\x00'
        self._send_packet(command)
        s = self._get_last_status()
        if not s.is_ok:
            raise CC3200Error(f"Erasing file failed: 0x{s.value:02x}")

    def write_file(self, local_file, cc_filename, file_id=-1, sign_file=None, size=0, commit_flag=False, use_api=True):
        # size must be known in advance, so read the whole thing
        file_data = local_file.read()
        file_len = len(file_data)

        if not file_len:
            log.warn("Won't upload empty file")
            return

        sign_data = None
        fs_flags = None

        if commit_flag:
            fs_flags = SLFS_FILE_OPEN_FLAG_COMMIT

        if sign_file:
            sign_data = sign_file.read(256)
            fs_flags = (
                    SLFS_FILE_OPEN_FLAG_COMMIT |
                    SLFS_FILE_OPEN_FLAG_SECURE |
                    SLFS_FILE_PUBLIC_WRITE)
            
        if use_api == False:
            return self._write_file_raw(local_file, cc_filename, file_id, sign_data, fs_flags, size, file_data, file_len)
        else:
            return self._write_file_api(local_file, cc_filename, sign_data, fs_flags, size, file_data, file_len)
            
    def _write_file_raw(self, local_file, cc_filename, file_id, sign_data, fs_flags, size, file_data, file_len):
        fat_info = self.get_fat_info(inactive=False, extended=True)
        filefinfo = None
        for file in fat_info.files:
            if file_id == -1:
                if file.fname == cc_filename:
                    filefinfo = file
                    break
            elif file.index == file_id:
                filefinfo = file
                break
        
        if filefinfo == None:
            log.info("File not found, only overwriting is supported.")
            raise CC3200Error(f"{cc_filename} or id {file_id} not found, but only overwriting is supported.")
        
        #TODO: commit_flag --> Mirror
        alloc_size_effective = alloc_size = max(size, file_len) + self.SFFS_FAT_FILE_HEADER_SIZE
        blocks = int(alloc_size/fat_info.block_size+1) 
        if (fs_flags and fs_flags & SLFS_FILE_OPEN_FLAG_COMMIT):
            alloc_size_effective *= 2
        
        if blocks > filefinfo.size_blocks:
            max_size = filefinfo.size_blocks*fat_info.block_size+self.SFFS_FAT_FILE_HEADER_SIZE
            raise CC3200Error(f"{local_file.name} is too big. It should not be bigger that {max_size}bytes")
                    
        log.info("Uploading file %s -> %s (%i) [%d, disk=%d]...",
                 local_file.name, cc_filename, filefinfo.index, alloc_size, alloc_size_effective)
        
        if filefinfo.header == None or len(filefinfo.header) != self.SFFS_FAT_FILE_HEADER_SIZE:
            raise CC3200Error(f"File header in flash is missing or has the wrong size")
        
        fatfs_offset = filefinfo.start_block*fat_info.block_size
        header = list(filefinfo.header)
        #TODO: Use old filesize, so space stays reserved
        header[2] = (file_len>>16) & 0xFF
        header[1] = (file_len>>8) & 0xFF
        header[0] = (file_len>>0) & 0xFF
        header_new = bytearray(header)
        self._raw_write(fatfs_offset, header_new, storage_id=STORAGE_ID_SFLASH)
        self._raw_write(self.SFFS_FAT_FILE_HEADER_SIZE+fatfs_offset, file_data, storage_id=STORAGE_ID_SFLASH)
                
    def _write_file_api(self, local_file, cc_filename, sign_data, fs_flags, size, file_data, file_len):
        finfo = self._get_file_info(cc_filename)
        if finfo.exists:
            log.info("File exists on target, erasing")
            self.erase_file(cc_filename)

        alloc_size_effective = alloc_size = max(size, file_len)

        if (fs_flags and fs_flags & SLFS_FILE_OPEN_FLAG_COMMIT):
            alloc_size_effective *= 2

        timeout = self.port.timeout
        if (alloc_size_effective > 200000):
            timeout = max(timeout, 5 * ((alloc_size_effective / 200000) + 1))  # empirical value is ~252925 bytes for 5 sec timeout

        log.info("Uploading file %s -> %s [%d, disk=%d]...",
                 local_file.name, cc_filename, alloc_size, alloc_size_effective)

        with self._serial_timeout(timeout):
            self._open_file_for_write(cc_filename, alloc_size, fs_flags)

        pos = 0
        while pos < file_len:
            chunk = file_data[pos:pos+SLFS_BLOCK_SIZE]
            command = OPCODE_FILE_CHUNK + struct.pack(">I", pos)
            command += chunk
            self._send_packet(command)
            res = self._get_last_status()
            if not res.is_ok:
                raise CC3200Error(f"writing at pos {pos} failed")
            pos += len(chunk)
            sys.stderr.write('.')
            sys.stderr.flush()

        sys.stderr.write("\n")
        log.debug("Closing file ...")
        return self._close_file(sign_data)

    def read_file(self, cc_fname, local_file, file_id=-1):
        finfo = self._get_file_info(cc_fname, file_id)
        if not finfo.exists:
            raise CC3200Error(f"{cc_fname} does not exist on target")

        log.info("Reading file %s -> %s", cc_fname, local_file.name)

        if not self.port is None and file_id == -1:
            self._open_file_for_read(cc_fname)

            pos = 0
            while pos < finfo.size:
                toread = min(finfo.size - pos, SLFS_BLOCK_SIZE)
                command = OPCODE_READ_FILE_CHUNK + struct.pack(">II", pos, toread)
                self._send_packet(command)
                resp = self._read_packet()
                if len(resp) != toread:
                    raise CC3200Error("reading chunk failed")

                local_file.write(resp)
                pos += toread

            self._close_file()
            return
        
        fat_info = self.get_fat_info(inactive=False, extended=True)
        filefinfo = None
        for file in fat_info.files:
            if file_id == -1:
                if file.fname == cc_fname:
                    filefinfo = file
                    break
            elif file.index == file_id:
                filefinfo = file
                break
            
        sinfo = self._get_storage_info(storage_id=STORAGE_ID_SFLASH)
        fatfs_offset = filefinfo.start_block*fat_info.block_size        
        data = self._raw_read(self.SFFS_FAT_FILE_HEADER_SIZE+fatfs_offset, filefinfo.size, storage_id=STORAGE_ID_SFLASH, sinfo=sinfo)
        local_file.write(data) 

    def write_flash(self, image, erase=True):
        data = image.read()
        data_len = len(data)
        if erase:
            count = int(math.ceil(data_len / float(SLFS_BLOCK_SIZE)))
            self._erase_blocks(0, count, storage_id=STORAGE_ID_SFLASH)

        self._raw_write(8, data[8:], storage_id=STORAGE_ID_SFLASH)
        self._raw_write(0, data[:8], storage_id=STORAGE_ID_SFLASH)

    def read_flash(self, image_file, offset, size):
        data = self._raw_read(offset, size, storage_id=STORAGE_ID_SFLASH)
        image_file.write(data)

    def get_fat_info(self, inactive=False, extended=False):
        metadata2_offset = self.SFFS_FAT_METADATA2_CC3200_OFFSET
        if self._device == "cc32xx":
            metadata2_offset = self.SFFS_FAT_METADATA2_CC32XX_OFFSET
            
        sinfo = self._get_storage_info(storage_id=STORAGE_ID_SFLASH)
        
        fat_table_bytes = self._raw_read(0, 2 * sinfo.block_size,
                                         storage_id=STORAGE_ID_SFLASH,
                                         sinfo=sinfo)

        fat_table_bytes1 = fat_table_bytes[:sinfo.block_size]
        fat_table_bytes2 = fat_table_bytes[sinfo.block_size:]

        """
        In SFFS there're 2 entries of FAT, none of which has a fixed primary
        or secondary role. Instead, these entries are written interchangeably,
        with the newest one being marked with a larger 2-byte number, referred
        in this source code as 'fat_commit_revision' (this is a made-up term).

        The algorithm is described in detail here:
        http://processors.wiki.ti.com/index.php/CC3100_%26_CC3200_Serial_Flash_Guide#File_appending

        It was also noticed that after the successful write to a newer FAT,
        the older one might got overwritten with 0xFF by the CC3200's SFFS
        driver (effectively marking it as invalid), but not always.
        """

        fat_hdr1 = CC3x00SffsHeader(0, fat_table_bytes1, sinfo)
        fat_hdr2 = CC3x00SffsHeader(1, fat_table_bytes2, sinfo)

        fat_hdrs = []
        if fat_hdr1.is_valid:
            fat_hdrs.append(fat_hdr1)
        if fat_hdr2.is_valid:
            fat_hdrs.append(fat_hdr2)
            metadata2_offset += self.SFFS_FAT_PART_OFFSET

        meta2 = self._raw_read(metadata2_offset, metadata2_offset + self.SFFS_FAT_METADATA2_LENGTH,
                                         storage_id=STORAGE_ID_SFLASH,
                                         sinfo=sinfo)

        if len(fat_hdrs) == 0:
            raise CC3200Error("no valid fat tables found")

        if len(fat_hdrs) > 1:
            # find the latest
            fat_hdrs.sort(reverse=True, key=lambda e: e.fat_commit_revision)

        if inactive:
            if len(fat_hdrs) > 1:
                fat_hdr = fat_hdrs[1]
            else:
                raise CC3200Error("no valid inactive fat table found")
        else:
            fat_hdr = fat_hdrs[0]
        log.info("selected FAT revision: %d (%s)", fat_hdr.fat_commit_revision, inactive and 'inactive' or 'active')
        
        
        fat_info = CC3x00SffsInfo(fat_hdr, sinfo, meta2, self._device)
        
        if extended:
            for file in fat_info.files:
                fatfs_offset = file.start_block*sinfo.block_size
                fileheader = self._raw_read(fatfs_offset, self.SFFS_FAT_FILE_HEADER_SIZE, storage_id=STORAGE_ID_SFLASH, sinfo=sinfo)
                file.read_header(fileheader)
        
        return fat_info

    def list_filesystem(self, json_output=False, inactive=False, extended=False):
        fat_info = self.get_fat_info(inactive=inactive, extended=extended)
        fat_info.print_sffs_info(extended)
        if json_output:
            fat_info.print_sffs_info_json()
    
    def read_all_files(self, local_dir, by_file_id=False):
        fat_info = self.get_fat_info(inactive=False)
        fat_info.print_sffs_info()
        for f in fat_info.files:
            ccname = f.fname
            if by_file_id and f.fname == '':
                ccname = str(f.index)
                                
            if ccname.startswith('/'):
                ccname = ccname[1:]
            target_file = os.path.join(local_dir, ccname) 
            if not os.path.exists(os.path.dirname(target_file)):
                os.makedirs(name=os.path.dirname(target_file))

            try:
                if by_file_id and f.fname == '':
                    self.read_file(ccname, open(target_file, 'wb', -1), f.index)
                else:
                    self.read_file(f.fname, open(target_file, 'wb', -1))
            except Exception as ex:
                log.error("File %s could not be read, %s" % (f.fname, str (ex)))

    def write_all_files(self, local_dir, write=True, use_api=True):
        for root, dirs, files in os.walk(local_dir):
            for file in files:
                filepath = os.path.join(root, file)
                ccpath = filepath[len(local_dir):]
                if not ccpath.startswith("/"):
                    ccpath = "/" + ccpath

                if write:
                    self.write_file(local_file=open(filepath, 'rb', -1), cc_filename=ccpath, use_api=use_api)
                else:
                    log.info("Simulation: Would copy local file %s to cc3200 %s" % (filepath, ccpath))


def split_argv(cmdline_args):
    """Manually split sys.argv into subcommand sections

    The first returned element should contain all global options along with
    the first command. Subsequent elements will contain a command each, with
    options applicable for the specific command. This is needed so we can
    specify different --file-size for different write_file commands.
    """
    args = []
    have_cmd = False
    for x in cmdline_args:
        if x in subparsers.choices:
            if have_cmd:
                yield args
                args = []
            have_cmd = True
            args.append(x)
        else:
            args.append(x)

    if args:
        yield args


def main():
    commands = []
    for cmdargs in split_argv(sys.argv[1:]):
        commands.append(parser.parse_args(cmdargs))

    if len(commands) == 0:
        parser.print_help()
        sys.exit(-1)

    args = commands[0]

    sop2_method = args.sop2
    reset_method = args.reset
    if sop2_method.pin == reset_method.pin and reset_method.pin != 'none':
        log.error("sop2 and reset methods cannot be the same output pin")
        sys.exit(-3)

    port_name = args.port

    if not args.image_file is None:
        cc = CC3200Connection(None, reset_method, sop2_method, erase_timeout=args.erase_timeout, device=args.device, image_file=args.image_file, output_file=args.output_file)
    
    else:
        try:
            p = serial.Serial(
                port_name, baudrate=CC3200_BAUD, parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE)
        except (Exception, ) as e:
            log.warn("unable to open serial port %s: %s", port_name, e)
            sys.exit(-2)

        cc = CC3200Connection(p, reset_method, sop2_method, erase_timeout=args.erase_timeout)
        try:
            cc.connect()
            log.info("connected to target")
        except (Exception, ) as e:
            log.error(f"Could not connect to target: {e}")
            sys.exit(-3)

        log.info("Version: %s", cc.vinfo)

        # TODO: sane error handling

        if cc.vinfo.is_cc3200:
            log.info("This is a CC3200 device")
            cc.switch_to_nwp_bootloader()
            log.info("APPS version: %s", cc.vinfo_apps)

    check_fat = False

    for command in commands:
        if command.cmd == "format_flash":
            cc.format_slfs(command.size)

        if command.cmd == 'write_file':
            use_api = True
            if not command.image_file is None and not command.output_file is None:
                use_api = False
                cc.copy_input_file_to_output_file()
                
            cc.write_file(command.local_file, command.cc_filename, command.file_id,
                          command.signature, command.file_size,
                          command.commit_flag, use_api)
            check_fat = True

        if command.cmd == "read_file":
            cc.read_file(command.cc_filename, command.local_file, command.file_id)

        if command.cmd == "erase_file":
            log.info("Erasing file %s", command.filename)
            cc.erase_file(command.filename)

        if command.cmd == "write_flash":
            cc.write_flash(command.image_file, not command.no_erase)

        if command.cmd == "read_flash":
            cc.read_flash(command.dump_file, command.offset, command.size)

        if command.cmd == "list_filesystem":
            cc.list_filesystem(command.json_output, command.inactive, command.extended)

        if command.cmd == "read_all_files":
            cc.read_all_files(command.local_dir, command.by_file_id)

        if command.cmd == "write_all_files":
            use_api = True
            if not command.image_file is None and not command.output_file is None:
                use_api = False
                cc.copy_input_file_to_output_file()
            cc.write_all_files(command.local_dir, command.simulate, use_api)
            check_fat = True


    if check_fat:
        fat_info = cc.get_fat_info()  # check FAT after each write_file operation
        fat_info.print_sffs_info_short()

    if args.reboot_to_app:
        cc.reboot_to_app()

    log.info("All commands done, bye.")


if __name__ == '__main__':
    main()
