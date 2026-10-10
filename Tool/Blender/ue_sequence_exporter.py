# ======================================================================
# UE Sequencer Exporter - STAGE 1 (Blender side)
# ----------------------------------------------------------------------
# What this does:
#   1. Adds a panel in the 3D Viewport sidebar (press N, tab "UE Sequencer")
#   2. Lets you pick a character armature and a camera
#   3. Checks the scene for common problems and lists them live
#   4. Exports TWO files into a folder you choose:
#        <ShotName>.fbx         -> character mesh + skeleton + baked animation
#        <ShotName>_camera.json -> per-frame camera transform + lens data,
#                                  already converted to Unreal's space/units
# Stage 2 (Unreal side) will read these two files and build the Level Sequence.
# ======================================================================

# bl_info is a dictionary Blender reads to show the addon in Preferences > Add-ons
bl_info = {
    "name": "UE Sequencer Exporter",  # the name shown in the add-ons list
    "author": "Your Name",  # put your own name here
    "version": (0, 1, 0),  # addon version, bump it as you improve the tool
    "blender": (3, 6, 0),  # the oldest Blender version this addon supports
    "location": "View3D > Sidebar > UE Sequencer",  # where to find the panel
    "description": "Exports a character animation and camera for Unreal Sequencer",  # tooltip text
    "category": "Import-Export",  # the add-on category it is filed under
}

import bpy  # Blender's Python API, gives access to scenes, objects and operators
import json  # standard library module used to write the camera data file
import os  # standard library module for folders and file names
import shutil  # standard library module for copying texture files
import math  # standard library module for degrees, radians, atan2 and sqrt
from mathutils import Vector  # Blender's 3D vector class, handy for vector maths

# Unreal Engine works in centimetres, Blender works in metres by default
METRES_TO_CM = 100.0  # multiply a Blender length by this to get Unreal units


# ----------------------------------------------------------------------
# SMALL HELPER FUNCTIONS
# ----------------------------------------------------------------------

def clean_path(path):
    """Turn a Blender path into an absolute path that only uses forward slashes."""
    absolute = bpy.path.abspath(path)  # converts Blender relative paths like "//out" into full paths
    forward = absolute.replace("\\", "/")  # swap every backslash for a forward slash (avoids Windows escape errors)
    return forward.rstrip("/")  # remove any trailing slash so we can add "/" + filename ourselves


def clean_name(name):
    """Make a safe file name: keep letters, digits, underscore and dash only."""
    safe = ""  # start with an empty string that we will build up
    for ch in name.strip():  # look at every character in the trimmed name
        if ch.isalnum() or ch in "_-":  # letters, digits, underscore and dash are fine
            safe += ch  # keep the character as it is
        else:  # anything else (spaces, slashes, symbols) is unsafe in a file name
            safe += "_"  # replace it with an underscore
    return safe  # return the cleaned name (may be empty if the input was empty)


def scene_fps(scene):
    """Return the scene frame rate as a float, e.g. 24.0 or 29.97."""
    return scene.render.fps / scene.render.fps_base  # Blender stores fps as fps divided by fps_base


def blender_to_ue_vector(v):
    """Convert a direction or position from Blender axes to Unreal axes (no scaling).

    Blender is right-handed (X right, Y forward, Z up).
    Unreal is left-handed (X forward, Y right, Z up).
    Flipping the Y axis is the usual conversion between the two.
    If the camera ends up mirrored in Unreal later, this is the ONE place to adjust.
    """
    return Vector((v.x, -v.y, v.z))  # keep X and Z, flip Y


def rotator_from_axes(forward, right, up):
    """Build an Unreal rotator (pitch, yaw, roll in degrees) from three direction vectors.

    The vectors must already be in Unreal space. This mirrors how Unreal itself
    extracts a rotator from a rotation matrix whose X axis is forward.
    """
    yaw = math.atan2(forward.y, forward.x)  # yaw is the heading angle around the vertical (Z) axis
    horizontal = math.sqrt(forward.x ** 2 + forward.y ** 2)  # length of the forward vector on the ground plane
    pitch = math.atan2(forward.z, horizontal)  # pitch is how far forward points up (+) or down (-)
    side = Vector((-math.sin(yaw), math.cos(yaw), 0.0))  # the sideways axis of a camera with zero roll
    roll = math.atan2(up.dot(side), right.dot(side))  # roll is how far the real up/right are twisted from that
    return math.degrees(pitch), math.degrees(yaw), math.degrees(roll)  # Unreal wants degrees


