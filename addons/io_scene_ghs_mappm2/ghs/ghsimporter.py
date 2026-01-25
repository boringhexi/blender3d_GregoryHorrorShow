import json
from collections import defaultdict
from itertools import chain
from math import radians
from pathlib import Path
from typing import Optional

import bpy
from bpy.types import Action, Armature, FCurve, Material, Mesh, Object
from mathutils import Euler, Vector

from ..pm2.pm2importer import MatSettings, Pm2Importer
from ..pm2.pm2model import Pm2Model
from .meshposrot import mpr_from_file


def set_action_interpolation(bpyaction: Action):
    """set all pos/rot to LINEAR (but preserves CONSTANT) and all scale to CONSTANT"""
    for fcurve in bpyaction.fcurves:
        curvetype = fcurve.data_path.rsplit(".", maxsplit=1)[1]
        if curvetype in ("location", "rotation_euler", "value"):
            for point in fcurve.keyframe_points:
                if point.interpolation != "CONSTANT":
                    point.interpolation = "LINEAR"
        elif curvetype == "scale":
            for point in fcurve.keyframe_points:
                point.interpolation = "CONSTANT"
        else:
            continue


def set_action_1frame_interpolation(
    bpyaction: Action,
    frameidx: int,
    curvetypes: tuple[str, ...],
    interpolation: str,
    posebone: str = None,
):
    """set all fcurves at frameidx to interpolation

    posebone: if bpyaction belongs to an armature, can filter by posebone name
    """
    for fcurve in bpyaction.fcurves:
        curvetype = fcurve.data_path.rsplit(".", maxsplit=1)[1]
        if posebone is not None:
            fcurve_posebone = fcurve.data_path.split('"')[1]
        else:
            fcurve_posebone = None
        if curvetype in curvetypes and posebone == fcurve_posebone:
            frame_point = fcurve.keyframe_points[frameidx]
            frame_point.interpolation = interpolation


def has_scale_keyframe_at_frame(armobj, scalehide_bonename, frame):
    if armobj.animation_data is None:
        return False
    bpyaction = armobj.animation_data.action
    for fcurve in bpyaction.fcurves:
        poseandbonename, curvetype = fcurve.data_path.rsplit(".", maxsplit=1)
        bonename = poseandbonename.split('"')[1]
        if bonename != scalehide_bonename:
            continue
        if curvetype != "scale":
            continue
        for keyframe_point in fcurve.keyframe_points:
            match = keyframe_point.co[0] == frame
            if match:
                return True
    return False


def get_last_frame(bpyaction: Action) -> int:
    """return last frame of bpyaction (counting from 0), or 0 if it has no frames"""
    maxframe = 0
    for fcurve in bpyaction.fcurves:
        if not fcurve.keyframe_points:
            continue
        fcurve.update()  # ensure keyframes are in chronological order
        frame = int(fcurve.keyframe_points[-1].co[0])
        maxframe = max(frame, maxframe)
    return maxframe


