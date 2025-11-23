import argparse
import json
import re
import struct
import sys

#
# Helpers
#

def safe_chr(i):
    return chr(i) if i >= 32 else "."

def decode_lvl_color(color):
    '''Returns (A, R, G, B)'''
    return ((color >> 24) & 0xff,
            (color >> 16) & 0xff,
            (color >> 8) & 0xff,
            color & 0xff)

#
# Dumper
#

class Dumper:
    def __init__(self, verbose=False, level=0):
        self.verbose = verbose
        self.level = level

    def print(self, text):
        print('  ' * self.level + text)

    def inner(self):
        return Dumper(self.verbose, self.level + 1)

#
# Slicer
#

class Slicer:
    def __init__(self, data, begin=0, end=None):
        self.data = data
        self.begin = begin
        self.end = len(data) if end is None else end

    def length(self):
        return self.end - self.begin

    def take(self, length):
        if self.begin + length > self.end:
            raise ValueError('Not enough data')
        chunk = self.data[self.begin : self.begin + length]
        self.begin += length
        return chunk

    def take_byte(self):
        return self.take(1)[0]

    def slice(self, length):
        if self.begin + length > self.end:
            raise ValueError('Not enough data')
        part = Slicer(self.data, self.begin, self.begin + length)
        self.begin += length
        return part

    def unpack(self, strukt):
        return strukt.unpack(self.take(strukt.size))

#
# Screen
#

class Screen:
    TRANSPARENT_BACKGROUND = 255
    TRANSPARENT_TILE = 32

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.background = bytearray(width * height)
        self.foreground = bytearray(width * height)
        self.tag = bytearray(width * height)
        self.tile = bytearray(width * height)

    def count(self):
        return self.width * self.height

    def index(self, x, y):
        return y * self.width + x

    def get(self, index):
        return self.background[index], self.foreground[index], self.tag[index], self.tile[index]

    def get_text(self, index, length):
        return bytes(self.tile[index : index + length])

    def set(self, index, background, foreground, tag, tile):
        self.background[index] = background
        self.foreground[index] = foreground
        self.tag[index] = tag
        self.tile[index] = tile

    def apply(self, other):
        for index in range(self.count()):
            if other.background[index] != self.TRANSPARENT_BACKGROUND:
                # Opaque background
                self.background[index] = other.background[index]
                self.foreground[index] = other.foreground[index]
                self.tag[index] = other.tag[index]
                self.tile[index] = other.tile[index]
            elif other.tile[index] != self.TRANSPARENT_TILE:
                # Non-transparent tile with transparent background
                self.foreground[index] = other.foreground[index]
                self.tag[index] = other.tag[index]
                self.tile[index] = other.tile[index]

    @classmethod
    def from_lvl(cls, tree, width, height, tag):
        screen = cls(width, height)

        if len(tree['data']) != height:
            raise ValueError(f"Expected {height} rows, got {len(tree['data'])}")
        for y, row in enumerate(tree['data']):
            if len(row) != width:
                raise ValueError(f"Expected {width} columns, got {len(row)}")
            for x, col in enumerate(row):
                # lvllvl uses -1 for transparency, -1 & 0xff is 0xff
                screen.set(screen.index(x, y), col['bc'] & 0xff, col['fc'], tag, col['t'])

        return screen

class ScreenWriter:
    def __init__(self, screen):
        self.screen = screen
        self.index = 0
        self.background = None
        self.foreground = None
        self.tag = 0

    def set_transparent(self):
        self.background = Screen.TRANSPARENT_BACKGROUND

    def set_background(self, color):
        self.background = color

    def set_foreground(self, color):
        self.foreground = color

    def set_tag(self, tag):
        self.tag = tag

    def skip(self, count):
        self.index += count

    def write(self, tile):
        if self.background is None:
            raise ValueError('Background color not set')
        if self.foreground is None:
            raise ValueError('Foreground color not set')
        self.screen.set(self.index, self.background, self.foreground, self.tag, tile)
        self.index += 1