# ----------------------------------------------------------------------
# SCENE VALIDATION
# ----------------------------------------------------------------------

def character_images(char_obj):
    """Return every image used by an Image Texture node in the materials of the character's meshes."""
    images = []  # the unique images we find go in this list
    for child in char_obj.children_recursive:  # look at everything parented below the armature
        if child.type != 'MESH':  # only meshes carry materials
            continue  # skip anything else
        for slot in child.material_slots:  # every material slot on the mesh
            material = slot.material  # the material in this slot (can be empty)
            if material is None or not material.use_nodes or material.node_tree is None:  # no node-based material
                continue  # nothing to inspect
            for node in material.node_tree.nodes:  # every node inside the material
                if node.type == 'TEX_IMAGE' and node.image is not None and node.image not in images:  # an Image Texture node with an image
                    images.append(node.image)  # remember it once
    return images  # the list of images the character really uses


def texture_problems(char_obj):
    """Return (level, message) tuples for textures the FBX exporter cannot copy to disk properly."""
    problems = []  # collected problems
    images = character_images(char_obj)  # every image the character uses
    if not images:  # no image textures at all
        problems.append(("WARNING", "Character materials use no image textures"))  # not an error, just worth knowing
        return problems  # nothing more to check
    for image in images:  # check each image on its own
        if image.source == 'GENERATED' or (image.source == 'FILE' and not image.filepath and image.packed_file is None):  # made inside Blender, never saved
            problems.append(("WARNING", "Texture '%s' is not saved to disk" % image.name))  # it cannot be exported
        elif image.packed_file is not None:  # the pixels live inside the .blend file
            continue  # fine, packed textures are written out to disk by the exporter itself
        elif image.source == 'FILE' and not bpy.path.abspath(image.filepath).strip() == "":  # a normal file reference
            if not os.path.isfile(bpy.path.abspath(image.filepath)):  # the file is missing on disk
                problems.append(("WARNING", "Texture '%s' file is missing on disk" % image.name))  # broken link
    return problems  # the list of texture problems


# Which Principled BSDF socket feeds which texture role in Unreal.
# Each role lists the socket names to try (Blender 4.x renamed "Emission" to "Emission Color").
TEXTURE_ROLES = (
    ("base_color", ("Base Color",)),  # the colour / albedo map
    ("metallic", ("Metallic",)),  # the metal mask
    ("roughness", ("Roughness",)),  # the roughness map
    ("normal", ("Normal",)),  # the normal map (comes through a Normal Map node)
    ("emissive", ("Emission Color", "Emission")),  # the glow map
    ("opacity", ("Alpha",)),  # the transparency map
)


def find_socket_image(socket):
    """Follow the link plugged into a shader socket back to an Image Texture node and return its image (or None)."""
    if socket is None or not socket.is_linked:  # nothing is plugged in
        return None  # no texture for this socket
    node = socket.links[0].from_node  # the node feeding this socket
    if node.type == 'TEX_IMAGE':  # a plain Image Texture node
        return node.image  # its image (can be None if the node is empty)
    if node.type == 'NORMAL_MAP':  # a Normal Map node sits between the texture and the shader
        return find_socket_image(node.inputs.get("Color"))  # follow its Color input one step further
    return None  # anything more complex (mix nodes, ramps and so on) is not supported