class GhsImporter:
    def __init__(
        self,
        ghspath,
        pm2dir,
        mprdir,
        texdir,
        bl_name="",
        anim_method="1LONG",
        bone_parenting=True,
        oldexporter_compat=False,
        import_vcol_alpha=True,
    ):
        """

        :param ghspath:
        :param pm2dir:
        :param mprdir:
        :param anim_method: one of str: "1LONG" (all in a single timeline animation),
        "1LONG_EVERY100" (single timeline animation, each animation begins on a multiple
        of 100 frames), "DRIVER" (separate animations, uses a driver bone to drive shape
        keys), "TPOSE" (attempts to create a T-pose from the first animation), or "NONE"
        (rest pose taken from first anim if any)
        :param bl_name:
        :param bone_parenting: if True, parent meshes directly to armature bones. if
        False, weigh entire meshes to armature bones using vertex groups instead. Some
        exporters can't preserve the former, so the latter is a workaround.
        :param oldexporter_compat: if True, connect texture nodes directly to
            PBsdf, no vertex color or alpha clipping nodes
        :param import_vcol_alpha: if True, import vertex color alpha
        """
        self.ghspath = Path(ghspath)
        self.pm2dir = Path(pm2dir)
        self.mprdir = Path(mprdir) if mprdir is not None else None
        self.texdir = Path(texdir)
        self.bl_name = bl_name
        if anim_method not in (
            "DRIVER",
            "GLTF",
            "1LONG",
            "1LONG_EVERY100",
            "TPOSE",
            "NONE"
        ):
            raise ValueError(f"Unknown anim_method {anim_method!r}")
        self.anim_method = anim_method
        self._bone_parenting = bone_parenting
        self._oldexporter_compat = oldexporter_compat
        self._import_vcol_alpha = import_vcol_alpha
        self._matsettings_materials_to_reuse = None  # dict() to reuse materials

    def import_stuff(self):
        # load ghs data and MeshPosRots
        with open(self.ghspath, "rt") as ghsfile:
            ghsdata = json.load(ghsfile)
        boneparentinfo = ghsdata["bone_parenting_info"]
        defaultbodyparts = ghsdata["default_body_parts"]
        anims = ghsdata["animations"]
        mprs = []
        if self.mprdir is not None:
            mprpaths = self.mprdir.glob("*.mpr")
            for mprpath in sorted(mprpaths):
                with open(mprpath, "rb") as mprfile:
                    mpr = mpr_from_file(mprfile)
                mprs.append(mpr)

        # create armature
        armdata = bpy.data.armatures.new(self.bl_name)
        armobj = bpy.data.objects.new(armdata.name, armdata)
        bpy.context.collection.objects.link(armobj)
        bpy.context.view_layer.objects.active = armobj
        # rotate it to correct the axes
        armobj.rotation_euler = (radians(90), radians(180), 0)

        # create bones, set bone properties, and populate a mapping for later...
        boneidx_to_bonename = dict()
        for boneidx, boneparentdata in enumerate(boneparentinfo):
            bpy.ops.object.mode_set(mode="EDIT")
            bpyeditbone = armdata.edit_bones.new(name=f"b{boneidx:02}")
            bpyeditbone.tail = Vector((0, 1, 0))
            boneidx_to_bonename[boneidx] = bpyeditbone.name
            posebone_name = bpyeditbone.name
            bpy.ops.object.mode_set(mode="POSE")
            posebone = armobj.pose.bones[posebone_name]
            posebone.location = Vector(
                (
                    boneparentdata["posx"],
                    -boneparentdata["posy"],
                    boneparentdata["posz"],
                )
            )
        # ...then parent bones using that previously created mapping.
        bpy.ops.object.mode_set(mode="EDIT")
        for boneidx, boneparentdata in enumerate(boneparentinfo):
            parentidx = boneparentdata["parent"]
            if parentidx is None:
                continue
            bpybonename = boneidx_to_bonename[boneidx]
            bpyparentname = boneidx_to_bonename[parentidx]
            bpyeditbone = armdata.edit_bones[bpybonename]
            bpyparenteditbone = armdata.edit_bones[bpyparentname]
            bpyeditbone.parent = bpyparenteditbone

        # load default pm2 body parts
        pm2idx_to_meshobj = dict()
        pm2idx_to_scalehidebone = dict()
        original_default_pm2mesh_to_scalehide_bonename = dict()
        boneidx_to_default_scalehide_bonename = dict()
        default_scalehide_bonename_to_pm2mesh = dict()
        for boneidx, bodypart in enumerate(defaultbodyparts):
            # import default body part pm2
            pm2idx = bodypart["pm2"]
            if pm2idx is None:  # there is no default pm2 for this bone
                continue
            if pm2idx < 0:
                #  negative overwriting pm2idxs are presumably
                # specially handled by the game engine or ignored
                print(f"skipping default pm2idx {pm2idx} on bone {boneidx:02}")
                continue
            pm2path = self.pm2dir / f"{pm2idx:03x}.pm2"
            with open(pm2path, "rb") as fp:
                pm2model = Pm2Model.from_file(fp)
            pm2importer = Pm2Importer(
                pm2model,
                bl_name=f"{self.bl_name}_p{pm2idx:02x}",
                texdir=self.texdir,
                oldexporter_compat=self._oldexporter_compat,
                import_vcol_alpha=self._import_vcol_alpha,
                matsettings_materials_to_reuse=self._matsettings_materials_to_reuse,
            )
            pm2importer.import_scene()
            pm2meshobj = pm2importer.bl_meshobj

            if self.anim_method not in ("TPOSE", "NONE"):
                # create scalehide bone for this default body mesh
                bpy.ops.object.mode_set(mode="EDIT")
                scalehide_editbone = armobj.data.edit_bones.new(
                    name=f"b{boneidx:02}_p{pm2idx:02x}_hide"
                )
                scalehide_editbone.head = (0, 0, 0)
                scalehide_editbone.tail = (0, 1, 0)
                parent_bonename = boneidx_to_bonename[boneidx]
                parent_editbone = armobj.data.edit_bones[parent_bonename]
                scalehide_editbone.parent = parent_editbone
                scalehide_bonename = scalehide_editbone.name
                pm2idx_to_scalehidebone[pm2idx] = scalehide_bonename
                bpy.ops.object.mode_set(mode="POSE")
                pm2meshobj.parent = armobj
                if self._bone_parenting:
                    # parent mesh to bone. (some exporters can't export this accurately)
                    pm2meshobj.parent_type = "BONE"
                    pm2meshobj.parent_bone = scalehide_bonename
                    pm2meshobj.location[1] = -1
                else:
                    # Skinning, weigh entire mesh to the scalehide bone.
                    arm_modifier = pm2meshobj.modifiers.new("Armature", "ARMATURE")
                    arm_modifier.object = armobj
                    num_verts = len(pm2meshobj.data.vertices)
                    pm2meshobj.vertex_groups.new(name=scalehide_bonename)
                    pm2meshobj.vertex_groups[scalehide_bonename].add(
                        range(num_verts), 1, "ADD"
                    )
                # update important bone mappings
                boneidx_to_default_scalehide_bonename[boneidx] = scalehide_bonename
                default_scalehide_bonename_to_pm2mesh[scalehide_bonename] = (
                    pm2meshobj.data
                )
                # and important mesh mappings
                pm2idx_to_meshobj[pm2idx] = pm2meshobj
                original_default_pm2mesh_to_scalehide_bonename[pm2meshobj.data] = (
                    scalehide_bonename
                )
            else:
                # parent/weigh directly to the boneidx bone instead of to scalehide bone
                bpy.ops.object.mode_set(mode="POSE")
                boneidx_bonename = boneidx_to_bonename[boneidx]
                pm2meshobj.parent = armobj
                if self._bone_parenting:
                    pm2meshobj.parent_type = "BONE"
                    pm2meshobj.parent_bone = boneidx_bonename
                    pm2meshobj.location[1] = -1
                else:
                    arm_modifier = pm2meshobj.modifiers.new("Armature", "ARMATURE")
                    arm_modifier.object = armobj
                    num_verts = len(pm2meshobj.data.vertices)
                    if boneidx_bonename not in pm2meshobj.vertex_groups:
                        pm2meshobj.vertex_groups.new(name=boneidx_bonename)
                    pm2meshobj.vertex_groups[boneidx_bonename].add(
                        range(num_verts), 1, "ADD"
                    )
        bpy.ops.object.mode_set(mode="OBJECT")

        # get each anim's full length in advance by checking its mpr and keyframes.
        # it's used later for 1LONG anim concatenation and Action/NLA strip length.
        fullanimlengths = dict()
        for animidx, (mpr, anim) in enumerate(zip(mprs, anims)):
            full_anim_len = anim["anim_len"]
            for boneposedata in mpr.values():
                full_anim_len = max(full_anim_len, len(boneposedata["pos"]))
            for keyframes in anim["animation_data"]:
                for keyframe in keyframes:
                    keyframe_start = keyframe["keyframe_start"]
                    if keyframe_start < 999:
                        full_anim_len = max(full_anim_len, keyframe_start)
            fullanimlengths[animidx] = int(full_anim_len)

        frame_offset = 0
        pm2idx_to_driverbone = dict()
        animidx_to_scalehide_bones = defaultdict(list)
        boneidx_to_scalehide_bones = defaultdict(list)
        animated_shapekeys = set()
        deleteme_bonenames = []
        made_copies = False
        next_anim_start_frame = 0
        dummy_nla_arm = None  # to help add armature NLA tracks in reverse order
        shapekeys_to_dummy_nla = dict()  # to help add shape_keys' NLA tracks in reverse

        for animidx, (anim, mpr) in enumerate(zip(anims, mprs)):
            full_anim_len = fullanimlengths[animidx]
            is_last_animation = animidx + 1 == len(anims)

            # Clear out the previous animation's actions
            if self.anim_method in ("DRIVER", "GLTF"):
                if armobj.animation_data is not None:
                    armobj.animation_data.action = None
            if self.anim_method == "GLTF":
                for shape_keys in animated_shapekeys:  # shape keys from previous anim
                    shape_keys.animation_data.action = None
            animated_shapekeys = set()

            if self.anim_method in ("TPOSE", "NONE"):
                # pose armature using 1st frame of 1st mpr
                bpy.ops.object.mode_set(mode="POSE")
                for boneidx, boneposedata in mpr.items():
                    bpybonename = boneidx_to_bonename[boneidx]
                    bpyposebone = armobj.pose.bones[bpybonename]
                    bpyposebone.rotation_mode = (
                        "ZXY"  # pretty sure it's this and not ZYX
                    )
                    num_frames = len(boneposedata["pos"])
                    for frame in range(num_frames):
                        pos_raw = boneposedata["pos"][frame]
                        pos = Vector(pos_raw)
                        bpyposebone.location = pos
                        if self.anim_method == "NONE":
                            rot_raw = boneposedata["rot"][frame]
                            rot = Vector(rot_raw)
                            bpyposebone.rotation_euler = rot
                        break
                break
            else:
                # animate armature using mpr
                bpy.ops.object.mode_set(mode="POSE")
                # iterate through mpr bones, position pose bones
                for boneidx, boneposedata in mpr.items():
                    bpybonename = boneidx_to_bonename[boneidx]
                    bpyposebone = armobj.pose.bones[bpybonename]
                    bpyposebone.rotation_mode = (
                        "ZXY"  # pretty sure it's this and not ZYX
                    )
                    num_frames = len(boneposedata["pos"])
                    for frame in range(num_frames):
                        pos_raw = boneposedata["pos"][frame]
                        pos = Vector(pos_raw)
                        rot_raw = boneposedata["rot"][frame]
                        rot = Euler(rot_raw)
                        bpyposebone.location = pos
                        bpyposebone.rotation_euler = rot
                        bpyposebone.keyframe_insert(
                            "location", frame=frame_offset + frame
                        )
                        bpyposebone.keyframe_insert(
                            "rotation_euler", frame=frame_offset + frame
                        )
                        if frame == 0 and armobj.animation_data is not None:
                            # set this and future keyframes to LINEAR interpolation
                            bpyaction: Action = armobj.animation_data.action
                            set_action_1frame_interpolation(
                                bpyaction,
                                -1,
                                ("location", "rotation_euler"),
                                "LINEAR",
                                posebone=bpyposebone.name,
                            )
            if (
                self.anim_method in ("1LONG", "1LONG_EVERY100")
                and armobj.animation_data is not None
            ):
                bpyaction: Action = armobj.animation_data.action
                # prevent interpolation between consecutive animations by setting all
                # final keyframes in this mpr to CONSTANT
                # Note: a constant keyframe causes all future keyframes in the curve to
                # be created constant too, so later keyframes need to be set LINEAR
                set_action_1frame_interpolation(
                    bpyaction, -1, ("location", "rotation_euler"), "CONSTANT"
                )

            # keeping track of stuff to later prevent accidental interpolation between
            # consecutive animations in 1LONG/1LONG_EVERY100 modes
            shapekeys_already_keyframed = set()
            shapekeyactions = set()

            this_anim_length = max(anim["anim_len"] - 1, 1)
            # uncomment line below to make NLA anims the full anim length
            # this_anim_length = full_anim_len

            if (
                self.anim_method in ("DRIVER", "GLTF")
                and armobj.animation_data is not None
            ):
                # (a dummy nla track is used to add the real tracks in reverse order)
                if dummy_nla_arm is None:
                    dummy_nla_arm = armobj.animation_data.nla_tracks.new()
                # Get the armature Action...
                bpyaction: Action = armobj.animation_data.action
                anim_name = f"Anim{animidx:02}"
                bpyaction.name = anim_name
                # ...and put this Action into a new NLA track/strip
                bpy_nla_track = armobj.animation_data.nla_tracks.new(prev=dummy_nla_arm)
                bpy_nla_track.name = anim_name
                bpy_nla_strip = bpy_nla_track.strips.new(anim_name, 0, bpyaction)
                bpy_nla_strip.name = anim_name  # because it didn't stick the first time
                bpy_nla_strip.action_frame_end = this_anim_length
                # lock and mute all NLA tracks, just like the glTF importer. This
                # way an animation only plays when it is starred/solo'd in the GUI
                bpy_nla_track.mute = True
                bpy_nla_track.lock = True

            this_anim_start_frame = next_anim_start_frame
            next_anim_start_frame = 0
            last_frame = full_anim_len
            if self.anim_method == "1LONG":
                next_anim_start_frame = frame_offset + last_frame + 1
            elif self.anim_method == "1LONG_EVERY100":
                next_anim_start_frame = (frame_offset + last_frame + 100) // 100 * 100
            current_anim_endhide_frame = next_anim_start_frame

            if not anim["animation_data"]:
                # animation is made up entirely of default body parts
                animidx_to_scalehide_bones[animidx].extend(
                    boneidx_to_default_scalehide_bonename.values()
                )

            for boneidx, keyframes in enumerate(anim["animation_data"]):
                if self.anim_method in ("TPOSE", "NONE"):
                    break

                parent_bonename = boneidx_to_bonename[boneidx]

                if not keyframes:
                    default_scalehide_bonename = (
                        boneidx_to_default_scalehide_bonename.get(boneidx)
                    )
                    if default_scalehide_bonename is not None:
                        animidx_to_scalehide_bones[animidx].append(
                            boneidx_to_default_scalehide_bonename[boneidx]
                        )
                        default_scalehide_bone = armobj.pose.bones[
                            default_scalehide_bonename
                        ]
                        default_scalehide_bone.scale = (1, 1, 1)
                        default_scalehide_bone.keyframe_insert(
                            "scale", frame=this_anim_start_frame
                        )
                    continue

                prev_pm2idx = None
                first_delta_frame = None
                for keyframeidx, (keyframe, next_keyframe) in enumerate(
                    zip(keyframes, keyframes[1:] + [None])
                ):
                    keyframe_start = keyframe["keyframe_start"]
                    if next_keyframe is not None:
                        next_keyframe_start = next_keyframe["keyframe_start"]
                    else:
                        next_keyframe_start = None
                    if keyframe_start >= 999:
                        break
                    pm2idx = keyframe["pm2"]
                    next_pm2idx = next_keyframe["pm2"]

                    # determine animation start/end for this keyframe
                    if keyframe["interp_type"] == 0:
                        # formerly start=end=0, see if this works instead
                        interp_start = interp_end = keyframe["interp_start"]
                    elif keyframe["interp_type"] == 1:
                        interp_start, interp_end = 0, 1
                    elif keyframe["interp_type"] == 2:
                        interp_start = keyframe["interp_start"]
                        interp_end = interp_start + keyframe["interp_delta"]
                    elif keyframe["interp_type"] == -1:
                        interp_start, interp_end = 1, 0
                    # elif keyframe["interp_type"] == -2:
                    else:
                        print(
                            "WARNING: unknown interpolation type "
                            f'{keyframe["interp_type"]}'
                        )
                        interp_start = interp_end = 0

                    # handle situations where keyframes are non-increasing frame order
                    if next_keyframe_start is not None:
                        if first_delta_frame is not None:
                            interpscale_start = 1 - (
                                next_keyframe_start - first_delta_frame
                            ) / (next_keyframe_start - keyframe_start)
                            interp_start = interp_start + interpscale_start * (
                                interp_end - interp_start
                            )
                            keyframe_start = first_delta_frame

                        if next_keyframe_start <= keyframe_start:
                            first_delta_frame = keyframe_start + 1
                        else:
                            first_delta_frame = None

                    interp_shapekey_constant = interp_start == interp_end

                    # create scalehide bone or retrieve existing one
                    if pm2idx in pm2idx_to_scalehidebone:
                        scalehide_bonename = pm2idx_to_scalehidebone[pm2idx]
                        repeated_bone = True
                    else:
                        scalehide_bonename = None
                        repeated_bone = False
                    if scalehide_bonename is None or scalehide_bonename not in [
                        bone.name for bone in armobj.data.bones
                    ]:
                        bpy.ops.object.mode_set(mode="EDIT")
                        if pm2idx is not None and pm2idx >= 0:
                            scalehide_editbone_name = (
                                f"b{boneidx:02}_p{pm2idx:02x}_hide"
                            )
                            scalehide_editbone = armobj.data.edit_bones.new(
                                name=scalehide_editbone_name
                            )
                        else:
                            # no pm2 submesh is displayed this keyframe. We still place
                            # a scalehide bone for now to assist with calculating the
                            # visibility of the default pm2's scalehide bone later.
                            scalehide_editbone_name = f"a{animidx:02}_b{boneidx:02}_k{int(keyframe_start)}_DELETEME"
                            scalehide_editbone = armobj.data.edit_bones.new(
                                name=scalehide_editbone_name
                            )
                            deleteme_bonenames.append(scalehide_editbone.name)
                        scalehide_editbone.head = (0, 0, 0)
                        scalehide_editbone.tail = (0, 1, 0)
                        parent_editbone = armobj.data.edit_bones[parent_bonename]
                        scalehide_editbone.parent = parent_editbone
                        scalehide_bonename = scalehide_editbone.name
                        # save this new scalehide bone to be retrieved later unless it's
                        # a DELETEME bone, in which case we want a new one each time
                        if pm2idx is not None and pm2idx >= 0:
                            pm2idx_to_scalehidebone[pm2idx] = scalehide_bonename
                        repeated_bone = False
                    animidx_to_scalehide_bones[animidx].append(scalehide_bonename)
                    boneidx_to_scalehide_bones[boneidx].append(scalehide_bonename)

                    # and animate the scalehide bone
                    bpy.ops.object.mode_set(mode="POSE")
                    scalehide_posebone = armobj.pose.bones[scalehide_bonename]

                    if self.anim_method in ("1LONG", "1LONG_EVERY100"):
                        # hide later pm2s from previous animations
                        scalehide_posebone.scale = (0, 0, 0)
                        if not repeated_bone:
                            scalehide_posebone.keyframe_insert("scale", frame=0)
                        # hide previous pm2s from later animations
                        if not is_last_animation:
                            scalehide_posebone.keyframe_insert(
                                "scale", frame=current_anim_endhide_frame
                            )
                        # and hide this boneidx's default scalehide too, if it isn't
                        # supposed to be shown this anim
                        default_scalehide_bonename = (
                            boneidx_to_default_scalehide_bonename.get(boneidx)
                        )
                        if (
                            default_scalehide_bonename is not None
                            and scalehide_posebone.name != default_scalehide_bonename
                        ):
                            default_scalehide_bone = armobj.pose.bones[
                                default_scalehide_bonename
                            ]
                            default_scalehide_bone.scale = (0, 0, 0)
                            default_scalehide_bone.keyframe_insert(
                                "scale", frame=this_anim_start_frame
                            )
                        # place additional keyframe at anim start if the first keyframe
                        # is late; helps prevent pm2 from being hidden after the end of
                        # the previous animation
                        if keyframeidx == 0 and keyframe_start > 0:
                            scalehide_posebone.scale = (1, 1, 1)
                            scalehide_posebone.keyframe_insert(
                                "scale", frame=frame_offset
                            )

                    # place keyframe where scalehide bone is visible (scaled to 1)
                    scalehide_posebone.scale = (1, 1, 1)
                    scalehide_posebone.keyframe_insert(
                        "scale", frame=frame_offset + keyframe_start
                    )
                    # place keyframe where scalehide bone is hidden (scaled to 0)
                    if prev_pm2idx != pm2idx:
                        scalehide_posebone.scale = (0, 0, 0)
                        if keyframe_start > 0 and keyframeidx > 0:
                            scalehide_posebone.keyframe_insert(
                                "scale", frame=frame_offset + keyframe_start - 1
                            )
                    if pm2idx != next_pm2idx:
                        scalehide_posebone.scale = (0, 0, 0)
                        if (
                            next_keyframe_start is not None
                            and next_keyframe_start < 999
                        ):
                            scalehide_posebone.keyframe_insert(
                                "scale",
                                frame=frame_offset + next_keyframe_start,
                            )
                    # extra case where scalehide bone is hidden: overwritten by a later
                    # keyframe that has a lower or equal keyframe_start
                    if first_delta_frame is not None:
                        scalehide_posebone.scale = (0, 0, 0)
                        scalehide_posebone.keyframe_insert(
                            "scale",
                            frame=frame_offset + first_delta_frame,
                        )

                    prev_pm2idx = pm2idx

                    # If this keyframe has a model, load model for this keyframe or
                    # retrieve the already-loaded model
                    if pm2idx is not None:
                        if pm2idx in pm2idx_to_meshobj:
                            pm2meshobj = pm2idx_to_meshobj[pm2idx]
                        else:
                            if pm2idx < 0:
                                # negative overwriting pm2idxs are presumably
                                # specially handled by the game engine or ignored
                                print(
                                    f"skipping overwriting pm2idx {pm2idx} "
                                    f"on bone {boneidx:02}"
                                )
                                continue
                            pm2path = self.pm2dir / f"{pm2idx:03x}.pm2"
                            with open(pm2path, "rb") as fp:
                                pm2model = Pm2Model.from_file(fp)
                            pm2name = f"{self.bl_name}_p{pm2idx:02x}"
                            pm2importer = Pm2Importer(
                                pm2model,
                                bl_name=pm2name,
                                texdir=self.texdir,
                                oldexporter_compat=self._oldexporter_compat,
                                import_vcol_alpha=self._import_vcol_alpha,
                                matsettings_materials_to_reuse=self._matsettings_materials_to_reuse,
                            )
                            pm2importer.import_scene()
                            pm2meshobj = pm2importer.bl_meshobj

                            # once again, parent/weigh to scalehide bone
                            # bpy.ops.object.mode_set(mode="POSE")  # already Pose mode
                            pm2meshobj.parent = armobj
                            if self._bone_parenting:
                                pm2meshobj.parent_type = "BONE"
                                pm2meshobj.parent_bone = scalehide_bonename
                                pm2meshobj.location[1] = -1
                            else:
                                arm_modifier = pm2meshobj.modifiers.new(
                                    "Armature", "ARMATURE"
                                )
                                arm_modifier.object = armobj
                                num_verts = len(pm2meshobj.data.vertices)
                                pm2meshobj.vertex_groups.new(name=scalehide_bonename)
                                pm2meshobj.vertex_groups[scalehide_bonename].add(
                                    range(num_verts), 1, "ADD"
                                )
                            # update important mesh mapping
                            pm2idx_to_meshobj[pm2idx] = pm2meshobj

                        # animate shapekey of model
                        if pm2meshobj.data.shape_keys is not None:
                            shapekey = pm2meshobj.data.shape_keys.key_blocks["Anim"]
                            animated_shapekeys.add(pm2meshobj.data.shape_keys)
                            if self.anim_method == "DRIVER":
                                if pm2idx in pm2idx_to_driverbone:
                                    driver_bonename = pm2idx_to_driverbone[pm2idx]
                                else:
                                    # Create and parent driver bone
                                    bpy.ops.object.mode_set(mode="EDIT")
                                    driver_editbone = armobj.data.edit_bones.new(
                                        name=f"b{boneidx:02}_p{pm2idx:02x}_driver"
                                    )
                                    driver_editbone.head = (0, 0, 0)
                                    driver_editbone.tail = (0, 1, 0)
                                    driver_editbone.parent = armobj.data.edit_bones[
                                        scalehide_bonename
                                    ]
                                    driver_bonename = driver_editbone.name

                                    # Link driver bone to shape key
                                    bpy.ops.object.mode_set(mode="OBJECT")
                                    fcurve = pm2meshobj.data.shape_keys.key_blocks[
                                        "Anim"
                                    ].driver_add("value")
                                    driver = fcurve.driver
                                    driver.expression = "var"
                                    variable = driver.variables.new()
                                    variable.name = "var"
                                    variable.type = "TRANSFORMS"
                                    target = variable.targets[0]
                                    target.id = armobj
                                    target.bone_target = driver_bonename
                                    target.transform_space = "LOCAL_SPACE"
                                    target.transform_type = "LOC_X"
                                    pm2idx_to_driverbone[pm2idx] = driver_bonename

                                # Animate driver bone
                                bpy.ops.object.mode_set(mode="POSE")
                                driver_posebone = armobj.pose.bones[driver_bonename]
                                driver_posebone.location.x = interp_start
                                driver_posebone.keyframe_insert(
                                    "location",
                                    frame=frame_offset + keyframe_start,
                                )
                                bpyaction = armobj.animation_data.action
                                if interp_shapekey_constant:
                                    set_action_1frame_interpolation(
                                        bpyaction, -1, ("location",), "CONSTANT"
                                    )
                                else:
                                    set_action_1frame_interpolation(
                                        bpyaction, -1, ("location",), "LINEAR"
                                    )
                                if (
                                    next_keyframe_start is not None
                                    and next_keyframe_start < 999
                                ):
                                    driver_posebone.location.x = interp_end
                                    driver_posebone.keyframe_insert(
                                        "location",
                                        frame=frame_offset + next_keyframe_start,
                                    )

                            else:
                                shapekey.value = interp_start
                                shapekey.keyframe_insert(
                                    "value",
                                    frame=frame_offset + keyframe_start,
                                )
                                skaction = (
                                    pm2meshobj.data.shape_keys.animation_data.action
                                )
                                skaction.name = f"Anim{animidx:02}_{pm2meshobj.name}"
                                if interp_shapekey_constant:
                                    set_action_1frame_interpolation(
                                        skaction, -1, ("value",), "CONSTANT"
                                    )
                                else:
                                    set_action_1frame_interpolation(
                                        skaction, -1, ("value",), "LINEAR"
                                    )
                                shapekeyactions.add(skaction)
                                if self.anim_method in ("1LONG", "1LONG_EVERY100"):
                                    # prevent shapekey value from being held over from
                                    # the previous animation by setting an extra
                                    # keyframe at anim start
                                    if (
                                        shapekey not in shapekeys_already_keyframed
                                        and frame_offset + keyframe_start > 0
                                    ):
                                        shapekey.keyframe_insert(
                                            "value", frame=frame_offset
                                        )
                                # place additional keyframe at anim start if the first
                                # keyframe is late; helps prevent glTF re-import issues
                                if self.anim_method == "GLTF":
                                    if (
                                        shapekey not in shapekeys_already_keyframed
                                        and keyframe_start > 0
                                    ):
                                        shapekey.keyframe_insert("value", frame=0)
                                shapekeys_already_keyframed.add(shapekey)

                                if (
                                    next_keyframe is not None
                                    and next_keyframe_start < 999
                                ):
                                    shapekey.value = interp_end
                                    shapekey.keyframe_insert(
                                        "value",
                                        frame=frame_offset + next_keyframe_start,
                                    )

            if self.anim_method in ("1LONG", "1LONG_EVERY100"):
                frame_offset = next_anim_start_frame
                # prevent interpolation of shapekeys between consecutive animations
                for skaction in shapekeyactions:
                    set_action_1frame_interpolation(
                        skaction, -1, ("value",), "CONSTANT"
                    )
            if self.anim_method in (
                "1LONG",
                "1LONG_EVERY100",
                "GLTF",
            ):
                for skaction in shapekeyactions:
                    set_action_interpolation(skaction)

            # Now that an anim is done, group the armature and shapekey anims together
            # by putting them in NLA tracks with the same name.
            if self.anim_method == "GLTF":
                for shape_keys in animated_shapekeys:
                    # (a dummy nla track is used to add the real tracks in reverse)
                    dummy_nla_sks = shapekeys_to_dummy_nla.get(shape_keys)
                    if dummy_nla_sks is None:
                        dummy_nla_sks = shape_keys.animation_data.nla_tracks.new()
                        shapekeys_to_dummy_nla[shape_keys] = dummy_nla_sks
                    # Get the shape_keys Action...
                    skaction: Action = shape_keys.animation_data.action
                    if skaction is None:
                        continue
                    anim_name = f"Anim{animidx:02}"
                    # ...and put this Action into a new NLA track/strip
                    bpy_nla_track = shape_keys.animation_data.nla_tracks.new(prev=dummy_nla_sks)
                    bpy_nla_track.name = anim_name
                    bpy_nla_strip = bpy_nla_track.strips.new(anim_name, 0, skaction)
                    bpy_nla_strip.name = anim_name  # didn't stick the first time
                    action_last_frame = get_last_frame(skaction)
                    bpy_nla_strip.action_frame_end = min(
                        action_last_frame, this_anim_length
                    )

                    if action_last_frame > this_anim_length:
                        # Set manual frame range if action is too long
                        skaction.use_frame_range = True
                        skaction.frame_range = (0, this_anim_length)

                    # lock and mute all NLA tracks,just like the glTF importer. This
                    # way an anim only plays when it is starred/solo'd in the GUI
                    bpy_nla_track.mute = True
                    bpy_nla_track.lock = True

        if self.anim_method in ("DRIVER", "GLTF"):
            # remove the empty dummy track before we start messing with NLA stuff
            if dummy_nla_arm is not None:
                armobj.animation_data.nla_tracks.remove(dummy_nla_arm)
            # (let's remove all the shapekeys' dummy tracks too while we're at it)
            for shape_keys, dummy_nla_sks in shapekeys_to_dummy_nla.items():
                shape_keys.animation_data.nla_tracks.remove(dummy_nla_sks)

            # scale to 0 all scalehide bones not in the current animation
            bpy.ops.object.mode_set(mode="POSE")
            all_actions = []
            if armobj.animation_data is not None:
                # armature's nla tracks were added in reverse order
                arm_nla_tracks_in_order = reversed(armobj.animation_data.nla_tracks)
                for animidx, bpy_nla_track in enumerate(arm_nla_tracks_in_order):
                    bpy_nla_strip = bpy_nla_track.strips[0]
                    bpyaction = bpy_nla_strip.action
                    armobj.animation_data.action = bpyaction
                    all_actions.append(bpyaction)

                    # while we're at it, now that fcurves' lengths are finalized by now,
                    # shorten NLA strips & Actions to desired animation length
                    this_anim_length = bpy_nla_strip.action_frame_end
                    action_last_frame = get_last_frame(bpyaction)
                    bpy_nla_strip.action_frame_end = min(
                        action_last_frame, this_anim_length
                    )
                    if action_last_frame > this_anim_length:
                        # Set manual frame range if Action is too long
                        bpyaction.use_frame_range = True
                        bpyaction.frame_start = 0
                        bpyaction.frame_end = this_anim_length

                    # for all scalehide bones not in this animation, set frame 0 to
                    # scale 0 if there isn't already a scale keyframe there
                    this_anim_scalehide_bones = set(animidx_to_scalehide_bones[animidx])
                    all_scalehide_bones = set(
                        chain.from_iterable(animidx_to_scalehide_bones.values())
                    )
                    not_this_anim_scalehide_bones = all_scalehide_bones.difference(
                        this_anim_scalehide_bones
                    )
                    # set frame 0 to scale 0 if isn't already a scale keyframe there
                    for scalehide_bonename in not_this_anim_scalehide_bones:
                        scalehide_posebone = armobj.pose.bones[scalehide_bonename]
                        if not has_scale_keyframe_at_frame(
                            armobj, scalehide_bonename, 0
                        ):
                            scalehide_posebone.scale = (0, 0, 0)
                            scalehide_posebone.keyframe_insert("scale", frame=0)

                    # set frame-by-frame visibility of each default pm2's scalehide bone
                    # and other fcurve set/cleanup
                    set_action_interpolation(bpyaction)
                    simplify_scalehide_fcurves(bpyaction)
                    self.set_default_scalehide_bones_visibility(
                        boneidx_to_default_scalehide_bonename,
                        boneidx_to_scalehide_bones,
                        bpyaction,
                    )
                    set_action_interpolation(bpyaction)

            delete_unused_default_pm2meshes(
                default_scalehide_bonename_to_pm2mesh, armobj, all_actions
            )
            delete_deleteme_bones(deleteme_bonenames, armobj, all_actions)

        if self.anim_method in ("1LONG", "1LONG_EVERY100"):
            # set frame-by-frame visibility of each default pm2's scalehide bone
            # and other fcurve set/cleanup
            if armobj.animation_data is not None:
                bpyaction = armobj.animation_data.action
                set_action_interpolation(bpyaction)
                simplify_scalehide_fcurves(bpyaction)
                self.set_default_scalehide_bones_visibility(
                    boneidx_to_default_scalehide_bonename,
                    boneidx_to_scalehide_bones,
                    bpyaction,
                )
                set_action_interpolation(bpyaction)
            delete_unused_default_pm2meshes(
                default_scalehide_bonename_to_pm2mesh, armobj
            )
            delete_deleteme_bones(deleteme_bonenames, armobj)

        # Clear out the final animation's actions
        if self.anim_method in ("DRIVER", "GLTF"):
            if armobj.animation_data is not None:
                armobj.animation_data.action = None
        if self.anim_method == "GLTF":
            for shape_keys in animated_shapekeys:  # shape keys from final anim
                shape_keys.animation_data.action = None

        bpy.ops.object.mode_set(mode="OBJECT")

    def set_default_scalehide_bones_visibility(
        self,
        boneidx_to_default_scalehide_bonename,
        boneidx_to_scalehide_bones,
        bpyaction: Action,
    ):
        # calc and set frame-by-frame visibility of each default pm2's scalehide bone
        bpy.ops.object.mode_set(mode="POSE")
        for (
            boneidx,
            default_scalehide_bonename,
        ) in boneidx_to_default_scalehide_bonename.items():
            overwriting_scalehide_bonenames = boneidx_to_scalehide_bones.get(boneidx)
            if overwriting_scalehide_bonenames is None:
                continue

            # get the fcurves we'll need
            default_fcurves = [None, None, None]
            overwriting_fcurves_x = []
            for fcurve in bpyaction.fcurves:
                poseandbonename, curvetype = fcurve.data_path.rsplit(".", maxsplit=1)
                bonename = poseandbonename.split('"')[1]
                if curvetype == "scale":
                    if bonename == default_scalehide_bonename:
                        # there are 3 fcurves, x y and z scale
                        default_fcurves[fcurve.array_index] = fcurve
                    elif (
                        bonename in overwriting_scalehide_bonenames
                        and fcurve.array_index == 0
                    ):
                        overwriting_fcurves_x.append(fcurve)

            # create default scalehide fcurves if they don't already exist
            for axis, default_fcurve in enumerate(default_fcurves):
                if default_fcurve is None:
                    data_path = f'pose.bones["{default_scalehide_bonename}"].scale'
                    default_fcurve = bpyaction.fcurves.new(data_path, index=axis)
                    default_fcurves[axis] = default_fcurve

            self.calc_default_scalehide_fcurves(default_fcurves, overwriting_fcurves_x)

    def calc_default_scalehide_fcurves(
        self,
        default_fcurves: tuple[FCurve, FCurve, FCurve],
        overwriting_fcurves: list[FCurve],
    ) -> None:
        """modify default_fcurves to display correctly around overwriting_fcurves

        In plain speak, this makes the default body part meshes display only when needed
        (instead of being displayed all the time)

        Prerequisite: all fcurves must be already in CONSTANT interpolation, with all
        keyframe values being either 0 or 1.

        :param default_fcurves: list [x,y,z] of scale fcurves of the default scalehide
            bone. These fcurves will be modified in-place
        :param overwriting_fcurves: list of scalehide fcurves of the overwriting pm2s
        """

        default_timeline = fcurve_to_timeline(default_fcurves[0])
        if (
            self.anim_method in ("1LONG", "1LONG_EVERY100")
            and default_timeline
            and default_timeline[0][0] != 0
        ):
            # fixes a bug in 1LONG mode where when the same pm2mesh is both a default
            # and overwriting pm2mesh, it would not be properly hidden until later in
            # the timeline.
            default_timeline = [(0, 0)] + default_timeline
        overwriting_timelines = [fcurve_to_timeline(fc) for fc in overwriting_fcurves]
        overwriting_sum = sum_scalehide_timelines(overwriting_timelines)
        inverted_overwriting_sum = invert_scalehide_timeline(overwriting_sum)
        new_default_timeline = sum_scalehide_timelines(
            [default_timeline, inverted_overwriting_sum]
        )
        new_default_timeline = simplify_scalehide_timeline(new_default_timeline)
        for default_fcurve in default_fcurves:
            timeline_into_fcurve(new_default_timeline, default_fcurve)


