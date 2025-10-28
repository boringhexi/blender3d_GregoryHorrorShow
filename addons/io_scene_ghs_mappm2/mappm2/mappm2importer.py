from math import radians
from pathlib import Path

from bpy.types import Material

from ..pm2.pm2importer import MatSettings, Pm2Importer
from ..pm2.pm2model import Pm2Model
from .mappm2container import MapPm2Container


class MapPm2Importer:
    def __init__(
        self,
        mappm2path,
        texdir,
        bl_name="",
        tex_oldexporters_compat=False,
        import_vcol_alpha=True,
    ):
        """

        :param mappm2path:
        :param bl_name:
        :param tex_oldexporters_compat: if True, connect texture nodes directly to
            PBsdf, no vertex color or alpha clipping nodes
        :param import_vcol_alpha: if True, import vertex color alpha
        """
        self.mappm2path = Path(mappm2path)
        self.texdir = texdir
        self.bl_name = bl_name
        self._tex_oldexporters_compat = tex_oldexporters_compat
        self._import_vcol_alpha = import_vcol_alpha
        self._matsettings_materials_to_reuse = None  # dict() to reuse materials

    def import_mappm2(self):
        with open(self.mappm2path, "rb") as file:
            mappm2container = MapPm2Container.from_file(file)

        # import pm2 files
        for i, contentfile in enumerate(mappm2container):
            pm2model = Pm2Model.from_file(contentfile)
            pm2importer = Pm2Importer(
                pm2model,
                bl_name=f"{self.bl_name}{i:03}",
                texdir=self.texdir,
                tex_oldexporters_compat=self._tex_oldexporters_compat,
                import_vcol_alpha=self._import_vcol_alpha,
                matsettings_materials_to_reuse=self._matsettings_materials_to_reuse,
            )
            pm2importer.import_scene()
            # map-pm2 models are 4x too small compared to ghs
            pm2meshobj = pm2importer.bl_meshobj
            pm2meshobj.scale = (4, 4, 4)
            # also need to be rotated to correct axes
            pm2meshobj.rotation_euler = (radians(90), radians(180), 0)
