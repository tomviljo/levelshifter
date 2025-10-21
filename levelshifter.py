import argparse
import copy
import json
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
    TRANSPARENT_BG = 255
    TRANSPARENT_CH = 32

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.bg = bytearray(width * height)
        self.fg = bytearray(width * height)
        self.ch = bytearray(width * height)

    def count(self):
        return self.width * self.height

    def index(self, x, y):
        return y * self.width + x

    def get(self, index):
        return self.bg[index], self.fg[index], self.ch[index]

    def get_text(self, index, length):
        return bytes(self.ch[index : index + length])

    def set(self, index, bg, fg, ch):
        self.bg[index] = bg
        self.fg[index] = fg
        self.ch[index] = ch

    def apply(self, other):
        for index in range(self.count()):
            if other.bg[index] != self.TRANSPARENT_BG:
                # Opaque background
                self.bg[index] = other.bg[index]
                self.fg[index] = other.fg[index]
                self.ch[index] = other.ch[index]
            elif other.ch[index] != self.TRANSPARENT_CH:
                # Non-transparent character with transparent background
                self.fg[index] = other.fg[index]
                self.ch[index] = other.ch[index]

    @classmethod
    def from_lvl(cls, tree, width, height):
        screen = cls(width, height)

        if len(tree['data']) != height:
            raise ValueError(f"Expected {height} rows, got {len(tree['data'])}")
        for y, row in enumerate(tree['data']):
            if len(row) != width:
                raise ValueError(f"Expected {width} columns, got {len(row)}")
            for x, col in enumerate(row):
                # lvllvl uses -1 for transparency, -1 & 0xff is 0xff
                screen.set(screen.index(x, y), col['bc'] & 0xff, col['fc'], col['t'])

        return screen

class ScreenWriter:
    def __init__(self, screen):
        self.screen = screen
        self.index = 0
        self.bg = None
        self.fg = None

    def set_transparent(self):
        self.bg = Screen.TRANSPARENT_BG

    def set_background(self, color):
        self.bg = color

    def set_foreground(self, color):
        self.fg = color

    def skip(self, count):
        self.index += count

    def write(self, char):
        if self.bg is None:
            raise ValueError('Background color not set')
        if self.fg is None:
            raise ValueError('Foreground color not set')
        self.screen.set(self.index, self.bg, self.fg, char)
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
                bg_a, fg_a, ch_a = self.screen.get(index)
                bg_b, fg_b, ch_b = self.screen.get(index + 1)
                if bg_a == bg_b and fg_a == fg_b:
                    # Unchanged color, can be part of same run
                    run_count += 1
                    if ch_a == ch_b:
                        # Also unchanged char, can be part of same fill
                        fill_count += 1
                    else:
                        # Changed char, breaks fill
                        fill_count = 1
                else:
                    # Changed color, breaks runs and fills
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
        last_bg = None
        last_fg = None

        while index < self.screen.count():
            bg, fg, ch = self.screen.get(index)

            if self.skip_len[index]:
                count = min(self.skip_len[index], 4096)
                command = LVSCommandSkip(count - 1)
            elif self.fill_len[index] >= 3:
                count = min(self.fill_len[index], 4096)
                command = LVSCommandFill(count - 1, ch)
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

            # Make sure colors are up to date before adding character command
            if not isinstance(command, LVSCommandSkip):
                if bg != last_bg:
                    if bg == Screen.TRANSPARENT_BG:
                        commands.append(LVSCommandTransparent())
                    else:
                        commands.append(LVSCommandBackground(bg))
                    last_bg = bg
                if fg != last_fg:
                    commands.append(LVSCommandForeground(fg))
                    last_fg = fg

            commands.append(command)

        return commands

#
# LVS classes
#

def lvs_decode(slicer, subtypes):
    chunks = []

    while slicer.length():
        signature, body_length = slicer.unpack(LVSChunk.CHUNK_STRUCT)
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