def fcurve_to_timeline(fcurve: FCurve) -> list[tuple[int, float]]:
    """get timeline from fcurve keyframes"""
    fcurve.update()  # ensure keyframes are in increasing frame order
    num_keyframes = len(fcurve.keyframe_points)
    if num_keyframes == 0:
        return []
    seq = [0] * 2 * num_keyframes
    fcurve.keyframe_points.foreach_get("co", seq)
    # seq is now populated. "chunk" seq into pairs and return
    return [(seq[i], seq[i + 1]) for i in range(0, len(seq), 2)]


def sum_scalehide_timelines(
    timelines: list[list[tuple[int, float]]]
) -> list[tuple[int, float]]:
    """return the sum of all the timelines (for a given definition of "sum")

    :param timelines: list of timelines to be summed
    :return: timeline where each value is either 0 (if all timelines at that frame were
        0) or 1 (if any timeline at that frame was 1)
    """
    timelines = [x.copy() for x in timelines if x]
    if len(timelines) == 1:
        return timelines[0]
    if len(timelines) == 0:
        return []

    # If a timeline lacks a keyframe at frame 0, give it one
    for timeline in timelines:
        firstframe, firstval = timeline[0]
        if firstframe > 0:
            timeline.insert(0, (0, firstval))

    # create a mapping to be used later
    last_keyframe = 0
    framenum_to_keyframed_timeline_indices_and_vals = defaultdict(list)
    for i, timeline in enumerate(timelines):
        for framenum, value in timeline:
            framenum_to_keyframed_timeline_indices_and_vals[framenum].append((i, value))
            last_keyframe = max(last_keyframe, framenum)

    current_val_per_timelines = [1] * len(timelines)

    summed_timeline = []
    for framenum in range(int(last_keyframe + 1)):
        keyframed_timeline_indices_and_vals = (
            framenum_to_keyframed_timeline_indices_and_vals.get(framenum)
        )
        if keyframed_timeline_indices_and_vals is None:
            continue
        for timeline_i, value in keyframed_timeline_indices_and_vals:
            current_val_per_timelines[timeline_i] = value
        summed_value = max(current_val_per_timelines)
        summed_timeline.append((framenum, summed_value))
    return summed_timeline


