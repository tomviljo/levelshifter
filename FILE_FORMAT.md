# Levelshifter file format

Levelshifter (LVS) is a file format and collection of tools for tile- or text-based animations.

This document aims to explain the format of LVS files and how to render them.

## Definitions

A complete LVS file consists of a palette, a tileset, and one or more animations, to be rendered in sequence and/or in layers.

A palette maps from a color index to an RGB color.

A tileset maps from a tile index to a rectangular bitmap which chooses between background and foreground color.

An animation consists of a series of frames (still images) rendered to a screen over time.

A screen is a rectangular grid of tiles.

A tile is a rectangular area of pixels which has a tileset index, a foreground color index, a background color index and a tag.

Each pixel in a tile is either the foreground or background color from the palette, depending on the corresponding bit in the tile's bitmap in the tileset.

A frame defines the tileset index, foreground color index and background color index for each tile.

## Chunks

An LVS file is a series of chunks without a global header. Every chunk is technically a valid LVS file in itself.
It is therefore possible to create a larger LVS file by concatenating multiple smaller LVS files.

A chunk consists of a header and a body. A header consists of four bytes:

```
Byte 0-1: SIGNATURE: The type and version of the chunk as two ASCII characters.
Byte 2-3: LENGTH:    The length of the chunk, including header, as a big-endian 16-bit integer.
```

The length of the chunk is always even, to assure 16-bit alignment.
If the length of the chunk is originally odd, a zero byte of padding is appended when creating the chunk.
Therefore, an unexpected zero byte at the end of the body should be ignored when parsing the chunk.

### Palette chunk

The palette is a lookup table from color index to RGB value.

The signature of a palette chunk is "P1".

The body consists of a header and color data.

The header consists of one single-byte integer:

```
Byte 0: BITPLANES: Number of bitplanes (between 1 and 5).
```

The color data is an array of 2^BITPLANES colors, where a color consists of three single-byte integers:

```
Byte 0: RED:   Red channel.
Byte 1: GREEN: Green channel.
Byte 2: BLUE:  Blue channel.
```

### Tileset chunk

The tileset, also known as font, is a lookup table from tile index to fixed-size single-bitplane bitmap.

The signature of a tileset chunk is "T1".

The body consists of a header and tile data.

The header consists of seven single-byte integers:

```
Byte 0: WIDTH:         Width in pixels of each tile.
Byte 1: HEIGHT:        Height in pixels of each tile.
Byte 2: SQUARE_WIDTH:  Rendering width of each tile, for pixels with a 1:1 aspect ratio.
Byte 3: SQUARE_HEIGHT: Rendering height of each tile, for pixels with a 1:1 aspect ratio.
Byte 4: STRIDE:        The number of bytes per row in each tile.
Byte 5: FIRST:         The first tile index in the tile data.
Byte 6: LAST:          The last tile index in the tile data.
```

The tile data is an array of (LAST - FIRST) + 1 tiles, where a tile consists of HEIGHT rows, and a row consists of STRIDE bytes.

The top-left corner of a tile is bit 7 of the first byte in the first row.

### Animation chunk

An animation chunk describes the properties of an animation, and applies to all frames between itself and the next animation chunk.

The signature of an animation chunk is "A1".

The body consists of three single-byte integers:

```
Byte 0: WIDTH:  Width in tiles of each frame.
Byte 1: HEIGHT: Height in tiles of each frame.
Byte 2: LOOPS:  Number of loops to render, or zero to loop indefinitely.
```

### Frame chunk

A frame chunk determines the tileset index, foreground color index and background color index for each tile in an animation frame.
The data is compressed using both run-length encoding within a frame and delta encoding between frames.

The signature of a frame chunk is "F1".

The body consists of a header and a series of commands.

The header consists of a two-byte integer:

```
Byte 0-1: DURATION: Number of PAL vertical blanks (20 ms) to wait before switching to the next frame, as a big-endian 16-bit integer.
```

Each command consists of one or more bytes. The three highest bits of the first byte in a command determine its type:

```
Bits 7-5: OPCODE: Type of command.
Bits 4-0:         <Depends on OPCODE>
```

There are seven types commands defined:

```
OPCODE  Command
---------------------------------
     0  TRANSPARENT
     1  BACKGROUND
     2  FOREGROUND
     3  TAG
     4  SKIP
     5  FILL
     6  RUN
     7  <Reserved for future use>
```

A frame is rendered by executing all commands in order. During execution, four variables are kept:

```
Variable    Description               Range                      Default
------------------------------------------------------------------------
INDEX       Index into screen         0 to (WIDTH * HEIGHT - 1)  0
BACKGROUND  Current background color  0 to 31, or TRANSPARENT    N/A
FOREGROUND  Current foreground color  0 to 31                    N/A
TAG         Current tag               0 to 31                    0
```

INDEX is the index into the WIDTH * HEIGHT (from animation header) tiles on the screen when treated as a one-dimensional array,
so that the minimum value is the top-left corner and the maximum value is the bottom-right corner.

BACKGROUND is the background color index to assign to upcoming tiles.
TRANSPARENT is a special implementation-defined value which is used for layering.

FOREGROUND is the foreground color index to assign to upcoming tiles.

TAG is the tag to assign to upcoming tiles.

#### Transparency command

The command TRANSPARENT sets the variable BACKGROUND to the special value TRANSPARENT.

```
Bits 7-5: OPCODE: Set to 0.
Bits 4-0: RFU:    Set to 0.
```

#### Background command

The command BACKGROUND sets the variable BACKGROUND to the given value.

```
Bits 7-5: OPCODE: Set to 1.
Bits 4-0: COLOR:  Background color.
```

#### Foreground command

The command FOREGROUND sets the variable FOREGROUND to the given value.

```
Bits 7-5: OPCODE: Set to 2.
Bits 4-0: COLOR:  Foreground color.
```

#### Tag command

The command TAG sets the variable TAG to the given value.

```
Bits 7-5: OPCODE: Set to 3.
Bits 4-0: TAG:    Tag value.
```

#### Skip command

The command SKIP increments INDEX by COUNT + 1, leaving the skipped tiles unchanged from the previous frame.

If COUNT is less than 16:

```
Bits 7-5: OPCODE: Set to 4.
Bit    4: LONG:   Set to 0.
Bits 3-0: COUNT:  Number of tiles to skip, minus one.
```

Otherwise, if COUNT is between 16 and 4095:

```
Byte 0, bits 7-5: OPCODE:   Set to 4.
Byte 0, bit    4: LONG:     Set to 1.
Byte 0, bits 3-0: COUNT_HI: Upper 4 bits of COUNT.
Byte 1:           COUNT_LO: Lower 8 bits of COUNT.
```

#### Fill command

The command FILL copies the given tileset index to the next COUNT + 1 tiles, incrementing INDEX by COUNT + 1.
The contents of BACKGROUND, FOREGROUND and TAG are assigned to each tile.

If COUNT is less than 16:

```
Byte 0, bits 7-5: OPCODE: Set to 5.
Byte 0, bit    4: LONG:   Set to 0.
Byte 0, bits 3-0: COUNT:  Number of tiles to fill, minus one.
Byte 1:           TILE:   Tileset index.
```

Otherwise, if COUNT is between 16 and 4095:

```
Byte 0, bits 7-5: OPCODE:   Set to 5.
Byte 0, bit    4: LONG:     Set to 1.
Byte 0, bits 3-0: COUNT_HI: Upper 4 bits of COUNT.
Byte 1:           COUNT_LO: Lower 8 bits of COUNT.
Byte 2:           TILE:     Tileset index.
```

#### Run command

The command RUN copies the given tileset indices to the next COUNT + 1 tiles, incrementing INDEX by COUNT + 1.
The contents of BACKGROUND, FOREGROUND and TAG are assigned to each tile.

If COUNT is less than 16:

```
Byte 0, bits 7-5: OPCODE: Set to 6.
Byte 0, bit    4: LONG:   Set to 0.
Byte 0, bits 3-0: COUNT:  Number of tiles to copy, minus one.
Byte 1+:          TILE:   Tileset indices, COUNT + 1 bytes in total.
```

Otherwise, if COUNT is between 16 and 4095:

```
Byte 0, bits 7-5: OPCODE:   Set to 5.
Byte 0, bit    4: LONG:     Set to 1.
Byte 0, bits 3-0: COUNT_HI: Upper 4 bits of COUNT.
Byte 1:           COUNT_LO: Lower 8 bits of COUNT.
Byte 2+:          TILE:     Tileset indices, COUNT + 1 bytes in total.
```

## Validation

Not every file which is constructed according to the specification above is valid and complete.

A file which does not contain one palette, one tileset and at least one animation with at least one frame is not complete and cannot be used for rendering, even if it is valid.
However, it may be useful to keep incomplete files, such as a palette, for constructing a complete file out of parts.

In a tileset, WIDTH must not be higher than STRIDE * 8.

In a tileset, the index 32 (ASCII whitespace) should be included and consist entirely of the background color. This is not a requirement though.