class ScreenAnalyzer:
    def __init__(self, screen, prev_screen=None):
        self.screen = screen
        self.prev_screen = prev_screen
        self.analyze()

    def analyze(self):
        self.skip_len = [0 for i in range(self.screen.count())]
        self.fill_len = [0 for i in range(self.screen.count())]
        self.run_len = [0 for i in range(self.screen.count())]

        skip_count = 0
        fill_count = 1
        run_count = 1
        for i in range(self.screen.count()):
            # Work backwards
            index = (self.screen.count() - 1) - i

            if self.prev_screen and self.prev_screen.get(index) == self.screen.get(index):
                # Unchanged cell, increase count
                skip_count += 1
            else:
                # Changed cell, reset count
                skip_count = 0
            if i:
                # Compare this cell with next
                background_a, foreground_a, tag_a, tile_a = self.screen.get(index)
                background_b, foreground_b, tag_b, tile_b = self.screen.get(index + 1)
                if background_a == background_b and foreground_a == foreground_b and tag_a == tag_b:
                    # Unchanged properties, can be part of same run
                    run_count += 1
                    if tile_a == tile_b:
                        # Also unchanged tile, can be part of same fill
                        fill_count += 1
                    else:
                        # Changed tile, breaks fill
                        fill_count = 1
                else:
                    # Changed properties, breaks runs and fills
                    run_count = 1
                    fill_count = 1
            else:
                # Last cell, can't compare to next
                run_count = 1
                fill_count = 1

            # Backward-looking counters become forward-looking lengths
            self.skip_len[index] = skip_count
            self.fill_len[index] = fill_count
            self.run_len[index] = run_count

    def commands(self):
        commands = []
        index = 0
        last_background = None
        last_foreground = None
        last_tag = 0

        while index < self.screen.count():
            background, foreground, tag, tile = self.screen.get(index)

            if self.skip_len[index]:
                count = min(self.skip_len[index], 4096)
                command = LVSCommandSkip(count - 1)
            elif self.fill_len[index] >= 3:
                count = min(self.fill_len[index], 4096)
                command = LVSCommandFill(count - 1, tile)
            else:
                count = min(self.run_len[index], 4096)
                # Look for skip or fill opportunity inside run
                for i in range(1, count):
                    if self.skip_len[index + i] >= 3 or self.fill_len[index + i] >= 3:
                        # Cut run short for skip
                        count = i
                        break
                command = LVSCommandRun(self.screen.get_text(index, count))
            index += count

            # Make sure properties are up to date before adding tile command
            if not isinstance(command, LVSCommandSkip):
                if background != last_background:
                    if background == Screen.TRANSPARENT_BACKGROUND:
                        commands.append(LVSCommandTransparent())
                    else:
                        commands.append(LVSCommandBackground(background))
                    last_background = background
                if foreground != last_foreground:
                    commands.append(LVSCommandForeground(foreground))
                    last_foreground = foreground
                if tag != last_tag:
                    commands.append(LVSCommandTag(tag))
                    last_tag = tag

            commands.append(command)

        return commands

#
# LVS classes
#

def lvs_decode(slicer, subtypes):
    chunks = []

    while slicer.length():
        signature, length = slicer.unpack(LVSChunk.CHUNK_STRUCT)
        body_length = length - LVSChunk.CHUNK_STRUCT.size
        if body_length & 1:
            raise ValueError('Chunk body has odd length')

        chunk = None
        for subtype in subtypes:
            if signature == subtype.SIGNATURE:
                chunk = subtype.decode(slicer.slice(body_length))
                break
        if not chunk:
            raise ValueError(f'Unexpected chunk signature {signature}')
        chunks.append(chunk)

    return chunks