def invert_scalehide_timeline(
    timeline: list[tuple[int, float]]
) -> list[tuple[int, float]]:
    """invert the values of timeline (for a given definition of "invert")

    :param timeline: list of (framenum, value)
    :return: list of (framenum, value) where value is 0 if it was 1, or 1 if it was 0
    """
    ret = []
    for framenum, value in timeline:
        if value == 0:
            value = 1
        else:  # elif value == 1:
            value = 0
        ret.append((framenum, value))
    return ret


def simplify_scalehide_timeline(
    timeline: list[tuple[int, float]]
) -> list[tuple[int, float]]:
    simplified_timeline = []
    previous_value = None
    for framenum, value in timeline:
        if value != previous_value:
            simplified_timeline.append((framenum, value))
        previous_value = value
    return simplified_timeline


def timeline_into_fcurve(timeline: list[tuple[int, float]], fcurve: FCurve) -> None:
    """insert timeline into fcurve as keyframes

    modifies fcurve in-place, replacing its keyframes with those from timeline
    """
    num_keyframes = len(timeline)
    seq = list(chain.from_iterable(timeline))
    fcurve.keyframe_points.clear()
    fcurve.keyframe_points.add(count=num_keyframes)
    fcurve.keyframe_points.foreach_set("co", seq)