Currently, every animation in a file must have the same WIDTH and HEIGHT.

A frame must encode exactly WIDTH * HEIGHT tiles. That is; when all commands have run, the INDEX variable must equal WIDTH * HEIGHT.

The first frame in an animation must not contain a SKIP command.

In a frame, a BACKGROUND or FOREGROUND command may not contain a COLOR outside the range of the palette.

In a frame, a FILL or RUN command may not contain a TILE outside the range of the tileset.

When decoding a frame, a FILL or RUN command may not appear before the BACKGROUND and FOREGROUND variables are set using the BACKGROUND or TRANSPARENT command and the FOREGROUND command.

## Rendering

To render an animation, do the following:

Create a display which is at least Animation.WIDTH * Tileset.WIDTH pixels wide and at least Animation.HEIGHT * Tileset.HEIGHT pixels high,
and which supports at least 2 ^ Palette.BITPLANES colors.

Allocate a buffer of Animation.WIDTH * Animation.HEIGHT tuples consisting of BACKGROUND, FOREGROUND, TAG and TILE.

Create a frame counter, loop counter and tick counter, initialize all to zero.

Render the first frame to the buffer by executing its commands, then render the buffer to the screen using the palette and tileset.

Start a timer which ticks every vertical blank (20 ms).

Every timer tick, increment the tick counter.

Every time the tick counter reaches DURATION of the current frame, set the tick counter to zero, increment the frame counter and render the next frame.

Every time the frame index reaches the end of the animation, check Animation.LOOPS.
If Animation.LOOPS is zero, set the frame counter to zero and keep rendering.
Otherwise, increment the loop counter and compare against Animation.LOOPS.
If the loop counter equals Animation.LOOPS, stop rendering.
Otherwise, set the frame counter to zero and keep rendering.

### Layering

It is possible to render two or more animations as layers on the same screen, even updating at different frequencies.

To do this, follow the previous section but allocate an intermediary buffer and set of counters for each animation and render them to each buffer in parallel.

Also allocate a final buffer where all intermediary buffers are combined.

When any intermediary buffer has changed, the final buffer needs to updated.

First, copy the intermediary buffer of the base layer to the final buffer.

For each subsequent layer, apply the following rules for each tile:

* If the BACKGROUND value is not TRANSPARENT, overwrite the tile.

* If the BACKGROUND value is TRANSPARENT and TILE is not 32, overwrite FOREGROUND, TAG and TILE.

* If the BACKGROUND value is TRANSPARENT and TILE is 32, do nothing.

If after layering any tile in the final buffer has the BACKGROUND value TRANSPARENT, use zero instead.
This can be avoided by setting a non-transparent background color in every tile of the base layer.

### Scripting

Currently, it is up to the implementation to decide how animations should be sequenced and layered.

For instance, this could be controlled from a Protracker module or similar music file.

The TAG value in each tile can be used to apply special effects to selected portions of the screen.

## Example

This pseudocode file is an example of an indefinitely looping animation of 5 x 2 tiles where the upper row says "Hello" and the lower row says "hello" in blinking text.

```
Hex                Description
----------------------------------
50 31              SIGNATURE "P1" (Palette)
00 0C              LENGTH
01                 BITPLANES
35 28 79           C64 dark blue
6C 5E B5           C64 light blue
00                 Padding

54 31              SIGNATURE "T1" (Tileset)
08 0C              LENGTH
08                 WIDTH
08                 HEIGHT
08                 SQUARE_WIDTH
08                 SQUARE_HEIGHT
01                 STRIDE
00                 FIRST
FF                 LAST
..                 Bitmap data (256 * 8 * 1 bytes)
00                 Padding

41 31              SIGNATURE "A1" (Animation)
00 08              LENGTH
05                 WIDTH
02                 HEIGHT
00                 LOOPS
00                 Padding

46 31              SIGNATURE "F1" (Frame)
00 10              LENGTH
00 19              DURATION (500 ms)
20                 BACKGROUND (Color 0)
41                 FOREGROUND (Color 1)
C4 48 65 6C 6C 6F  RUN (Count 4, Tiles "H", "e", "l", "l", "o")
A4 20              FILL (Count 4, Tile " ")

46 31              SIGNATURE "F1" (Frame)
00 10              LENGTH
00 19              DURATION (500 ms)
20                 BACKGROUND (Color 0)
41                 FOREGROUND (Color 1)
84                 SKIP (Count 4)
C4 77 6F 72 6C 64  RUN (Count 4, Tiles "w", "o", "r", "l", "d")
00                 Padding (also TRANSPARENT)
```