class LVSFile():
    def __init__(self, palette, tileset, animations):
        self.palette = palette
        self.tileset = tileset
        self.animations = animations

    def validate(self):
        if self.palette:
            self.palette.validate()
        if self.tileset:
            self.tileset.validate()
        for anim in self.animations:
            anim.validate(self)

    def to_bytes(self):
        data = bytes()
        if self.palette:
            data += self.palette.to_bytes()
        if self.tileset:
            data += self.tileset.to_bytes()
        for anim in self.animations:
            data += anim.to_bytes()
            for frame in anim.frames:
                data += frame.to_bytes()
        return data

    def dump(self, dumper):
        if self.palette:
            dumper.print('Palette:')
            self.palette.dump(dumper.inner())
        if self.tileset:
            dumper.print('Tileset:')
            self.tileset.dump(dumper.inner())
        for i, anim in enumerate(self.animations):
            dumper.print(f'Animation {i}:')
            anim.dump(dumper.inner())

    def export_gif(self, path, scale):
        try:
            from PIL import Image
        except:
            print('PIL not found, install with "pip install pillow"')
            sys.exit(1)

        if not self.palette:
            raise ValueError(f'Palette not found')
        if not self.tileset:
            raise ValueError(f'Tileset not found')
        if not self.animations:
            raise ValueError(f'No animation found')

        anim = self.animations[0]
        scaled_cell_width = scale * self.tileset.square_width
        scaled_cell_height = scale * self.tileset.square_height
        screen = Screen(anim.width, anim.height)
        images = []
        for frame in anim.frames:
            image = Image.new('P', (anim.width * scaled_cell_width,
                                    anim.height * scaled_cell_height))
            image.putpalette(self.palette.colors_to_bytes())
            writer = ScreenWriter(screen)
            for command in frame.commands:
                command.execute(writer)
            for row in range(anim.height):
                for column in range(anim.width):
                    background, foreground, _, tile = screen.get(screen.index(column, row))
                    background = 0 if (background == Screen.TRANSPARENT_BACKGROUND) else background
                    tile = self.tileset.get(tile)
                    for cell_y in range(scaled_cell_height):
                        tile_y = (cell_y * self.tileset.height) // scaled_cell_height
                        for cell_x in range(scaled_cell_width):
                            tile_x = (cell_x * self.tileset.width) // scaled_cell_width
                            color = foreground if tile.get(tile_x, tile_y) else background
                            image.putpixel((column * scaled_cell_width + cell_x, row * scaled_cell_height + cell_y), color)
            images.append(image)

        images[0].save(
            path,
            save_all=True,
            append_images=images[1:],
            duration=[f.duration * 20 for f in anim.frames],
            loop=anim.loops
        )

    @classmethod
    def from_lvl(cls, tree, tile_size=None):
        if tree['version'] != 1:
            raise ValueError(f"Unexpected version: {tree['version']}")

        if 'colorPalette' in tree:
            palette = LVSPalette.from_lvl(tree['colorPalette'])
        else:
            palette = None

        if 'tileSet' in tree:
            tileset = LVSTileset.from_lvl(tree['tileSet'], tile_size)
        else:
            tileset = None

        if 'tileMap' in tree:
            animations = [LVSAnimation.from_lvl(tree['tileMap'])]
        else:
            animations = []

        return cls(palette, tileset, animations)

    @classmethod
    def from_bytes(cls, data):
        palette = None
        tileset = None
        animations = []
        slicer = Slicer(data)

        for chunk in lvs_decode(slicer, [LVSPalette, LVSTileset, LVSAnimation, LVSFrame]):
            if isinstance(chunk, LVSPalette):
                if palette:
                    raise ValueError('More than one palette')
                palette = chunk
            elif isinstance(chunk, LVSTileset):
                if tileset:
                    raise ValueError('More than one tileset')
                tileset = chunk
            elif isinstance(chunk, LVSAnimation):
                animations.append(chunk)
            elif isinstance(chunk, LVSFrame):
                if not animations:
                    raise ValueError('Frame without animation')
                animations[-1].frames.append(chunk)
            else:
                raise ValueError('Unexpected chunk: {type(chunk)}')

        return cls(palette, tileset, animations)

class LVSChunk:
    '''Base class, do not instantiate'''

    CHUNK_STRUCT = struct.Struct('> 2s H')

    def body_to_bytes(self):
        raise NotImplementedError()

    def to_bytes(self):
        body = self.body_to_bytes()

        # Add padding if odd size
        if len(body) & 1:
            body += b'\x00'

        return self.CHUNK_STRUCT.pack(self.SIGNATURE, self.CHUNK_STRUCT.size + len(body)) + body

