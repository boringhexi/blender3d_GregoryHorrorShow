import os.path
from math import radians

from .common.findimportdirs import find_ghs_import_dirs, find_mappm2_tex_dir
from .ghs.ghsimporter import GhsImporter
from .mappm2.mappm2importer import MapPm2Importer
from .pm2.pm2importer import Pm2Importer
from .pm2.pm2model import Pm2Model


def load_ghs_mappm2(
    context,
    *,
    filepath,
    files=None,
    bl_name_override="",
    ghs_anim_method="DRIVER",
    armature_parenting_workaround=False,
    pm2_texdir="",
    oldexporter_compat=False,
    vcol_alpha="AUTO",
):
    if files:
        dirname = os.path.dirname(filepath)
        filepaths = [os.path.join(dirname, file.name) for file in files]
    else:
        filepaths = [filepath]

    for inpath in filepaths:
        basename = os.path.basename(inpath)
        if bl_name_override:
            bl_name = bl_name_override
            ext = os.path.splitext(basename)[1]
        else:
            bl_name, ext = os.path.splitext(basename)

        if ext == ".ghs":
            texdir, pm2dir, mprdir = find_ghs_import_dirs(inpath)
            import_vcol_alpha = vcol_alpha in ("AUTO", "IMPORT")  # if AUTO, import
            ghsimporter = GhsImporter(
                inpath,
                pm2dir,
                mprdir,
                texdir,
                bl_name,
                anim_method=ghs_anim_method,
                bone_parenting=not armature_parenting_workaround,
                oldexporter_compat=oldexporter_compat,
                import_vcol_alpha=import_vcol_alpha,
            )
            ghsimporter.import_stuff()

        elif ext == ".map-pm2":
            if not bl_name_override:
                bl_name = f"{bl_name}_"
            texdir = find_mappm2_tex_dir(inpath)
            import_vcol_alpha = vcol_alpha == "IMPORT"  # if AUTO, ignore
            mappm2importer = MapPm2Importer(
                inpath,
                texdir,
                bl_name,
                oldexporter_compat=oldexporter_compat,
                import_vcol_alpha=import_vcol_alpha,
            )
            mappm2importer.import_mappm2()

        elif ext == ".pm2":
            import_vcol_alpha = vcol_alpha in ("AUTO", "IMPORT")  # if AUTO, import
            with open(inpath, "rb") as fp:
                pm2model = Pm2Model.from_file(fp)
            pm2importer = Pm2Importer(
                pm2model,
                bl_name=bl_name,
                texdir=pm2_texdir,
                oldexporter_compat=oldexporter_compat,
                import_vcol_alpha=import_vcol_alpha,
            )
            pm2importer.import_scene()

            # also needs to be rotated to correct axes
            pm2meshobj = pm2importer.bl_meshobj
            pm2meshobj.rotation_euler = (radians(90), radians(180), 0)
            # and set as the active object
            context.view_layer.objects.active = pm2meshobj
            del pm2model, pm2importer

        else:
            pass

    context.view_layer.update()


def load_with_profiler(context, **keywords):
    import cProfile
    import pstats

    pro = cProfile.Profile()
    pro.runctx("load_ghs_mappm2(context, **keywords)", globals(), locals())
    st = pstats.Stats(pro)
    st.sort_stats("time")
    st.print_stats(0.1)
    st.print_callers(0.1)
    return {"FINISHED"}


def load(context, **keywords):
    # load_with_profiler(context, **keywords)
    load_ghs_mappm2(context, **keywords)
    return {"FINISHED"}