class LVSChunk:
    '''Base class, do not instantiate'''

    CHUNK_STRUCT = struct.Struct('> 4s L')

    def body_to_bytes(self):
        raise NotImplementedError()

    def to_bytes(self):
        body = self.body_to_bytes()

        # Add padding if odd size
        if len(body) & 1:
            body += b'\x00'

        return self.CHUNK_STRUCT.pack(self.SIGNATURE, len(body)) + body

class LVSFile(LVSChunk):
    SIGNATURE = b'LVS1'

    def __init__(self, palette, font, animations):
        self.palette = palette
        self.font = font
        self.animations = animations

    def validate(self):
        if self.palette:
            self.palette.validate()
        if self.font:
            self.font.validate()
        for anim in self.animations:
            anim.validate(self)

    def body_to_bytes(self):
        data = bytes()
        if self.palette:
            data += self.palette.to_bytes()
        if self.font:
            data += self.font.to_bytes()
        for anim in self.animations:
            data += anim.to_bytes()
        return data

    def dump(self, dumper):
        if self.palette:
            dumper.print('Palette:')
            self.palette.dump(dumper.inner())
        if self.font:
            dumper.print('Font:')
            self.font.dump(dumper.inner())
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
        if not self.font:
            raise ValueError(f'Font not found')
        if not self.animations:
            raise ValueError(f'No animation found')

        anim = self.animations[0]
        scaled_cell_width = scale * anim.cell_width
        scaled_cell_height = scale * anim.cell_height
        screen = Screen(anim.grid_width, anim.grid_height)
        images = []
        for frame in anim.frames:
            image = Image.new('P', (anim.grid_width * scaled_cell_width,
                                    anim.grid_height * scaled_cell_height))
            image.putpalette(self.palette.colors_to_bytes())
            writer = ScreenWriter(screen)
            for command in frame.commands:
                command.execute(writer)
            for row in range(anim.grid_height):
                for column in range(anim.grid_width):
                    bg, fg, ch = screen.get(screen.index(column, row))
                    bg = 0 if (bg == Screen.TRANSPARENT_BG) else bg
                    char = self.font.get_char(ch)
                    for cell_y in range(scaled_cell_height):
                        char_y = (cell_y * self.font.height) // scaled_cell_height
                        for cell_x in range(scaled_cell_width):
                            char_x = (cell_x * self.font.width) // scaled_cell_width
                            color = fg if char.get(char_x, char_y) else bg
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
    def from_lvl(cls, tree):
        if tree['version'] != 1:
            raise ValueError(f"Unexpected version: {tree['version']}")

        if 'colorPalette' in tree:
            palette = LVSPalette.from_lvl(tree['colorPalette'])
        else:
            palette = None

        if 'tileSet' in tree:
            font = LVSFont.from_lvl(tree['tileSet'])
        else:
            font = None

        if 'tileMap' in tree:
            animations = [LVSAnimation.from_lvl(tree['tileMap'])]
        else:
            animations = []

        return cls(palette, font, animations)

    @classmethod
    def decode(cls, slicer):
        palette = None
        font = None
        animations = []

        for chunk in lvs_decode(slicer, [LVSPalette, LVSFont, LVSAnimation]):
            if isinstance(chunk, LVSPalette):
                if palette:
                    raise ValueError('More than one palette')
                palette = chunk
            elif isinstance(chunk, LVSFont):
                if font:
                    raise ValueError('More than one font')
                font = chunk
            elif isinstance(chunk, LVSAnimation):
                animations.append(chunk)
            else:
                raise ValueError('Unexpected chunk: {type(chunk)}')

        return cls(palette, font, animations)

    @classmethod
    def from_file(cls, path):
        with open(path, 'rb') as f:
            slicer = Slicer(f.read())
        files = lvs_decode(slicer, [cls])
        if not files:
            raise ValueError('Empty file')
        files[0].validate()
        return files[0]