class LVSPalette(LVSChunk):
    SIGNATURE = b'P1'
    STRUCT = struct.Struct('B')

    def __init__(self, bitplanes, colors):
        self.bitplanes = bitplanes
        self.colors = colors

    def maximum_color(self):
        return (1 << self.bitplanes) - 1

    def validate(self):
        if self.bitplanes < 1 or self.bitplanes > 5:
            raise ValueError(f'Illegal number of bitplanes: {self.bitplanes}')
        if len(self.colors) != (1 << self.bitplanes):
            raise ValueError(f'Expected {1 << self.bitplanes} colors, got {len(self.colors)}')

    def body_to_bytes(self):
        return self.STRUCT.pack(self.bitplanes) + self.colors_to_bytes()

    def colors_to_bytes(self):
        return b''.join(color.to_bytes() for color in self.colors)

    def dump(self, dumper):
        dumper.print(f'Bitplanes: {self.bitplanes}')
        if dumper.verbose:
            for i, color in enumerate(self.colors):
                dumper.print(f'Color {i}:')
                color.dump(dumper.inner())

    @classmethod
    def from_lvl(cls, tree):
        if tree['version'] != 1:
            raise ValueError(f"Unexpected version: {tree['version']}")
        bitplanes = {2: 1,
                     4: 2,
                     8: 3,
                     16: 4,
                     32: 5}.get(len(tree['data']))
        if not bitplanes:
            raise ValueError(f"Unsupported number of colors: {len(tree['data'])}")
        return cls(bitplanes, [LVSColor.from_lvl(col) for col in tree['data']])

    @classmethod
    def decode(cls, slicer):
        bitplanes = slicer.unpack(cls.STRUCT)[0]
        colors = [LVSColor.decode(slicer) for i in range(1 << bitplanes)]
        return cls(bitplanes, colors)

class LVSColor:
    STRUCT = struct.Struct('B B B')

    def __init__(self, red, green, blue):
        self.red = red
        self.green = green
        self.blue = blue

    def to_bytes(self):
        return self.STRUCT.pack(self.red, self.green, self.blue)

    def dump(self, dumper):
        dumper.print(f'Red: {self.red}, Green: {self.green}, Blue: {self.blue}')

    @classmethod
    def from_lvl(cls, col):
        _, r, g, b = decode_lvl_color(col)
        return cls(r, g, b)

    @classmethod
    def decode(cls, slicer):
        return cls(*slicer.unpack(cls.STRUCT))

class LVSTileset(LVSChunk):
    SIGNATURE = b'T1'
    STRUCT = struct.Struct('B B B B B B B')

    def __init__(self, width, height, square_width, square_height, stride, first, last, tiles):
        self.width = width
        self.height = height
        self.square_width = square_width
        self.square_height = square_height
        self.stride = stride
        self.first = first
        self.last = last
        self.tiles = tiles

    def validate(self):
        if self.width < 1:
            raise ValueError(f'Illegal tileset width: {self.width}')
        if self.height < 1:
            raise ValueError(f'Illegal tileset height: {self.height}')
        if self.square_width < 1:
            raise ValueError(f'Illegal tileset square_width: {self.square_width}')
        if self.square_height < 1:
            raise ValueError(f'Illegal tileset square_height: {self.square_height}')
        if self.stride < 1:
            raise ValueError(f'Illegal tileset stride: {self.stride}')
        if self.stride * 8 < self.width:
            raise ValueError(f'Tileset stride {self.stride} too small for width {self.width}')
        if self.first < 0 or self.first > 255:
            raise ValueError(f'Illegal tileset first index: {self.first}')
        if self.last < 0 or self.last > 255 or self.last < self.first:
            raise ValueError(f'Illegal tileset last index: {self.last}')
        if len(self.tiles) != self.last + 1 - self.first:
            raise ValueError(f'Expected {self.last + 1 - self.first} tiles, got {len(self.tiles)}')

    def body_to_bytes(self):
        return (self.STRUCT.pack(self.width, self.height, self.square_width, self.square_height, self.stride, self.first, self.last) +
                b''.join(tile.to_bytes() for tile in self.tiles))

    def dump(self, dumper):
        dumper.print(f'Width: {self.width}')
        dumper.print(f'Height: {self.height}')
        dumper.print(f'Square width: {self.square_width}')
        dumper.print(f'Square height: {self.square_height}')
        dumper.print(f'Stride: {self.stride}')
        dumper.print(f'First: {self.first}')
        dumper.print(f'Last: {self.last}')
        if dumper.verbose:
            for i, tile in enumerate(self.tiles):
                dumper.print(f'Tile {self.first + i}:')
                tile.dump(dumper.inner())

    def get(self, code):
        return self.tiles[code - self.first]

    @classmethod
    def from_lvl(cls, tree, tile_size=None):
        if tree['version'] != 1:
            raise ValueError(f"Unexpected version: {tree['version']}")
        original_width = tree['width']
        original_height = tree['height']
        if tile_size:
            width = tile_size[0]
            height = tile_size[1]
        else:
            width = original_width
            height = original_height
        num_tiles = len(tree['tiles'])
        if num_tiles != 256:
            raise ValueError(f"Unexpected number of tiles: {num_tiles}")
        first = 0
        last = 255
        stride = (width + 7) // 8
        tiles = [LVSTile.from_lvl(tile, width, height, original_width, original_height, stride) for tile in tree['tiles']]
        return cls(width, height, original_width, original_height, stride, first, last, tiles)

    @classmethod
    def decode(cls, slicer):
        width, height, square_width, square_height, stride, first, last = slicer.unpack(cls.STRUCT)
        tiles = [LVSTile.decode(slicer, stride, height) for i in range(last + 1 - first)]
        return cls(width, height, square_width, square_height, stride, first, last, tiles)

