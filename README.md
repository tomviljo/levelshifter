# levelshifter

Levelshifter (LVS) is a file format and collection of tools for tile-based animations.

## Import from lvllvl

Save lvllvl project as `animation.json`, then run:

```
python3 lvs.py lvl-import animation.json animation.lvs
```

## Print file information

To print a summary of `animation.lvs`, run:

```
python3 lvs.py info animation.lvs
```

## Export to GIF animation

To create a GIF from `animation.lvs` at 2x scale, run (requires Pillow):

```
python3 lvs.py gif-export --scale 2 animation.lvs animation.gif
```