def simplify_scalehide_fcurves(bpyaction: Action):
    for fcurve in bpyaction.fcurves:
        if fcurve.data_path.endswith("scale"):
            timeline = fcurve_to_timeline(fcurve)
            simplified_timeline = simplify_scalehide_timeline(timeline)
            timeline_into_fcurve(simplified_timeline, fcurve)


def delete_unused_default_pm2meshes(
    default_scalehide_bonename_to_pm2mesh: dict[str, Mesh],
    armobj: Object,
    actions: Optional[list[Action]] = None,
) -> None:
    """remove any pm2mesh and scalehide bone that is always hidden (across all Actions)

    Also remove any fcurves associated with the removed bones

    :param default_scalehide_bonename_to_pm2mesh: dict of bone names to mesh datablocks.
    Used to remove any pm2mesh that corresponds to an always-hidden scalehide bone.
    :param armobj: armature object. used to delete unused bones
    :param actions: if None, use armobj's action. Scan through all Actions to see
    whether a given scalehide bone is used in any of them or not
    """
    if actions is None:
        if armobj.animation_data is None:
            actions = []
        else:
            actions = [armobj.animation_data.action]
    original_mode = bpy.context.object.mode
    sets_of_editbones_to_remove = []
    sets_of_pm2meshes_to_remove = []
    fcurve_datapaths_unused_in_actions = defaultdict(list)
    bpy.ops.object.mode_set(mode="POSE")
    for action in actions:
        editbones_to_remove = set()
        pm2meshes_to_remove = set()
        for fcurve in action.fcurves:
            poseandbonename, curvetype = fcurve.data_path.rsplit(".", maxsplit=1)
            fcurve_bonename = poseandbonename.split('"')[1]
            if fcurve_bonename in default_scalehide_bonename_to_pm2mesh:
                if fcurve_is_all0(fcurve):
                    # If this condition is True every time (i.e. for every Action),
                    # then later we'll end up removing this editbone, Mesh, and fcurves
                    editbones_to_remove.add(fcurve_bonename)
                    pm2mesh = default_scalehide_bonename_to_pm2mesh[fcurve_bonename]
                    pm2meshes_to_remove.add(pm2mesh)
                    fcurve_datapaths_unused_in_actions[fcurve.data_path].append(True)
                else:
                    fcurve_datapaths_unused_in_actions[fcurve.data_path].append(False)
        sets_of_editbones_to_remove.append(editbones_to_remove)
        sets_of_pm2meshes_to_remove.append(pm2meshes_to_remove)

    if sets_of_pm2meshes_to_remove:
        bpy.ops.object.mode_set(mode="OBJECT")
        for pm2mesh in set.intersection(*sets_of_pm2meshes_to_remove):
            bpy.data.meshes.remove(pm2mesh)

    if sets_of_editbones_to_remove:
        bpy.ops.object.mode_set(mode="EDIT")
        arm: Armature = armobj.data
        for edit_bone in arm.edit_bones:
            if edit_bone.name in set.intersection(*sets_of_editbones_to_remove):
                arm.edit_bones.remove(edit_bone)

    for data_path, unused_in_actions in fcurve_datapaths_unused_in_actions.items():
        if all(unused_in_actions):
            for action in actions:
                # fcurves.find only returns the first axis's fcurve, unless you specify
                for axis in range(3):
                    fcurve = action.fcurves.find(data_path, index=axis)
                    if fcurve is not None:
                        action.fcurves.remove(fcurve)

    bpy.ops.object.mode_set(mode=original_mode)