class LVSTile:
    def __init__(self, stride, height, data=None):
        self.stride = stride
        self.height = height
        if data:
            self.data = data
        else:
            self.data = bytearray(stride * height)

    def to_bytes(self):
        return bytes(self.data)

    def dump(self, dumper):
        lookup = ['.', '#']
        for y in range(self.height):
            dumper.print(''.join(lookup[self.get(x, y)] for x in range(self.stride * 8)))

    def get(self, x, y):
        return (self.data[y * self.stride + x // 8] >> (7 - (x & 7))) & 1

    def set(self, x, y, bit):
        self.data[y * self.stride + x // 8] |= bit << (7 - (x & 7))

    @classmethod
    def from_lvl(cls, tree, width, height, original_width, original_height, stride):
        tile = cls(stride, height)
        for y in range(height):
            orig_y = (y * original_height) // height
            for x in range(width):
                orig_x = (x * original_width) // width
                tile.set(x, y, tree['data'][0][orig_y * width + orig_x])
        return tile

    @classmethod
    def decode(cls, slicer, stride, height):
        return cls(stride, height, slicer.take(stride * height))

class LVSAnimation(LVSChunk):
    SIGNATURE = b'A1'
    STRUCT = struct.Struct('B B B')

    def __init__(self, width, height, loops, frames):
        self.width = width
        self.height = height
        self.loops = loops
        self.frames = frames

    def num_cells(self):
        return self.width * self.height

    def validate(self, file):
        if self.width < 1:
            raise ValueError(f'Illegal width: {self.width}')
        if self.height < 1:
            raise ValueError(f'Illegal height: {self.height}')
        if self.loops < 0:
            raise ValueError(f'Illegal number of loops: {self.loops}')
        for frame in self.frames:
            frame.validate(file, self)

    def body_to_bytes(self):
        return self.STRUCT.pack(self.width, self.height, self.loops)

    def dump(self, dumper):
        dumper.print(f'Width: {self.width}')
        dumper.print(f'Height: {self.height}')
        dumper.print(f'Loops: {self.loops}')
        if dumper.verbose:
            for i, frame in enumerate(self.frames):
                dumper.print(f'Frame {i}:')
                frame.dump(dumper.inner())
        else:
            dumper.print(f'Frames: {len(self.frames)}')

    @classmethod
    def from_lvl(cls, tree):
        if not tree['layers']:
            raise ValueError(f"No layers")

        # Gather information from layers
        for i, layer in enumerate(tree['layers']):
            if len(layer['frames']) != len(tree['frames']):
                raise ValueError(f"Expected {len(tree['frames'])} frames in layer {i}, got {len(layer['frames'])}")
            if i:
                if layer['gridWidth'] != width:
                    raise ValueError(f"Expected gridWidth {width} in layer {i}, got {layer['gridWidth']}")
                if layer['gridHeight'] != height:
                    raise ValueError(f"Expected gridHeight {height} in layer {i}, got {layer['gridHeight']}")
            else:
                width = layer['gridWidth']
                height = layer['gridHeight']

        frames = []
        prev_screen = None

        # Render frames and create commands
        for i, frame in enumerate(tree['frames']):
            duration = frame['duration']

            screen = None

            # Render layers into screen
            for j, layer in enumerate(tree['layers']):
                layer_frame = layer['frames'][i]
                # bg_color = layer_frame['bgColor']
                # border_color = layer_frame['borderColor']
                match = re.fullmatch(r' *tag +(\d+) *', layer['label'], re.IGNORECASE)
                if match:
                    tag = int(match.group(1)) & 0x1f
                else:
                    tag = 0

                layer_screen = Screen.from_lvl(layer_frame, width, height, tag)
                if j:
                    # Transparent layer
                    screen.apply(layer_screen)
                else:
                    # Base layer
                    screen = layer_screen

            # Construct commands from screen and previous screen
            analyzer = ScreenAnalyzer(screen, prev_screen)
            frames.append(LVSFrame(duration, analyzer.commands()))

            prev_screen = screen

        return cls(width, height, 0, frames)

    @classmethod
    def decode(cls, slicer):
        width, height, loops = slicer.unpack(cls.STRUCT)
        return cls(width, height, loops, [])

class LVSFrame(LVSChunk):
    SIGNATURE = b'F1'
    STRUCT = struct.Struct('> H')

    def __init__(self, duration, commands):
        self.duration = duration
        self.commands = commands

    def validate(self, file, animation):
        if self.duration < 1:
            raise ValueError(f'Illegal frame duration: {self.duration}')
        index = 0
        for cmd in self.commands:
            cmd.validate(file)
            index += cmd.cells()
        if index != animation.num_cells():
            raise ValueError(f'Frame encodes {index} cells, expected {animation.num_cells()}')

    def body_to_bytes(self):
        return (self.STRUCT.pack(self.duration) +
                b''.join(cmd.to_bytes() for cmd in self.commands))

    def dump(self, dumper):
        dumper.print(f'Duration: {self.duration}')
        if dumper.verbose:
            dumper.print(f'Commands:')
            for cmd in self.commands:
                cmd.dump(dumper.inner())

    @classmethod
    def decode(cls, slicer):
        duration = slicer.unpack(cls.STRUCT)[0]
        commands = lvs_command_decode(slicer)
        return cls(duration, commands)

def lvs_command_decode(slicer):
    commands = []

    while slicer.length():
        header = slicer.take_byte()
        opcode = header >> 5
        value = header & 0x1f

        if opcode == LVSCommandTransparent.OPCODE:
            commands.append(LVSCommandTransparent())
        elif opcode == LVSCommandBackground.OPCODE:
            commands.append(LVSCommandBackground(value))
        elif opcode == LVSCommandForeground.OPCODE:
            commands.append(LVSCommandForeground(value))
        elif opcode == LVSCommandTag.OPCODE:
            commands.append(LVSCommandTag(value))
        elif opcode == LVSCommandSkip.OPCODE:
            if value & 0x10:
                value = (value & 0x0f) << 8 | slicer.take_byte()
            commands.append(LVSCommandSkip(value))
        elif opcode == LVSCommandFill.OPCODE:
            if value & 0x10:
                value = (value & 0x0f) << 8 | slicer.take_byte()
            commands.append(LVSCommandFill(value, slicer.take_byte()))
        elif opcode == LVSCommandRun.OPCODE:
            if value & 0x10:
                value = (value & 0x0f) << 8 | slicer.take_byte()
            commands.append(LVSCommandRun(slicer.take(value + 1)))
        else:
            raise ValueError(f'Opcode {opcode} not supported')

    return commands

class LVSCommand:
    def validate(self, file):
        raise NotImplementedError()

    def cells(self):
        raise NotImplementedError()

    def dump(self, dumper):
        raise NotImplementedError()

    def execute(self, writer):
        raise NotImplementedError()

    @classmethod
    def validate_color(cls, color, file):
        if file.palette and color > file.palette.maximum_color():
            raise ValueError(f'Color {color} exceeds maximum {file.palette.maximum_color()}')

    @classmethod
    def validate_tiles(cls, data, file):
        for tile in data:
            if file.tileset and (tile < file.tileset.first or tile > file.tileset.last):
                raise ValueError(f'Tile {tile} outside range [{tileset.first}, {tileset.last}] of tileset')

class LVSShortCommand(LVSCommand):
    def __init__(self, value):
        self.value = value

    def to_bytes(self):
        if self.value < 0x20:
            return struct.pack('B', self.OPCODE << 5 | self.value)
        else:
            raise ValueError(f'Too large value: {self.value}')

class LVSLongCommand(LVSCommand):
    def __init__(self, value, data):
        self.value = value
        self.data = data

    def to_bytes(self):
        if self.value < 0x10:
            return struct.pack('B', self.OPCODE << 5 | self.value) + self.data
        elif self.value < 0x1000:
            return struct.pack('B B', self.OPCODE << 5 | 0x10 | self.value >> 8, self.value & 0xff) + self.data
        else:
            raise ValueError(f'Too large value: {self.value}')

class LVSCommandTransparent(LVSShortCommand):
    OPCODE = 0

    def __init__(self):
        super().__init__(0)

    def validate(self, file):
        pass

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print('Transparent')

    def execute(self, writer):
        writer.set_transparent()

class LVSCommandBackground(LVSShortCommand):
    OPCODE = 1

    def __init__(self, color):
        super().__init__(color)

    def validate(self, file):
        self.validate_color(self.value, file)

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print(f'Background ({self.value})')

    def execute(self, writer):
        writer.set_background(self.value)

class LVSCommandForeground(LVSShortCommand):
    OPCODE = 2

    def __init__(self, color):
        super().__init__(color)

    def validate(self, file):
        self.validate_color(self.value, file)

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print(f'Foreground ({self.value})')

    def execute(self, writer):
        writer.set_foreground(self.value)

class LVSCommandTag(LVSShortCommand):
    OPCODE = 3

    def __init__(self, tag):
        super().__init__(tag)

    def validate(self, file):
        pass

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print(f'Tag ({self.value})')

    def execute(self, writer):
        writer.set_tag(self.value)

class LVSCommandSkip(LVSLongCommand):
    OPCODE = 4

    def __init__(self, count):
        super().__init__(count, bytes())

    def validate(self, file):
        pass

    def cells(self):
        return self.value + 1

    def dump(self, dumper):
        dumper.print(f'Skip (Count: {self.value})')

    def execute(self, writer):
        writer.skip(self.value + 1)

class LVSCommandFill(LVSLongCommand):
    OPCODE = 5

    def __init__(self, count, tile):
        super().__init__(count, bytes([tile]))

    def validate(self, file):
        self.validate_tiles(self.data, file)

    def cells(self):
        return self.value + 1

    def dump(self, dumper):
        dumper.print(f'Fill (Count: {self.value}, Tile: {self.data[0]}, Text: "{safe_chr(self.data[0])}")')

    def execute(self, writer):
        for i in range(self.value + 1):
            writer.write(self.data[0])

class LVSCommandRun(LVSLongCommand):
    OPCODE = 6

    def __init__(self, data):
        if not data:
            raise ValueError(f'No data')
        super().__init__(len(data) - 1, data)

    def validate(self, file):
        self.validate_tiles(self.data, file)

    def cells(self):
        return len(self.data)

    def dump(self, dumper):
        dumper.print(f'Run (Tiles: {str(list(self.data))}, Text: "{"".join(safe_chr(c) for c in self.data)}")')

    def execute(self, writer):
        for tile in self.data:
            writer.write(tile)

#
# LVL parsing
#

def lvl_dump(tree, verbose):
    print('Version:', tree['version'])

    if 'tileSet' in tree:
        print('Tileset:')
        print('  Version:', tree['tileSet']['version'])
        print('  Width:', tree['tileSet']['width'])
        print('  Height:', tree['tileSet']['height'])
        print('  Number of tiles:', len(tree['tileSet']['tiles']))
        if verbose:
            w = tree['tileSet']['width']
            h = tree['tileSet']['height']
            print('  Tiles:')
            for i, tile in enumerate(tree['tileSet']['tiles']):
                print(f'    Index: {i} ({safe_chr(i)})')
                for y in range(h):
                    line = ''
                    for x in range(w):
                        bit = tile['data'][0][y * w + x]
                        line += '#' if bit else '.'
                    print(f'    {line}')
    else:
        print('No tileset found!')

    if 'colorPalette' in tree:
        print('Palette:')
        print('  Version:', tree['colorPalette']['version'])
        print('  Number of colors:', len(tree['colorPalette']['data']))
        if verbose:
            print('  Colors:')
            for i, col in enumerate(tree['colorPalette']['data']):
                col_a, col_r, col_g, col_b = lvl_decode_color(col)
                print(f'    Index: {i}, Alpha: {col_a}, Red: {col_r}, Green: {col_g}, Blue: {col_b}')
    else:
        print('No palette found!')

    if 'tileMap' in tree:
        print('Tilemap:')
        print('  Number of frames:', len(tree['tileMap']['frames']))
        durations = [f['duration'] for f in tree['tileMap']['frames']]
        print('  Durations:', durations)
        print('  Total duration:', sum(durations))
        print('  Layers:')
        for i, layer in enumerate(tree['tileMap']['layers']):
            print('    Label:', layer['label'])
            print('    Grid width:', layer['gridWidth'])
            print('    Grid height:', layer['gridHeight'])
            print('    Cell width:', layer['cellWidth'])
            print('    Cell height:', layer['cellHeight'])
            print('    Screen width:', layer['gridWidth'] * layer['cellWidth'])
            print('    Screen height:', layer['gridHeight'] * layer['cellHeight'])
            print('    Number of frames:', len(layer['frames']))
            if verbose:
                print('    Frames:')
                for j, frame in enumerate(layer['frames']):
                    print(f'      Index: {j}, Background color: {frame["bgColor"]}, Border color: {frame["borderColor"]}')
                    print('      Tile:')
                    for row in frame['data']:
                        line = ''.join(safe_chr(col['t']) for col in row)
                        print(f'        [{line}]')
                    print('      Foreground color:')
                    for row in frame['data']:
                        line = ''.join('%2d' % col['fc'] for col in row)
                        print(f'        {line}')
                    print('      Background color:')
                    for row in frame['data']:
                        line = ''.join('%2d' % col['bc'] for col in row)
                        print(f'        {line}')
    else:
        print('No tilemap found!')

#
# Commands
#

def cmd_dump(args):
    with open(args.LVS_FILE, 'rb') as f:
        file = LVSFile.from_bytes(f.read())
    file.dump(Dumper(True))

def cmd_gif_export(args):
    with open(args.LVS_FILE, 'rb') as f:
        file = LVSFile.from_bytes(f.read())
    file.validate()
    file.export_gif(args.GIF_FILE, args.scale)

def cmd_info(args):
    with open(args.LVS_FILE, 'rb') as f:
        file = LVSFile.from_bytes(f.read())
    file.dump(Dumper(False))

def cmd_lvl_dump(args):
    with open(args.LVL_FILE) as f:
        tree = json.load(f)
    lvl_dump(tree, True)

def cmd_lvl_import(args):
    with open(args.LVL_FILE) as f:
        tree = json.load(f)
    lvs = LVSFile.from_lvl(tree, args.tile_size)
    lvs.validate()
    with open(args.LVS_FILE, 'wb') as f:
        f.write(lvs.to_bytes())

def cmd_lvl_info(args):
    with open(args.LVL_FILE) as f:
        tree = json.load(f)
    lvl_dump(tree, False)

def cmd_validate(args):
    with open(args.LVS_FILE, 'rb') as f:
        file = LVSFile.from_bytes(f.read())
    file.validate()

#
# CLI
#

def main():
    parser = argparse.ArgumentParser(description='Tool for text-mode animations in the LVS format.')
    subparsers = parser.add_subparsers(required=True)

    subparser = subparsers.add_parser('dump', description='Print detailed contents of LVS file.')
    subparser.add_argument('LVS_FILE', help='LVS file')
    subparser.set_defaults(func=cmd_dump)

    subparser = subparsers.add_parser('gif-export', description='Export LVS file to GIF animation.')
    subparser.add_argument('-s', '--scale', type=int, default=1, help='Scale factor (default 1)')
    subparser.add_argument('LVS_FILE', help='LVS file')
    subparser.add_argument('GIF_FILE', help='GIF file to create')
    subparser.set_defaults(func=cmd_gif_export)

    subparser = subparsers.add_parser('info', description='Print summary of LVS file.')
    subparser.add_argument('LVS_FILE', help='LVS file')
    subparser.set_defaults(func=cmd_info)

    subparser = subparsers.add_parser('lvl-dump', description='Print detailed contents of lvllvl file.')
    subparser.add_argument('LVL_FILE', help='lvllvl project exported as JSON')
    subparser.set_defaults(func=cmd_lvl_dump)

    subparser = subparsers.add_parser('lvl-import', description='Convert lvllvl file to LVS.')
    subparser.add_argument('-t', '--tile-size', type=int, nargs=2, metavar=('WIDTH', 'HEIGHT'), help='Resize tileset')
    subparser.add_argument('LVL_FILE', help='lvllvl project exported as JSON')
    subparser.add_argument('LVS_FILE', help='LVS file to create')
    subparser.set_defaults(func=cmd_lvl_import)

    subparser = subparsers.add_parser('lvl-info', description='Print summary of lvllvl file.')
    subparser.add_argument('LVL_FILE', help='lvllvl project exported as JSON')
    subparser.set_defaults(func=cmd_lvl_info)

    subparser = subparsers.add_parser('validate', description='Validate LVS file.')
    subparser.add_argument('LVS_FILE', help='LVS file')
    subparser.set_defaults(func=cmd_validate)

    args = parser.parse_args()
    args.func(args)

if __name__ == '__main__':
    main()
