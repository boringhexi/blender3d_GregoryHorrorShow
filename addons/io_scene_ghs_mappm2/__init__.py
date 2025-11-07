import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

bl_info = {
    "name": "Import Gregory Horror Show",
    "author": "boringhexi",
    "version": (0, 2, 1),
    "blender": (3, 4, 1),
    "location": "File > Import",
    "description": "Import GHS/MAP-PM2/PM2 files from Gregory Horror Show (PS2)",
    "warning": "",
    "doc_url": "https://github.com/boringhexi/GregoryHorrorShow-Blender-IO/",
    "category": "Import-Export",
}

# Make the entire addon reloadable by Blender:
# The "Reload Scripts" command reloads only this file (the top-level __init__.py).
# That means it won't reload our modules imported by this file (or other modules
# imported by those modules). So instead, the code below will reload our modules
# whenever this file is reloaded.
if "_this_file_was_already_loaded" in locals():
    from .common.reload_modules import reload_modules

    # Order matters. Reload module B before reloading module A that imports module B
    modules_to_reload = (
        ".common.reload_modules",
        ".common.datautils",
        ".common.findimportdirs",
        ".pm2.pm2model",
        ".pm2.pm2importer",
        ".ghs.meshposrot",
        ".ghs.ghsimporter",
        ".mappm2.mappm2container",
        ".mappm2.mappm2importer",
        ".import_ghs_mappm2",
    )
    reload_modules(*modules_to_reload, pkg=__package__)
_this_file_was_already_loaded = True  # to detect the reload next time
# After this point, any imports of the modules above will be up-to-date.


class ImportGHSMAPPM2(bpy.types.Operator, ImportHelper):
    """Import GHS, MAP-PM2, and/or PM2 files"""

    bl_idname = "import_scene.ghsmappm2"
    bl_label = "Import GHS/MAP-PM2"
    bl_options = {"REGISTER", "UNDO"}

    filter_glob: StringProperty(default="*.ghs;*.map-pm2;*.pm2", options={"HIDDEN"})
    files: CollectionProperty(type=bpy.types.OperatorFileListElement)

    bl_name_override: StringProperty(
        name="Object name override",
        default="",
        description="Object names will be based on this instead of being generated "
        "automatically from filenames",
    )

    ghs_anim_method: EnumProperty(
        name="GHS animation",
        items=[
            (
                "DRIVER",
                "Easy animation viewer",
                "Imports animations as separate NLA tracks with shapekey drivers. "
                "Works well as an animation viewer, but may have trouble exporting "
                "shapekey animations to other formats",
            ),
            (
                "GLTF",
                "For glTF export",
                "Imports animations as separate NLA tracks, also using NLA tracks for "
                "shapekey animations. From this, glTF exporter can then export a "
                "single file with multiple animations, including shapekey animations",
            ),
            (
                "1LONG",
                "Single timeline",
                "Imports all animations into the timeline in sequence",
            ),
            (
                "1LONG_EVERY100",
                "Single timeline (starts every 100)",
                "Each animation starts on a multiple of 100 frames. Suitable for "
                "exporting to Unity",
            ),
            (
                "TPOSE",
                "T-Pose approx",
                "No animation. Approximates a good-enough T-Pose (by using only "
                "default model parts and no rest pose rotation)",
            ),
            (
                "NONE",
                "None",
                "No animation. Only default body parts, and rest pose is taken from "
                "the first frame of the first animation (if any)",
            ),
        ],
        description="How .ghs animations should be imported",
        default="DRIVER",
    )

    armature_parenting_workaround: BoolProperty(
        name="Armature parenting workaround",
        description="Usually meshes will be parented directly to armature bones, but "
        "some exporters don't support this. So enable this option to use an armature "
        "modifier and vertex groups instead",
        default=False,
    )

    pm2_texdir: StringProperty(
        name="PM2 texture directory",
        description="When importing standalone PM2 files, load textures from this "
        "directory (absolute path). Otherwise no textures will be loaded",
        default="",
    )

    oldexporter_compat: BoolProperty(
        name="Old exporter compatibility",
        description="Allow textures and vertex colors to be detected and exported by "
        "some older exporters (e.g. Collada)",
        default=False,
    )

    vcol_alpha: EnumProperty(
        name="Vertex color alpha",
        items=[
            (
                "AUTO",
                "Automatic",
                "Ignore vertex color alpha for just map-pm2 files, "
                "import it for everything else (ghs and standalone pm2 files)",
            ),
            (
                "IMPORT",
                "Import",
                "Always import the vertex color alpha",
            ),
            (
                "IGNORE",
                "Ignore",
                "Always ignore the vertex color alpha",
            ),
        ],
        description="Whether to import or ignore vertex color alpha. "
        "Automatic is recommended, but the Final Chase map-pm2 needs Import, "
        "while decoration pm2s need Ignore",
        default="AUTO",
    )

    def draw(self, context):
        layout = self.layout

        layout.use_property_split = True
        layout.use_property_decorate = False  # No animation.

        layout.prop(self, "ghs_anim_method")
        layout.prop(self, "vcol_alpha")

        header, body = layout.panel("GHSMAPPM2_import_advanced", default_closed=True)
        header.label(text="Advanced")
        if body is not None:
            body.prop(self, "bl_name_override")
            body.prop(self, "armature_parenting_workaround")
            body.prop(self, "oldexporter_compat")
            body.prop(self, "pm2_texdir")

    def execute(self, context):
        # to reduce Blender startup time, delay import until now
        from . import import_ghs_mappm2

        keywords = self.as_keywords(ignore=("filter_glob",))
        return import_ghs_mappm2.load(context, **keywords)


def menu_func_import(self, context):
    self.layout.operator(
        ImportGHSMAPPM2.bl_idname, text="Gregory Horror Show (.ghs/.map-pm2)"
    )


classes = (ImportGHSMAPPM2,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in classes:
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