class LVSPalette(LVSChunk):
    SIGNATURE = b'PALE'
    STRUCT = struct.Struct('> H')

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

class LVSFont(LVSChunk):
    SIGNATURE = b'FONT'
    STRUCT = struct.Struct('> H H H H H')

    def __init__(self, width, height, stride, begin, end, chars):
        self.width = width
        self.height = height
        self.stride = stride
        self.begin = begin
        self.end = end
        self.chars = chars

    def validate(self):
        if self.width < 1:
            raise ValueError(f'Illegal font width: {self.width}')
        if self.height < 1:
            raise ValueError(f'Illegal font height: {self.height}')
        if self.stride < 1:
            raise ValueError(f'Illegal font stride: {self.stride}')
        if self.stride * 8 < self.width:
            raise ValueError(f'Font stride {self.stride} too small for width {self.width}')
        if self.begin < 0 or self.begin > 256:
            raise ValueError(f'Illegal font begin index: {self.begin}')
        if self.end < 0 or self.end > 256 or self.end < self.begin:
            raise ValueError(f'Illegal font end index: {self.end}')
        if len(self.chars) != self.end - self.begin:
            raise ValueError(f'Expected {self.end - self.begin} font characters, got {len(self.chars)}')

    def body_to_bytes(self):
        return (self.STRUCT.pack(self.width, self.height, self.stride, self.begin, self.end) +
                b''.join(char.to_bytes() for char in self.chars))

    def dump(self, dumper):
        dumper.print(f'Width: {self.width}')
        dumper.print(f'Height: {self.height}')
        dumper.print(f'Stride: {self.stride}')
        dumper.print(f'Begin: {self.begin}')
        dumper.print(f'End: {self.end}')
        if dumper.verbose:
            for i, char in enumerate(self.chars):
                dumper.print(f'Character {self.begin + i}:')
                char.dump(dumper.inner())

    def get_char(self, code):
        return self.chars[code - self.begin]

    @classmethod
    def from_lvl(cls, tree):
        if tree['version'] != 1:
            raise ValueError(f"Unexpected version: {tree['version']}")
        width = tree['width']
        height = tree['height']
        num_tiles = len(tree['tiles'])
        if num_tiles != 256:
            raise ValueError(f"Unexpected number of tiles: {num_tiles}")
        begin = 0
        end = 256
        stride = (width + 7) // 8
        chars = [LVSCharacter.from_lvl(tile, width, height, stride) for tile in tree['tiles']]
        return cls(width, height, stride, begin, end, chars)

    @classmethod
    def decode(cls, slicer):
        width, height, stride, begin, end = slicer.unpack(cls.STRUCT)
        chars = [LVSCharacter.decode(slicer, stride, height) for i in range(end - begin)]
        return cls(width, height, stride, begin, end, chars)

class LVSCharacter:
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
    def from_lvl(cls, tree, width, height, stride):
        char = cls(stride, height)
        for y in range(height):
            for x in range(width):
                char.set(x, y, tree['data'][0][y * width + x])
        return char

    @classmethod
    def decode(cls, slicer, stride, height):
        return cls(stride, height, slicer.take(stride * height))