def fcurve_is_all0(fcurve: FCurve) -> bool:
    timeline = fcurve_to_timeline(fcurve)
    if not timeline:
        return False
    framenums, values = zip(*timeline)
    return not any(values)


def delete_deleteme_bones(
    deleteme_bonenames: list[str],
    armobj: Object,
    actions: Optional[list[Action]] = None,
) -> None:
    """remove any bone whose name is in deleteme_bonenames, as well as their FCurves

    :param deleteme_bonenames: list of bone names
    :param armobj: armature object containing the bones
    :param actions: if None, use armobj's action. FCurves associated with the removed
    bones will be removed from all actions.
    """
    if actions is None:
        if armobj.animation_data is None:
            actions = []
        else:
            actions = [armobj.animation_data.action]
    arm: Armature = armobj.data
    original_mode = bpy.context.object.mode

    bpy.ops.object.mode_set(mode="EDIT")
    for edit_bone in arm.edit_bones:
        if edit_bone.name in deleteme_bonenames:
            arm.edit_bones.remove(edit_bone)

    for i, action in enumerate(actions):
        fcurves_to_remove = []
        for fcurve in action.fcurves:
            poseandbonename, curvetype = fcurve.data_path.rsplit(".", maxsplit=1)
            fcurve_bonename = poseandbonename.split('"')[1]
            if fcurve_bonename in deleteme_bonenames:
                fcurves_to_remove.append(fcurve)
        for fcurve in fcurves_to_remove:
            action.fcurves.remove(fcurve)

    bpy.ops.object.mode_set(mode=original_mode)
