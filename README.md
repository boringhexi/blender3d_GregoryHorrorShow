# GregoryHorrorShow-Blender-IO 
GregoryHorrorShow-Blender-IO is an addon for Blender that imports models from the game Gregory Horror Show for PlayStation 2.
- Supported file types are .ghs, .map-pm2, and .pm2. These must first be extracted from the game using [ghs-tools](https://github.com/boringhexi/ghs-tools) (see [Usage](#Usage) below).
- Supported features: meshes, textures/materials, animations (armature, shape key), vertex colors
- Blender version compatibililty: probably 3.4.1 and up (tested with 3.4.1, 4.5.3)

## Installation
### Blender 4.0.0+
1. Download the latest release from the [releases page](https://github.com/boringhexi/blender3d_GregoryHorrorShow/releases). Don't unzip it.
2. Open Blender, then drag the downloaded zip file into the Blender window.
3. Hit OK.
### Blender 3.x.x
1. Download the latest release from the [releases page](https://github.com/boringhexi/blender3d_GregoryHorrorShow/releases). Don't unzip it.
2. Open Blender, go to Edit &rarr; Preferences.
3. In the new window that pops up, click Addons in the left column, then click "Install..." near the top right.
4. Navigate to and choose the file you downloaded in step 1.
5. If installed correctly, this addon will be the only one shown in the addon window. Enable the checkbox next to it.


## Usage
First use [ghs-tools](https://github.com/boringhexi/ghs-tools) to extract files from the game:

1. Run `ghs_filestm_unpack.py -v FILE.STM` to unpack the EU version's FILE.STM contents.
   - The resulting folder will be named `GHS_EU_FILE_STM`.
2. Then, use `ghs_modelmeta_extract.py [path_to_executable]` on the EU version executable file.
   - The EU version executable file is named `SLES_519.33`.
   - This will extract `###.ghs` files into the existing `GHS_EU_FILE_STM` folder structure.

Then use this Blender addon to import the resulting .ghs, .map-pm2, or .pm2 files into Blender.
- **.ghs**: characters, room props
- **.map-pm2**: rooms
- **.pm2**: model parts
  - Usually you don't import these directly. Rather the addon imports them behind the scenes (from a nearby folder in the case of .ghs, or from inside the .map-pm2 file in the case of map-pm2).
  - However, some models can only be imported via pm2 files (such as objects found on the ground).
  - To load textures, you must paste the absolute texture directory into the Import options &rarr; Advanced &rarr; PM2 texture directory. See: [I imported .pm2 files directly. Why are they invisible in Blender's textured view modes?](https://github.com/boringhexi/blender3d_GregoryHorrorShow/wiki/Problems-and-solutions#i-imported-pm2-files-directly-why-are-they-invisible-in-blenders-textured-view-modes).