class LVSAnimation(LVSChunk):
    SIGNATURE = b'ANIM'
    STRUCT = struct.Struct('> H H H H H')

    def __init__(self, grid_width, grid_height, cell_width, cell_height, loops, frames):
        self.grid_width = grid_width
        self.grid_height = grid_height
        self.cell_width = cell_width
        self.cell_height = cell_height
        self.loops = loops
        self.frames = frames

    def num_cells(self):
        return self.grid_width * self.grid_height

    def validate(self, file):
        first_animation = file.animations[0]

        if self.grid_width < 1:
            raise ValueError(f'Illegal grid width: {self.grid_width}')
        if self.grid_width != first_animation.grid_width:
            raise ValueError(f'Expected grid width {first_animation.grid_width}, got {self.grid_width}')
        if self.grid_height < 1:
            raise ValueError(f'Illegal grid height: {self.grid_height}')
        if self.grid_height != first_animation.grid_height:
            raise ValueError(f'Expected grid height {first_animation.grid_height}, got {self.grid_height}')
        if self.cell_width < 1:
            raise ValueError(f'Illegal cell width: {self.cell_width}')
        if self.cell_height < 1:
            raise ValueError(f'Illegal cell height: {self.cell_height}')
        if self.loops < 0:
            raise ValueError(f'Illegal number of loops: {self.loops}')
        for frame in self.frames:
            frame.validate(file)

    def body_to_bytes(self):
        return (self.STRUCT.pack(self.grid_width, self.grid_height, self.cell_width, self.cell_height, self.loops) +
                b''.join(frame.to_bytes() for frame in self.frames))

    def dump(self, dumper):
        dumper.print(f'Grid width: {self.grid_width}')
        dumper.print(f'Grid height: {self.grid_height}')
        dumper.print(f'Cell width: {self.cell_width}')
        dumper.print(f'Cell height: {self.cell_height}')
        dumper.print(f'Loops: {self.loops}')
        for i, frame in enumerate(self.frames):
            dumper.print(f'Frame {i}:')
            frame.dump(dumper.inner())

    @classmethod
    def from_lvl(cls, tree):
        if not tree['layers']:
            raise ValueError(f"No layers")

        # Gather information from layers
        for i, layer in enumerate(tree['layers']):
            if len(layer['frames']) != len(tree['frames']):
                raise ValueError(f"Expected {len(tree['frames'])} frames in layer {i}, got {len(layer['frames'])}")
            if i:
                if layer['gridWidth'] != grid_width:
                    raise ValueError(f"Expected gridWidth {grid_width} in layer {i}, got {layer['gridWidth']}")
                if layer['gridHeight'] != grid_height:
                    raise ValueError(f"Expected gridHeight {grid_height} in layer {i}, got {layer['gridHeight']}")
            else:
                grid_width = layer['gridWidth']
                grid_height = layer['gridHeight']
                cell_width = layer['cellWidth']
                cell_height = layer['cellHeight']

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

                layer_screen = Screen.from_lvl(layer_frame, grid_width, grid_height)
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

        return cls(grid_width, grid_height, cell_width, cell_height, 0, frames)

    @classmethod
    def decode(cls, slicer):
        grid_width, grid_height, cell_width, cell_height, loops = slicer.unpack(cls.STRUCT)
        frames = lvs_decode(slicer, [LVSFrame])

        return cls(grid_width, grid_height, cell_width, cell_height, loops, frames)

class LVSFrame(LVSChunk):
    SIGNATURE = b'FRAM'
    STRUCT = struct.Struct('> H')

    def __init__(self, duration, commands):
        self.duration = duration
        self.commands = commands

    def validate(self, file):
        first_animation = file.animations[0]

        if self.duration < 1:
            raise ValueError(f'Illegal frame duration: {self.duration}')
        index = 0
        for cmd in self.commands:
            cmd.validate(file)
            index += cmd.cells()
        if index != first_animation.num_cells():
            raise ValueError(f'Frame encodes {index} cells, expected {first_animation.num_cells()}')

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
    def validate_char(cls, char, file):
        if file.font and (char < file.font.begin or char >= file.font.end):
            raise ValueError(f'Char {char} outside range [{font.begin}, {font.end}) of font')

    @classmethod
    def encode_with_5_bit_value(cls, value):
        if value < 0x20:
            return struct.pack('B', cls.OPCODE << 5 | value)
        else:
            raise ValueError(f'Too large value: {value}')

    @classmethod
    def encode_with_12_bit_value(cls, value):
        if value < 0x10:
            return struct.pack('B', cls.OPCODE << 5 | value)
        elif value < 0x1000:
            return struct.pack('B B', cls.OPCODE << 5 | 0x10 | value >> 8, value & 0xff)
        else:
            raise ValueError(f'Too large value: {value}')

