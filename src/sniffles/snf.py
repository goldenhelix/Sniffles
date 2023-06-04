#!/usr/bin/env python3
#
# Sniffles2
# A fast structural variant caller for long-read sequencing data
#
# Created: 26.09.2021
# Author:  Moritz Smolka
# Contact: moritz.g.smolka@gmail.com
#

import pickle
import json
import gzip
import math
from collections import OrderedDict
from typing import Optional

from sniffles import sv


class SNFile:
    header_length: int
    _header: Optional[dict]

    def __init__(self, config, handle, filename=None):
        self.config = config
        self.handle = handle
        self.filename = filename
        self.blocks = {}
        self._header = None
        self._index = {}
        self.total_length = 0

    @property
    def index(self) -> dict:
        return self._index

    @property
    def header(self) -> dict:
        return self._header

    def is_open(self) -> bool:
        return self.handle is not False

    def open(self):
        if self.handle is not False:
            self.close()
        self.handle = open(self.filename, "rb")

    def store(self, svcand):
        block_index = int(svcand.pos / self.config.snf_block_size) * self.config.snf_block_size
        if block_index not in self.blocks:
            self.blocks[block_index] = {svtype: [] for svtype in sv.TYPES}
            self.blocks[block_index]["_COVERAGE"] = {}
        if not self.config.output_rnames:
            svcand.rnames = None
        self.blocks[block_index][svcand.svtype].append(svcand)

    def annotate_block_coverages(self, lead_provider, resolution=500):
        config = self.config
        start_bin = lead_provider.covrtab_min_bin
        end_bin = int(lead_provider.end / config.coverage_binsize) * config.coverage_binsize
        coverage_fwd = 0
        coverage_rev = 0

        coverage_sum = 0
        bin_count = 0

        coverage_binsize_combine = self.config.coverage_binsize_combine
        snf_block_size = config.snf_block_size

        for bin in range(start_bin, end_bin + config.coverage_binsize, config.coverage_binsize):
            if bin in lead_provider.covrtab_fwd:
                coverage_fwd += lead_provider.covrtab_fwd[bin]

            if bin in lead_provider.covrtab_rev:
                coverage_rev += lead_provider.covrtab_rev[bin]

            coverage_sum += coverage_fwd + coverage_rev
            bin_count += 1

            if bin % coverage_binsize_combine == 0:
                block_index = int(bin / snf_block_size) * snf_block_size

                coverage_total_curr = math.ceil(coverage_sum / float(bin_count))
                if coverage_total_curr > 0:
                    if block_index not in self.blocks:
                        self.blocks[block_index] = {svtype: [] for svtype in sv.TYPES}
                        self.blocks[block_index]["_COVERAGE"] = {}

                    self.blocks[block_index]["_COVERAGE"][bin] = coverage_total_curr

                coverage_sum = 0
                bin_count = 0

    def serialize_block(self, block_id):
        return pickle.dumps(self.blocks[block_id])

    def unserialize_block(self, data):
        return pickle.loads(data)

    def write_and_index(self):
        if not self.is_open():
            self.open()
        offset = 0
        for block_id in sorted(self.blocks):
            data = gzip.compress(self.serialize_block(block_id))
            self.handle.write(data)
            data_len = len(data)
            self._index[block_id] = (offset, data_len)
            offset += data_len
            self.total_length += data_len
        if self.config.combine_close_handles:
            self.close()

    def read_header(self):
        if not self.is_open():
            self.open()
        try:
            header_text = self.handle.readline()
            self.header_length = len(header_text)
            self._header = json.loads(header_text.strip())
        except Exception as e:
            print(f"Error when reading SNF header from '{self.filename}': {e}. The file may not be a valid .snf file or could have been corrupted.")
            raise e
        self._index = self._header["index"]
        if self.config.combine_close_handles:
            self.close()

    def read_blocks(self, contig, block_index):
        if not self.is_open():
            self.open()
        block_index = str(block_index)
        if contig not in self.index:
            if self.config.combine_close_handles:
                self.close()
            return None

        if block_index not in self.index[contig]:
            if self.config.combine_close_handles:
                self.close()
            return None

        blocks = []
        for block_data_start, block_data_length in self.index[contig][block_index]:
            try:
                self.handle.seek(self.header_length + block_data_start)
                data = gzip.decompress(self.handle.read(block_data_length))
                blocks.append(self.unserialize_block(data))
            except Exception as e:
                print(f"Error when reading block '{contig}.{block_index}' from '{self.filename}': {e}. The file may not be a valid .snf file or could have been corrupted.")
                if self.config.combine_close_handles:
                    self.close()
                raise e
        if self.config.combine_close_handles:
            self.close()
        return blocks

    def get_index(self):
        return self.index

    def get_total_length(self):
        return self.total_length

    def close(self):
        if self.handle is not False:
            self.handle.close()
            self.handle = False

    def get_all_blocks(self, contig: str):
        blocks = {}
        for block_start in self.index[contig].keys():
            blocks[block_start] = self.read_blocks(contig, block_start)[0]
        return blocks

    def get_full_coverage(self, contig: str):
        coverage = {}
        for b in self.get_all_blocks(contig).values():
            coverage.update(b['_COVERAGE'])
        return coverage


class LazySNFile(SNFile):
    """
    An SNFile, but its header data may be unloaded from memory and lazily loaded back in.

    Important: Do not keep references to index or header outside, otherwise this class will
               massively increase memory usage instead of limiting it.
    """
    _MAX_ACTIVE = 50  # maximum number of objects that will retain their data in memory
    _ACTIVE: OrderedDict['LazySNFile'] = OrderedDict()

    @property
    def index(self) -> dict:
        if self._header is None:
            self.read_header()

        return super().index

    @property
    def header(self) -> dict:
        if self._header is None:
            self.read_header()

        return super().header

    def read_header(self):
        """
        Loads header data for this object, potentially displacing another files' data from memory
        """
        if self in self._ACTIVE:
            self._ACTIVE.move_to_end(self)
        else:
            self._ACTIVE[self] = True

        if len(self._ACTIVE) > self._MAX_ACTIVE:
            oldest_snf, _ = self._ACTIVE.popitem(last=False)
            oldest_snf.unload()

        # SBFile.read_header assumes start of file, so if we have an active handle, reset it there
        if self.handle:
            self.handle.seek(0)

        return super().read_header()

    def unload(self):
        """
        Unloads data for this object (=remove reference to header and index and allow them to be collected by GC).
        """
        if self in self._ACTIVE:
            self._ACTIVE.pop(self)

        self._header = None
        self._index = None
