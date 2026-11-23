import argparse
import os
import struct


BLOCK_SIZE = 256
BLOCK_COUNT = 256
INODE_COUNT = 64
INODE_SIZE = 64
INODE_TABLE_START = 3
INODE_TABLE_BLOCKS = 16
DATA_START = 19
DIRECT_COUNT = 10
INODE_FORMAT = "<BI10II"
INODE_BYTES = struct.calcsize(INODE_FORMAT)
SUPER_FORMAT = "<6I"
MAGIC = 0x544F5946


class FSError(Exception):
    pass


class ToyFS:
    def __init__(self, image_path=None):
        self.image_path = image_path
        if image_path and os.path.exists(image_path):
            self.load(image_path)
        else:
            self.mkfs()

    def mkfs(self):
        self.disk = bytearray(BLOCK_SIZE * BLOCK_COUNT)
        struct.pack_into(
            SUPER_FORMAT,
            self.disk,
            0,
            MAGIC,
            BLOCK_SIZE,
            BLOCK_COUNT,
            INODE_COUNT,
            INODE_TABLE_START,
            DATA_START,
        )

        for block in range(DATA_START):
            self.set_data_bit(block, True)

        self.set_inode_bit(0, True)
        self.write_inode(0, 2, 0, [19] + [0] * 9, 0)
        self.set_data_bit(19, True)
        self.check()

    def load(self, filename):
        with open(filename, "rb") as image:
            data = bytearray(image.read())
        if len(data) != BLOCK_SIZE * BLOCK_COUNT:
            raise FSError("bad image size")
        if struct.unpack_from("<I", data, 0)[0] != MAGIC:
            raise FSError("bad file system image")
        self.disk = data
        self.check()
        self.image_path = filename

    def save(self, filename=None):
        filename = filename or self.image_path
        if not filename:
            raise FSError("no image file")
        with open(filename, "wb") as image:
            image.write(self.disk)
        self.image_path = filename

    def set_inode_bit(self, number, value):
        byte = number // 8
        bit = number % 8
        if value:
            self.disk[BLOCK_SIZE + byte] |= 1 << bit
        else:
            self.disk[BLOCK_SIZE + byte] &= ~(1 << bit)

    def inode_bit(self, number):
        return bool(self.disk[BLOCK_SIZE + number // 8] & (1 << (number % 8)))

    def set_data_bit(self, number, value):
        byte = number // 8
        bit = number % 8
        offset = BLOCK_SIZE * 2 + byte
        if value:
            self.disk[offset] |= 1 << bit
        else:
            self.disk[offset] &= ~(1 << bit)

    def data_bit(self, number):
        offset = BLOCK_SIZE * 2 + number // 8
        return bool(self.disk[offset] & (1 << (number % 8)))

    def free_inode_count(self):
        return sum(not self.inode_bit(i) for i in range(INODE_COUNT))

    def free_data_count(self):
        return sum(not self.data_bit(i) for i in range(DATA_START, BLOCK_COUNT))

    def alloc_inode(self):
        for number in range(1, INODE_COUNT):
            if not self.inode_bit(number):
                self.set_inode_bit(number, True)
                return number
        raise FSError("no free inodes")

    def free_inode(self, number):
        self.set_inode_bit(number, False)
        self.write_inode(number, 0, 0, [0] * DIRECT_COUNT, 0)

    def alloc_block(self):
        for number in range(DATA_START, BLOCK_COUNT):
            if not self.data_bit(number):
                self.set_data_bit(number, True)
                start = number * BLOCK_SIZE
                self.disk[start:start + BLOCK_SIZE] = b"\0" * BLOCK_SIZE
                return number
        raise FSError("disk full")

    def free_block(self, number):
        if number:
            self.set_data_bit(number, False)

    def read_inode(self, number):
        block = INODE_TABLE_START + number // 4
        offset = block * BLOCK_SIZE + (number % 4) * INODE_SIZE
        values = struct.unpack_from(INODE_FORMAT, self.disk, offset)
        return values[0], values[1], list(values[2:12]), values[12]

    def write_inode(self, number, kind, size, direct, indirect):
        block = INODE_TABLE_START + number // 4
        offset = block * BLOCK_SIZE + (number % 4) * INODE_SIZE
        values = struct.pack(INODE_FORMAT, kind, size, *direct, indirect)
        self.disk[offset:offset + INODE_SIZE] = values + b"\0" * (INODE_SIZE - len(values))

    def block_numbers(self, inode):
        kind, size, direct, indirect = self.read_inode(inode)
        numbers = [number for number in direct if number]
        if indirect:
            values = struct.unpack_from("<64I", self.disk, indirect * BLOCK_SIZE)
            numbers.extend(number for number in values if number)
        return numbers

    def inode_block_count(self, inode):
        return len(self.block_numbers(inode))

    def read_data(self, inode):
        kind, size, direct, indirect = self.read_inode(inode)
        data = bytearray()
        for number in self.block_numbers(inode):
            start = number * BLOCK_SIZE
            data.extend(self.disk[start:start + BLOCK_SIZE])
        return bytes(data)

    def write_data(self, inode, data):
        kind, old_size, old_direct, old_indirect = self.read_inode(inode)
        for number in self.block_numbers(inode):
            self.free_block(number)
        if old_indirect:
            self.free_block(old_indirect)

        count = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
        direct = [0] * DIRECT_COUNT
        blocks = []
        for start in range(0, len(data), BLOCK_SIZE):
            number = self.alloc_block()
            blocks.append(number)
            part = data[start:start + BLOCK_SIZE]
            begin = number * BLOCK_SIZE
            self.disk[begin:begin + len(part)] = part

        for index, number in enumerate(blocks[:DIRECT_COUNT]):
            direct[index] = number

        indirect = 0
        if count > DIRECT_COUNT:
            indirect = self.alloc_block()
            pointers = blocks[DIRECT_COUNT:] + [0] * (64 - len(blocks[DIRECT_COUNT:]))
            struct.pack_into("<64I", self.disk, indirect * BLOCK_SIZE, *pointers)

        self.write_inode(inode, kind, len(data), direct, indirect)

    def components(self, path):
        if not path.startswith("/"):
            raise FSError("path must be absolute")
        return [part for part in path.split("/") if part]

    def find_entry(self, directory, name):
        for number, slot, entry_name, inode in self.directory_entries(directory):
            if entry_name == name:
                return slot, inode
        return None

    def resolve(self, path):
        current = 0
        for name in self.components(path):
            kind = self.read_inode(current)[0]
            if kind != 2:
                raise FSError("not a directory")
            entry = self.find_entry(current, name)
            if entry is None:
                raise FSError("not found")
            current = entry[1]
        return current

    def parent(self, path):
        names = self.components(path)
        if not names:
            raise FSError("invalid path")
        name = names.pop()
        current = 0
        for part in names:
            if self.read_inode(current)[0] != 2:
                raise FSError("not a directory")
            entry = self.find_entry(current, part)
            if entry is None:
                raise FSError("not found")
            current = entry[1]
        if self.read_inode(current)[0] != 2:
            raise FSError("not a directory")
        return current, name

    def directory_entries(self, inode):
        for number in self.block_numbers(inode):
            start = number * BLOCK_SIZE
            for slot in range(BLOCK_SIZE // 32):
                offset = start + slot * 32
                raw_name = bytes(self.disk[offset:offset + 28])
                if raw_name[0] == 0:
                    continue
                name = raw_name.split(b"\0", 1)[0].decode()
                child = struct.unpack_from("<I", self.disk, offset + 28)[0]
                yield number, slot, name, child

    def directory_slot(self, inode):
        for number in self.block_numbers(inode):
            start = number * BLOCK_SIZE
            for slot in range(BLOCK_SIZE // 32):
                offset = start + slot * 32
                if self.disk[offset] == 0:
                    return number, slot
        return None

    def add_entry(self, directory, name, child):
        encoded = name.encode()
        if len(encoded) > 28:
            raise FSError("name too long")
        place = self.directory_slot(directory)
        kind, size, direct, indirect = self.read_inode(directory)
        if place is None:
            if len(self.block_numbers(directory)) >= DIRECT_COUNT:
                raise FSError("directory full")
            number = self.alloc_block()
            for index in range(DIRECT_COUNT):
                if direct[index] == 0:
                    direct[index] = number
                    break
            self.write_inode(directory, kind, size, direct, indirect)
            place = (number, 0)

        number, slot = place
        offset = number * BLOCK_SIZE + slot * 32
        self.disk[offset:offset + 28] = encoded + b"\0" * (28 - len(encoded))
        struct.pack_into("<I", self.disk, offset + 28, child)
        self.write_inode(directory, kind, size + 32, direct, indirect)

    def remove_entry(self, directory, name):
        entry = self.find_entry(directory, name)
        if entry is None:
            raise FSError("not found")
        number, slot, unused, child = next(
            item for item in self.directory_entries(directory) if item[2] == name
        )
        offset = number * BLOCK_SIZE + slot * 32
        self.disk[offset:offset + 32] = b"\0" * 32
        kind, size, direct, indirect = self.read_inode(directory)
        self.write_inode(directory, kind, max(0, size - 32), direct, indirect)
        return child

    def mkdir(self, path):
        parent, name = self.parent(path)
        if not name or len(name.encode()) > 28:
            raise FSError("name too long")
        if self.find_entry(parent, name) is not None:
            raise FSError("exists")
        if self.free_inode_count() < 1 or self.free_data_count() < 1:
            raise FSError("disk full")
        if self.directory_slot(parent) is None and len(self.block_numbers(parent)) >= DIRECT_COUNT:
            raise FSError("directory full")

        inode = self.alloc_inode()
        block = self.alloc_block()
        self.write_inode(inode, 2, 0, [block] + [0] * 9, 0)
        self.add_entry(parent, name, inode)
        self.check()

    def write(self, path, text):
        data = text.encode()
        if len(data) > (DIRECT_COUNT + 64) * BLOCK_SIZE:
            raise FSError("file too large")
        parent, name = self.parent(path)
        if len(name.encode()) > 28:
            raise FSError("name too long")
        found = self.find_entry(parent, name)
        needed = (len(data) + BLOCK_SIZE - 1) // BLOCK_SIZE
        if needed > DIRECT_COUNT:
            needed += 1

        if found is None:
            if self.free_inode_count() < 1:
                raise FSError("no free inodes")
            if self.directory_slot(parent) is None and len(self.block_numbers(parent)) >= DIRECT_COUNT:
                raise FSError("directory full")
            inode = -1
        else:
            inode = found[1]
            if self.read_inode(inode)[0] != 1:
                raise FSError("not a file")
            needed -= len(self.block_numbers(inode))
            if self.read_inode(inode)[3]:
                needed -= 1

        if self.free_data_count() < max(needed, 0):
            raise FSError("disk full")

        if inode == -1:
            inode = self.alloc_inode()
            self.write_inode(inode, 1, 0, [0] * 10, 0)
            self.add_entry(parent, name, inode)
        self.write_data(inode, data)
        self.check()

    def read(self, path):
        inode = self.resolve(path)
        if self.read_inode(inode)[0] != 1:
            raise FSError("not a file")
        return self.read_data(inode).decode(errors="replace")

    def rm(self, path):
        parent, name = self.parent(path)
        found = self.find_entry(parent, name)
        if found is None:
            raise FSError("not found")
        inode = found[1]
        kind, size, direct, indirect = self.read_inode(inode)
        if kind == 2 and any(True for _ in self.directory_entries(inode)):
            raise FSError("directory not empty")
        if kind not in (1, 2):
            raise FSError("not found")

        for number in self.block_numbers(inode):
            self.free_block(number)
        self.free_inode(inode)
        self.remove_entry(parent, name)
        self.check()

    def ls(self, path):
        inode = self.resolve(path)
        if self.read_inode(inode)[0] != 2:
            raise FSError("not a directory")
        result = []
        for number, slot, name, child in self.directory_entries(inode):
            kind, size, direct, indirect = self.read_inode(child)
            result.append((name + "/" if kind == 2 else name, size))
        return result

    def stat(self, path):
        inode = self.resolve(path)
        kind, size, direct, indirect = self.read_inode(inode)
        return {
            "type": "directory" if kind == 2 else "file",
            "inode": inode,
            "size": size,
            "blocks": self.block_numbers(inode),
        }

    def fsstat(self):
        used_inodes = 0
        files = 0
        directories = 0
        for number in range(INODE_COUNT):
            if self.inode_bit(number):
                used_inodes += 1
                kind = self.read_inode(number)[0]
                if kind == 1:
                    files += 1
                elif kind == 2:
                    directories += 1
        used_blocks = sum(self.data_bit(number) for number in range(DATA_START, BLOCK_COUNT))
        return {
            "inodes_used": used_inodes,
            "inodes_free": INODE_COUNT - used_inodes,
            "data_used": used_blocks,
            "data_free": BLOCK_COUNT - DATA_START - used_blocks,
            "files": files,
            "directories": directories,
        }

    def check(self):
        reachable = set(range(DATA_START))
        for inode_number in range(INODE_COUNT):
            if not self.inode_bit(inode_number):
                continue
            kind, size, direct, indirect = self.read_inode(inode_number)
            if kind not in (1, 2):
                raise FSError("bad inode")
            for number in self.block_numbers(inode_number):
                if number < DATA_START or number >= BLOCK_COUNT or number in reachable:
                    raise FSError("bad block reference")
                reachable.add(number)
            if indirect:
                if indirect < DATA_START or indirect >= BLOCK_COUNT or indirect in reachable:
                    raise FSError("bad indirect block")
                reachable.add(indirect)

        for number in range(BLOCK_COUNT):
            if self.data_bit(number) != (number in reachable):
                raise FSError("bitmap mismatch")

        for number in range(INODE_COUNT):
            if not self.inode_bit(number) and self.read_inode(number)[0] != 0:
                raise FSError("free inode has data")
        if not self.inode_bit(0) or self.read_inode(0)[0] != 2:
            raise FSError("root is missing")


def print_stats(fs):
    values = fs.fsstat()
    print(
        f"inodes {values['inodes_used']}/{INODE_COUNT} used "
        f"({values['inodes_free']} free)"
    )
    print(
        f"data blocks {values['data_used']}/{BLOCK_COUNT - DATA_START} used "
        f"({values['data_free']} free)"
    )
    print(f"files {values['files']} dirs {values['directories']}")


def command_loop(fs, image):
    while True:
        try:
            line = input("fs> ").strip()
        except EOFError:
            fs.save(image)
            break
        if not line:
            continue
        parts = line.split(maxsplit=2)
        command = parts[0]
        try:
            changed = False
            if command == "mkdir" and len(parts) == 2:
                fs.mkdir(parts[1])
                changed = True
            elif command == "ls" and len(parts) == 2:
                for name, size in fs.ls(parts[1]):
                    print(f"{name} {size}")
            elif command == "write" and len(parts) == 3:
                fs.write(parts[1], parts[2])
                changed = True
            elif command == "cat" and len(parts) == 2:
                print(fs.read(parts[1]), end="")
                print()
            elif command == "rm" and len(parts) == 2:
                fs.rm(parts[1])
                changed = True
            elif command == "stat" and len(parts) == 2:
                value = fs.stat(parts[1])
                print(
                    f"{value['type']} inode {value['inode']} size {value['size']} "
                    f"blocks {value['blocks']}"
                )
            elif command == "fsstat" and len(parts) == 1:
                print_stats(fs)
            elif command == "save" and len(parts) in (1, 2):
                fs.save(parts[1] if len(parts) == 2 else image)
            elif command == "load" and len(parts) == 2:
                fs.load(parts[1])
            elif command in ("exit", "quit") and len(parts) == 1:
                fs.save(image)
                break
            else:
                print("error: invalid command")
                continue
            if changed:
                fs.save(image)
        except (FSError, UnicodeError) as error:
            print(f"error: {error}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="disk.img")
    args = parser.parse_args()
    try:
        fs = ToyFS(args.image)
        command_loop(fs, args.image)
    except FSError as error:
        print(f"error: {error}")


if __name__ == "__main__":
    main()