class LVSCommandTransparent(LVSCommand):
    OPCODE = 0

    def __init__(self):
        pass

    def to_bytes(self):
        return self.encode_with_5_bit_value(0)

    def validate(self, file):
        pass

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print('Transparent')

    def execute(self, writer):
        writer.set_transparent()

class LVSCommandBackground(LVSCommand):
    OPCODE = 1

    def __init__(self, color):
        self.color = color

    def to_bytes(self):
        return self.encode_with_5_bit_value(self.color)

    def validate(self, file):
        self.validate_color(self.color, file)

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print(f'Background (Color: {self.color})')

    def execute(self, writer):
        writer.set_background(self.color)

class LVSCommandForeground(LVSCommand):
    OPCODE = 2

    def __init__(self, color):
        self.color = color

    def to_bytes(self):
        return self.encode_with_5_bit_value(self.color)

    def validate(self, file):
        self.validate_color(self.color, file)

    def cells(self):
        return 0

    def dump(self, dumper):
        dumper.print(f'Foreground (Color: {self.color})')

    def execute(self, writer):
        writer.set_foreground(self.color)

class LVSCommandSkip(LVSCommand):
    OPCODE = 3

    def __init__(self, count):
        self.count = count

    def to_bytes(self):
        return self.encode_with_12_bit_value(self.count)

    def validate(self, file):
        pass

    def cells(self):
        return self.count + 1

    def dump(self, dumper):
        dumper.print(f'Skip (Count: {self.count})')

    def execute(self, writer):
        writer.skip(self.count + 1)

class LVSCommandFill(LVSCommand):
    OPCODE = 4

    def __init__(self, count, char):
        self.count = count
        self.char = char

    def to_bytes(self):
        return self.encode_with_12_bit_value(self.count) + bytes([self.char])

    def validate(self, file):
        self.validate_char(self.char, file)

    def cells(self):
        return self.count + 1

    def dump(self, dumper):
        dumper.print(f'Fill (Count: {self.count}, Char: {self.char}, Text: "{safe_chr(self.char)}")')

    def execute(self, writer):
        for i in range(self.count + 1):
            writer.write(self.char)

class LVSCommandRun(LVSCommand):
    OPCODE = 5

    def __init__(self, data):
        self.data = data

    def to_bytes(self):
        return self.encode_with_12_bit_value(len(self.data) - 1) + self.data

    def validate(self, file):
        if not self.data:
            raise ValueError(f'No data')
        for char in self.data:
            self.validate_char(char, file)

    def cells(self):
        return len(self.data)

    def dump(self, dumper):
        dumper.print(f'Run (Chars: {str(list(self.data))}, Text: "{"".join(safe_chr(c) for c in self.data)}")')

    def execute(self, writer):
        for char in self.data:
            writer.write(char)

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