def copy_image_to_folder(image, textures_dir):
    """Write one image into the textures folder and return its file name, or None if it cannot be written."""
    os.makedirs(textures_dir, exist_ok=True)  # make sure the textures folder exists
    source_path = bpy.path.abspath(image.filepath).replace("\\", "/") if image.filepath else ""  # where the image lives now, forward slashes
    if image.packed_file is not None:  # the pixels are stored inside the .blend file
        name = os.path.basename(source_path) if source_path else ""  # reuse the original file name if it has one
        if not name:  # no usable name
            name = clean_name(image.name) + ".png"  # build one from the image name
        with open(textures_dir + "/" + name, "wb") as handle:  # open the destination for writing bytes
            handle.write(image.packed_file.data)  # write the packed pixels out as a normal file
        return name  # the file name we wrote
    if not source_path or not os.path.isfile(source_path):  # nothing on disk to copy
        return None  # give up on this image
    name = os.path.basename(source_path)  # keep the original file name
    destination = textures_dir + "/" + name  # where the copy goes
    if os.path.abspath(source_path) != os.path.abspath(destination):  # do not copy a file onto itself
        shutil.copy2(source_path, destination)  # copy the image file
    return name  # the file name we wrote


def export_materials(char_obj, folder):
    """Copy the character's textures into <folder>/textures and return a description of each material.

    The result goes into the JSON file so the Unreal side can rebuild the materials itself.
    """
    textures_dir = folder + "/textures"  # all textures go in one subfolder
    result = []  # one dictionary per material that has at least one texture
    seen = []  # materials we have already handled
    for child in char_obj.children_recursive:  # look at everything parented below the armature
        if child.type != 'MESH':  # only meshes carry materials
            continue  # skip anything else
        for slot in child.material_slots:  # every material slot on the mesh
            material = slot.material  # the material in this slot (can be empty)
            if material is None or material in seen:  # empty slot or already done
                continue  # skip it
            seen.append(material)  # remember it
            if not material.use_nodes or material.node_tree is None:  # no node-based material
                continue  # nothing to read
            principled = next((n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)  # the Principled BSDF node
            if principled is None:  # a different kind of shader
                continue  # not supported
            maps = {}  # role -> texture path (relative to the export folder)
            for role, socket_names in TEXTURE_ROLES:  # check each texture role in turn
                for socket_name in socket_names:  # try each possible socket name
                    image = find_socket_image(principled.inputs.get(socket_name))  # the image feeding that socket
                    if image is None:  # nothing found on this socket
                        continue  # try the next name
                    file_name = copy_image_to_folder(image, textures_dir)  # write the image to disk
                    if file_name:  # it worked
                        maps[role] = "textures/" + file_name  # store the relative path
                    break  # we found an image for this role, stop trying names
            if maps:  # only keep materials that have textures
                result.append({"name": material.name, "maps": maps})  # add the material to the manifest
    return result  # the list that gets written into the JSON


def validate_scene(scene):
    """Check the scene and return a list of (level, message) tuples.

    level is "ERROR" (blocks export) or "WARNING" (export still allowed).
    """
    props = scene.ue_seq  # our settings group, registered further down in this file
    problems = []  # we collect every problem in this list
    char = props.character  # the armature object chosen in the panel (or None)
    cam = props.camera  # the camera object chosen in the panel (or None)

    # ---- character checks ----
    if char is None:  # nothing picked yet
        problems.append(("ERROR", "Pick a character armature"))  # tell the user what to do
    elif char.type != 'ARMATURE':  # the object is not a skeleton
        problems.append(("ERROR", "Character must be an armature"))  # block export
    else:  # we have a valid armature, so run deeper checks
        if char.hide_get():  # hidden objects cannot be selected for export
            problems.append(("ERROR", "Character is hidden in the viewport"))  # block export
        if char.animation_data is None or char.animation_data.action is None:  # no keyframes assigned
            problems.append(("ERROR", "Character has no active action (no animation)"))  # block export
        if any(abs(s - 1.0) > 0.0001 for s in char.scale):  # unapplied scale breaks FBX bone sizes
            problems.append(("WARNING", "Armature scale is not 1 (apply scale)"))  # allow, but warn
        meshes = [c for c in char.children_recursive if c.type == 'MESH']  # every mesh parented under the armature
        if not meshes:  # the FBX would only contain bones
            problems.append(("WARNING", "No child meshes, FBX will be skeleton only"))  # allow, but warn
        problems.extend(texture_problems(char))  # warn about textures that cannot be exported

    # ---- camera checks ----
    if cam is None:  # nothing picked yet
        problems.append(("ERROR", "Pick a camera"))  # tell the user what to do
    elif cam.type != 'CAMERA':  # the object is not a camera
        problems.append(("ERROR", "Camera must be a camera object"))  # block export
    else:  # valid camera, run deeper checks
        obj_anim = cam.animation_data is not None and cam.animation_data.action is not None  # moving camera?
        data_anim = cam.data.animation_data is not None and cam.data.animation_data.action is not None  # animated lens?
        if not (obj_anim or data_anim):  # nothing is keyed on the camera
            problems.append(("WARNING", "Camera has no animation (static shot)"))  # not wrong, just worth knowing

    # ---- frame range checks ----
    if scene.frame_end <= scene.frame_start:  # a shot needs at least two frames
        problems.append(("ERROR", "Frame end must be after frame start"))  # block export

    # ---- frame rate check ----
    fps = scene_fps(scene)  # the real frame rate of the scene
    if abs(fps - round(fps)) > 0.001:  # e.g. 29.97 or 23.976
        problems.append(("WARNING", "Frame rate %.3f is not a whole number" % fps))  # Unreal needs care with these

    # ---- unit scale check ----
    if abs(scene.unit_settings.scale_length - 1.0) > 0.0001:  # unit scale other than 1.0
        problems.append(("WARNING", "Unit scale is not 1.0, sizes may mismatch"))  # allow, but warn

    # ---- output settings checks ----
    if clean_name(props.shot_name) == "":  # empty or all-symbol name
        problems.append(("ERROR", "Shot name is empty"))  # block export
    if props.export_dir.strip() == "":  # no folder chosen
        problems.append(("ERROR", "Choose an export folder"))  # block export

    return problems  # hand the full list back to whoever asked


# ----------------------------------------------------------------------
# EXPORT LOGIC
# ----------------------------------------------------------------------

def export_character_fbx(context, char_obj, filepath):
    """Export the armature and its child meshes, with baked animation, to one FBX."""
    view_layer = context.view_layer  # the view layer holds selection and the active object
    previous_selection = list(context.selected_objects)  # remember what the user had selected
    previous_active = view_layer.objects.active  # remember which object was active

    if context.object is not None and context.object.mode != 'OBJECT':  # e.g. user is in Pose or Edit mode
        bpy.ops.object.mode_set(mode='OBJECT')  # operators need Object mode to change selection reliably

    bpy.ops.object.select_all(action='DESELECT')  # clear the selection so only our character gets exported
    char_obj.select_set(True)  # select the armature
    for child in char_obj.children_recursive:  # walk through everything parented below the armature
        if child.type == 'MESH':  # only meshes (skip empties, lights and so on)
            child.select_set(True)  # add the mesh to the selection
    view_layer.objects.active = char_obj  # the FBX exporter wants an active object

    try:  # try/finally makes sure we ALWAYS restore the selection, even if the export fails
        bpy.ops.export_scene.fbx(  # call Blender's built-in FBX exporter
            filepath=filepath,  # where to write the file (forward slashes)
            use_selection=True,  # export only the selected objects
            object_types={'ARMATURE', 'MESH'},  # skeleton and meshes only, no cameras or lights here
            use_mesh_modifiers=True,  # apply modifiers (e.g. Subdivision, Mirror) in the export
            path_mode='STRIP',  # write only texture file names in the FBX, because textures are exported separately by export_materials()
            embed_textures=False,  # do not embed textures in the FBX
            mesh_smooth_type='FACE',  # export face smoothing, which Unreal reads without warnings
            add_leaf_bones=False,  # do not add extra end bones that Unreal would import as junk
            primary_bone_axis='Y',  # bone direction axis (Blender default)
            secondary_bone_axis='X',  # bone secondary axis (Blender default)
            apply_scale_options='FBX_SCALE_ALL',  # bake Blender's metres into FBX scale so Unreal sees centimetres
            global_scale=1.0,  # no extra scaling on top
            axis_forward='-Z',  # forward axis written into the FBX
            axis_up='Y',  # up axis written into the FBX
            bake_anim=True,  # include animation in the file
            bake_anim_use_all_bones=True,  # key every bone each frame, so nothing is lost
            bake_anim_use_nla_strips=False,  # use the active action, not separate NLA strip takes
            bake_anim_use_all_actions=False,  # export only the active action, not every action in the file
            bake_anim_force_startend_keying=True,  # make sure the first and last frames are keyed
            bake_anim_step=1.0,  # sample once per frame
            bake_anim_simplify_factor=0.0,  # no key reduction, keep full quality
        )
    finally:  # this block runs whether the export worked or crashed
        bpy.ops.object.select_all(action='DESELECT')  # clear our temporary selection
        for obj in previous_selection:  # go back through the user's original selection
            try:  # an object could have been deleted meanwhile
                obj.select_set(True)  # re-select it
            except ReferenceError:  # the object no longer exists
                pass  # simply skip it
        view_layer.objects.active = previous_active  # restore the active object

    try:  # check that the finished FBX really contains an animation
        with open(filepath, "rb") as handle:  # open the FBX as raw bytes
            return b"AnimationStack" in handle.read()  # animated FBX files contain an AnimationStack entry
    except OSError:  # the file could not be read back
        return True  # assume it is fine rather than raising a false alarm


def sample_camera(scene, cam_obj):
    """Step through every frame and record the camera in Unreal space and units."""
    frames = []  # one dictionary per frame goes into this list
    original_frame = scene.frame_current  # remember the frame the user was on
    scale = scene.unit_settings.scale_length * METRES_TO_CM  # Blender units -> Unreal centimetres
    cam_data = cam_obj.data  # the camera data block (lens, sensor, depth of field)

    for frame in range(scene.frame_start, scene.frame_end + 1):  # inclusive of the last frame
        scene.frame_set(frame)  # jump to this frame so every animation is evaluated
        world = cam_obj.matrix_world.copy()  # the camera's final world transform at this frame (includes parenting)
        rot = world.to_3x3().normalized()  # just the rotation part, with scale removed

        forward_b = rot @ Vector((0.0, 0.0, -1.0))  # a Blender camera looks down its local -Z axis
        right_b = rot @ Vector((1.0, 0.0, 0.0))  # its local +X axis points to the camera's right
        up_b = rot @ Vector((0.0, 1.0, 0.0))  # its local +Y axis points to the camera's top

        forward_ue = blender_to_ue_vector(forward_b)  # convert each direction into Unreal axes
        right_ue = blender_to_ue_vector(right_b)  # (same Y flip as positions)
        up_ue = blender_to_ue_vector(up_b)  # (this keeps all three consistent)

        pitch, yaw, roll = rotator_from_axes(forward_ue, right_ue, up_ue)  # turn the three axes into an Unreal rotator
        location_ue = blender_to_ue_vector(world.to_translation()) * scale  # position in Unreal space and centimetres

        focus_object = cam_data.dof.focus_object  # an object the camera is set to focus on (or None)
        if focus_object is not None:  # Blender then ignores the typed focus distance and uses this object
            target = focus_object.matrix_world.translation.copy()  # the object's origin in world space
            subtarget = cam_data.dof.focus_subtarget  # a bone name when the focus object is an armature
            if focus_object.type == 'ARMATURE' and subtarget in focus_object.pose.bones:  # focusing on one bone
                target = focus_object.matrix_world @ focus_object.pose.bones[subtarget].head  # that bone's head in world space
            depth = abs((target - world.to_translation()).dot(forward_b))  # distance measured along the camera's viewing direction
            focus_cm = depth * scale  # converted to Unreal centimetres
        else:  # no focus object, so the typed focus distance is used
            focus_cm = cam_data.dof.focus_distance * scale  # focus distance in cm

        frames.append({  # build this frame's record
            "frame": frame,  # the Blender frame number
            "location_cm": [location_ue.x, location_ue.y, location_ue.z],  # Unreal position
            "rotation_deg": {"pitch": pitch, "yaw": yaw, "roll": roll},  # Unreal rotation
            "focal_length_mm": cam_data.lens,  # lens focal length at this frame (can be animated)
            "focus_distance_cm": focus_cm,  # focus distance at this frame
            "f_stop": cam_data.dof.aperture_fstop,  # aperture at this frame
            "blender_matrix_world": [v for row in world for v in row],  # raw Blender matrix, kept as a backup for debugging
        })

    scene.frame_set(original_frame)  # put the timeline back where the user left it
    return frames  # the complete per-frame camera track


def write_camera_json(scene, props, fbx_name, json_path, materials):
    """Write the camera track and shot settings to a JSON file."""
    cam_obj = props.camera  # the chosen camera object
    cam_data = cam_obj.data  # its camera data block
    shot = {  # top-level dictionary that becomes the JSON file
        "format_version": 2,  # lets Stage 2 know which layout to expect (2 adds the materials list)
        "materials": materials,  # each material's texture files, so Unreal can rebuild them
        "shot_name": clean_name(props.shot_name),  # the shot's name
        "character_fbx": fbx_name,  # file name of the FBX exported next to this JSON
        "fps": scene_fps(scene),  # frame rate for the Unreal sequence
        "frame_start": scene.frame_start,  # first frame of the shot
        "frame_end": scene.frame_end,  # last frame of the shot (inclusive)
        "resolution_x": scene.render.resolution_x,  # render width, used for aspect ratio
        "resolution_y": scene.render.resolution_y,  # render height, used for aspect ratio
        "camera_name": cam_obj.name,  # name of the Blender camera
        "sensor_width_mm": cam_data.sensor_width,  # Blender's sensor width
        "sensor_height_mm": cam_data.sensor_height,  # Blender's sensor height
        "sensor_fit": cam_data.sensor_fit,  # AUTO, HORIZONTAL or VERTICAL (matters for matching framing in Stage 3)
        "use_dof": cam_data.dof.use_dof,  # whether depth of field is switched on
        "frames": sample_camera(scene, cam_obj),  # the per-frame camera track
    }
    with open(json_path, "w") as handle:  # open the file for writing (the with block closes it for us)
        json.dump(shot, handle, indent=2)  # write the dictionary as readable, indented JSON


# ----------------------------------------------------------------------
# SETTINGS STORED ON THE SCENE
# ----------------------------------------------------------------------

def poll_armature(self, obj):
    """Only let armature objects appear in the character picker."""
    return obj.type == 'ARMATURE'  # True means "show this object in the dropdown"


def poll_camera(self, obj):
    """Only let camera objects appear in the camera picker."""
    return obj.type == 'CAMERA'  # True means "show this object in the dropdown"


class UESEQ_Props(bpy.types.PropertyGroup):
    """The settings the user fills in on the panel. Saved inside the .blend file."""

    character: bpy.props.PointerProperty(  # a slot that points at an object in the scene
        name="Character",  # label shown in the panel
        description="Armature to export (its child meshes come with it)",  # tooltip
        type=bpy.types.Object,  # the slot holds an Object
        poll=poll_armature,  # filter the dropdown to armatures
    )
    camera: bpy.props.PointerProperty(  # a second object slot for the shot camera
        name="Camera",  # label shown in the panel
        description="Camera to export",  # tooltip
        type=bpy.types.Object,  # the slot holds an Object
        poll=poll_camera,  # filter the dropdown to cameras
    )
    shot_name: bpy.props.StringProperty(  # a text field
        name="Shot Name",  # label shown in the panel
        description="Used for the exported file names",  # tooltip
        default="Shot_01",  # starting value
    )
    export_dir: bpy.props.StringProperty(  # a text field with a folder browser button
        name="Export Folder",  # label shown in the panel
        description="Folder that receives the FBX and camera JSON",  # tooltip
        subtype='DIR_PATH',  # tells Blender to show a folder picker
        default="//ue_export",  # a folder next to the .blend file
    )


# ----------------------------------------------------------------------
# OPERATOR (the thing the button runs)
# ----------------------------------------------------------------------

class UESEQ_OT_export_shot(bpy.types.Operator):
    """Export the character FBX and camera JSON for Unreal Sequencer"""

    bl_idname = "ue_seq.export_shot"  # internal id used by buttons and scripts
    bl_label = "Export to UE Sequencer"  # text shown on the button

    def execute(self, context):
        """Runs when the user clicks the button."""
        scene = context.scene  # the current scene
        props = scene.ue_seq  # our settings group

        errors = [msg for level, msg in validate_scene(scene) if level == "ERROR"]  # collect only the blocking problems
        if errors:  # at least one blocking problem exists
            self.report({'ERROR'}, errors[0])  # show the first one in Blender's status bar
            return {'CANCELLED'}  # stop without exporting

        folder = clean_path(props.export_dir)  # absolute folder path with forward slashes
        name = clean_name(props.shot_name)  # safe file name stem
        fbx_name = name + ".fbx"  # e.g. Shot_01.fbx
        fbx_path = folder + "/" + fbx_name  # full path to the FBX
        json_path = folder + "/" + name + "_camera.json"  # full path to the camera JSON

        try:  # catch any unexpected failure and report it nicely instead of a traceback
            import os  # standard library module for folders
            os.makedirs(folder, exist_ok=True)  # create the export folder if it does not exist yet
            has_animation = export_character_fbx(context, props.character, fbx_path)  # write the character and animation
            materials = export_materials(props.character, folder)  # copy the textures and describe the materials
            write_camera_json(scene, props, fbx_name, json_path, materials)  # write the camera track and materials
        except Exception as error:  # something went wrong
            self.report({'ERROR'}, "Export failed: %s" % error)  # tell the user what happened
            return {'CANCELLED'}  # stop here

        if not has_animation:  # the FBX came out without any animation
            self.report({'WARNING'}, "Exported, but the FBX contains NO animation. Check the character's active action.")  # warn loudly
            return {'FINISHED'}  # the files exist, so still finish
        self.report({'INFO'}, "Exported %s to %s" % (name, folder))  # success message
        return {'FINISHED'}  # tell Blender the operator completed


# ----------------------------------------------------------------------
# PANEL (the UI in the sidebar)
# ----------------------------------------------------------------------

class UESEQ_PT_panel(bpy.types.Panel):
    """The sidebar panel."""

    bl_label = "UE Sequencer Exporter"  # panel title
    bl_idname = "UESEQ_PT_panel"  # internal id of the panel
    bl_space_type = 'VIEW_3D'  # lives in the 3D Viewport
    bl_region_type = 'UI'  # in the sidebar region (the N panel)
    bl_category = "UE Sequencer"  # the sidebar tab name

    def draw(self, context):
        """Blender calls this every time the panel needs to be redrawn."""
        layout = self.layout  # the object we add UI elements to
        scene = context.scene  # the current scene
        props = scene.ue_seq  # our settings group

        layout.prop(props, "character")  # the character picker
        layout.prop(props, "camera")  # the camera picker
        layout.prop(props, "shot_name")  # the shot name text box
        layout.prop(props, "export_dir")  # the export folder field

        problems = validate_scene(scene)  # run the checks so we can show them live
        box = layout.box()  # a framed area for the check results
        if problems:  # at least one issue found
            for level, message in problems:  # show each issue on its own row
                icon = 'ERROR' if level == "ERROR" else 'INFO'  # red icon for errors, info icon for warnings
                box.label(text=message, icon=icon)  # draw the message with its icon
        else:  # nothing wrong
            box.label(text="Ready to export", icon='CHECKMARK')  # reassuring all-clear

        has_errors = any(level == "ERROR" for level, _ in problems)  # True if export is blocked
        row = layout.row()  # a row we can switch off
        row.scale_y = 1.6  # make the button taller so it stands out
        row.enabled = not has_errors  # grey the button out while errors exist
        row.operator("ue_seq.export_shot", icon='EXPORT')  # the export button


# ----------------------------------------------------------------------
# REGISTRATION (tells Blender about our classes)
# ----------------------------------------------------------------------

classes = (UESEQ_Props, UESEQ_OT_export_shot, UESEQ_PT_panel)  # order matters: settings first, UI last


def register():
    """Called when the addon is enabled."""
    for cls in classes:  # go through each class
        bpy.utils.register_class(cls)  # register it with Blender
    bpy.types.Scene.ue_seq = bpy.props.PointerProperty(type=UESEQ_Props)  # attach our settings to every scene


def unregister():
    """Called when the addon is disabled."""
    del bpy.types.Scene.ue_seq  # remove the settings from the scene type
    for cls in reversed(classes):  # unregister in reverse order
        bpy.utils.unregister_class(cls)  # remove it from Blender


# This lets you run the file straight from Blender's Text Editor to test it
if __name__ == "__main__":  # only true when run as a script, not when installed as an addon
    register()  # register everything