def lvl_import(tree, alignment):
    if tree['version'] != 1:
        raise ValueError(f"Unexpected version {tree['version']}")

    if 'colorPalette' in tree:
        if tree['colorPalette']['version'] != 1:
            raise ValueError(f"Unexpected colorPalette version {tree['colorPalette']['version']}")
        num_colors = len(tree['colorPalette']['data'])
        if num_colors < 2:
            raise ValueError(f"Too few colors")
        elif num_colors == 2:
            bitplanes = 1
        elif num_colors <= 4:
            bitplanes = 2
        elif num_colors <= 8:
            bitplanes = 3
        elif num_colors <= 16:
            bitplanes = 4
        elif num_colors <= 32:
            bitplanes = 5
        else:
            raise ValueError(f"Too many colors")
        colors = []
        for i in range(1 << bitplanes):
            if i < num_colors:
                _, r, g, b = lvl_decode_color(tree['colorPalette']['data'][i])
                colors.append(LVSColor(r, g, b))
            else:
                colors.append(LVSColor(0, 0, 0))
        palette = LVSPalette(colors)
    else:
        palette = None

    if 'tileSet' in tree:
        if tree['tileSet']['version'] != 1:
            raise ValueError(f"Unexpected tileSet version {tree['tileSet']['version']}")
        font_width = tree['tileSet']['width']
        font_height = tree['tileSet']['height']
        num_tiles = len(tree['tileSet']['tiles'])
        if num_tiles != 256:
            raise ValueError(f"Unexpected number of tiles: {num_tiles}")
        font_begin = 0
        font_end = 256
        font_stride = ((font_width + aligment - 1) // alignment) // 8
        font_data = bytearray(num_tiles * font_height * font_stride)
        for t in range(num_tiles):
            for y in range(font_height):
                for x in range(font_width):
                    if tree['tileSet']['tiles'][t]['data'][0][y * w + x]:
                        font_data[(t * font_height + y) * font_stride + x // 8] |= 0x80 >> (x & 7)
        font = LVSFont(font_width, font_height, font_stride, font_begin, font_end, font_data)
    else:
        font = None

    if 'tileMap' in tree:
        if not tree['tileMap']['layers']:
            raise ValueError(f"No layers in tileMap")

        for i, layer in enumerate(tree['tileMap']['layers']):
            if len(layer['frames']) != len(tree['tileMap']['frames']):
                raise ValueError(f"Unexpected number of frames in layer {i}: {len(layer['frames'])}")
            if i:
                if layer['gridWidth'] != grid_width:
                    raise ValueError(f"Unexpected gridWidth in layer {i}: {layer['gridWidth']}")
                if layer['gridHeight'] != grid_height:
                    raise ValueError(f"Unexpected gridHeight in layer {i}: {layer['gridHeight']}")
            else:
                grid_width = layer['gridWidth']
                grid_height = layer['gridHeight']
                text_buffer = TextBuffer(grid_width, grid_height)

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
        raise ValueError(f"No tileMap found")

#
# Commands
#

def cmd_dump(args):
    dumper = Dumper(True)
    with open(args.LVS_FILE, 'rb') as f:
        slicer = Slicer(f.read())
    for i, file in enumerate(lvs_decode(slicer, [LVSFile])):
        dumper.print(f'File {i}:')
        file.dump(dumper.inner())

def cmd_gif_export(args):
    LVSFile.from_file(args.LVS_FILE).export_gif(args.GIF_FILE, args.scale)

def cmd_info(args):
    dumper = Dumper(False)
    with open(args.LVS_FILE, 'rb') as f:
        slicer = Slicer(f.read())
    for i, file in enumerate(lvs_decode(slicer, [LVSFile])):
        dumper.print(f'File {i}:')
        file.dump(dumper.inner())

def cmd_lvl_dump(args):
    with open(args.LVL_FILE) as f:
        tree = json.load(f)
    lvl_dump(tree, True)

def cmd_lvl_import(args):
    with open(args.LVL_FILE) as f:
        tree = json.load(f)
    lvs = LVSFile.from_lvl(tree)
    lvs.validate()
    with open(args.LVS_FILE, 'wb') as f:
        f.write(lvs.to_bytes())

def cmd_lvl_info(args):
    with open(args.LVL_FILE) as f:
        tree = json.load(f)
    lvl_dump(tree, False)

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
    subparser.add_argument('LVL_FILE', help='lvllvl project exported as JSON')
    subparser.add_argument('LVS_FILE', help='LVS file to create')
    subparser.set_defaults(func=cmd_lvl_import)

    subparser = subparsers.add_parser('lvl-info', description='Print summary of lvllvl file.')
    subparser.add_argument('LVL_FILE', help='lvllvl project exported as JSON')
    subparser.set_defaults(func=cmd_lvl_info)

    args = parser.parse_args()
    args.func(args)

if __name__ == '__main__':
    main()
