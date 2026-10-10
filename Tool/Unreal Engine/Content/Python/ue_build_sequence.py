# ======================================================================
# UE Sequencer Builder - STAGE 2 (Unreal side, Unreal Engine 5 Python)
# ----------------------------------------------------------------------
# What this does:
#   1. Reads the <ShotName>_camera.json that the Blender exporter wrote
#   2. Imports the <ShotName>.fbx next to it (skeletal mesh + animation)
#   3. Creates a Level Sequence at the right frame rate and length
#   4. Adds the character with its animation track
#   5. Adds a cine camera with per-frame transform and focal length keys
#   6. Adds a camera cut so Sequencer looks through that camera
#
# How to use it:
#   - Normally the "Import Sequencer" button (ue_sequencer_ui.py) calls build_sequence()
#   - You can still run this file directly: edit JSON_PATH below, then use
#     Tools > Execute Python Script... and pick this file
# ======================================================================

import unreal  # Unreal's Python module, only available inside the Unreal Editor
import json  # standard library module used to read the camera JSON file
import os  # standard library module for checking whether a path is a file or a folder
import glob  # standard library module for finding files that match a pattern

# ----------------------------------------------------------------------
# SETTINGS - EDIT THESE TWO LINES
# ----------------------------------------------------------------------

# Path to the Blender export. This can be EITHER:
#   - the export folder (the newest *_camera.json inside it is used), or
#   - the full path to one specific *_camera.json file
# Use forward slashes ONLY. A single backslash such as \r in "Users\risha" is read
# by Python as a carriage return character and corrupts the path.
JSON_PATH = "C:/Users/risha/Documents/Blender Animation/Tech Art/Animation to Sequencer/ue_export"

# Content Browser folder where everything gets created (a subfolder per shot is added)
CONTENT_FOLDER = "/Game/Shots"


# ----------------------------------------------------------------------
# HELPER FUNCTIONS
# ----------------------------------------------------------------------

def unwrap_angle(previous, current):
    """Keep an angle close to the previous one so it never spins the long way round.

    Example: going from 179 to -179 degrees is really a 2 degree turn, not 358.
    """
    while current - previous > 180.0:  # the new angle jumped far above the old one
        current -= 360.0  # pull it back down by a full turn
    while current - previous < -180.0:  # the new angle jumped far below the old one
        current += 360.0  # push it back up by a full turn
    return current  # the adjusted angle


def get_section_channels(section):
    """Return a section's keyable channels, whatever this Unreal version calls the function."""
    for method_name in ("get_all_channels", "get_channels"):  # newer Unreal uses the first name, older versions the second
        method = getattr(section, method_name, None)  # look the method up, or get None if it does not exist
        if method is not None:  # this Unreal version has that method
            return list(method())  # call it and return the channels as a normal Python list
    names = [n for n in dir(section) if "channel" in n.lower()]  # collect every channel-related name on the section
    raise RuntimeError("No channel getter found on section. Channel-related names: " + ", ".join(names))  # show them so we can fix it


def remove_old_actors(actor_subsystem, labels):
    """Delete level actors left over from a previous run, found by their labels."""
    for actor in actor_subsystem.get_all_level_actors():  # look at every actor in the current level
        if actor.get_actor_label() in labels:  # does its label match one we created before?
            actor_subsystem.destroy_actor(actor)  # remove it so reruns do not stack duplicates


def clean_shot_folder(shot_folder):
    """Delete everything from a previous run of this shot, so the FBX is always imported fresh.

    Re-importing an FBX over existing assets only reports the mesh and skips the animation,
    which is why a second import of the same shot used to fail.
    """
    if not unreal.EditorAssetLibrary.does_directory_exist(shot_folder):  # first run for this shot, nothing to clean
        return  # done
    asset_editor = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)  # lets us close open asset windows
    for path in unreal.EditorAssetLibrary.list_assets(shot_folder, recursive=True, include_folder=False):  # every asset in the shot folder
        asset = unreal.EditorAssetLibrary.load_asset(path)  # load it so we can close any window showing it
        if asset is not None:  # the asset loaded
            asset_editor.close_all_editors_for_asset(asset)  # close its window, otherwise the delete can fail
    if unreal.EditorAssetLibrary.delete_directory(shot_folder):  # delete the whole folder and everything in it
        unreal.log("Cleaned the old files in " + shot_folder)  # confirmation in the Output Log
    else:  # something is still using an asset
        unreal.log_warning("Could not fully clean " + shot_folder + ". Old files may be reused.")  # explain and carry on


def import_character(fbx_path, dest_folder):
    """Import the FBX as a skeletal mesh with animation and return (mesh, animation)."""
    task = unreal.AssetImportTask()  # one import job
    task.set_editor_property("filename", fbx_path)  # the FBX file to import
    task.set_editor_property("destination_path", dest_folder)  # the Content Browser folder to import into
    task.set_editor_property("automated", True)  # skip the import dialog window
    task.set_editor_property("replace_existing", True)  # overwrite assets from a previous import
    task.set_editor_property("save", True)  # save the imported assets to disk

    options = unreal.FbxImportUI()  # the FBX import settings object
    options.set_editor_property("import_mesh", True)  # import the mesh
    options.set_editor_property("import_as_skeletal", True)  # treat it as a skeletal mesh, not a static mesh
    options.set_editor_property("import_animations", True)  # also import the animation stored in the file
    options.set_editor_property("import_materials", True)  # create materials from the FBX
    options.set_editor_property("import_textures", False)  # textures are imported separately by import_textures_and_build_materials()
    options.set_editor_property("automated_import_should_detect_type", False)  # do not guess, use our settings
    options.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_SKELETAL_MESH)  # skeletal mesh import mode
    task.set_editor_property("options", options)  # attach the settings to the job

    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])  # run the import

    skeletal_mesh = None  # will hold the imported skeletal mesh
    animation = None  # will hold the imported animation sequence
    found = []  # a readable list of everything the import reported, for the error message
    imported_paths = list(task.get_editor_property("imported_object_paths"))  # every asset the import reports it created
    unreal.log("FBX import reported " + str(len(imported_paths)) + " asset(s): " + ", ".join(str(p) for p in imported_paths))  # show it in the Output Log
    for path in imported_paths:  # check each reported asset
        asset = unreal.EditorAssetLibrary.load_asset(path)  # load the asset so we can check its type
        found.append(str(path) + " (" + (type(asset).__name__ if asset is not None else "could not load") + ")")  # note what it is
        if isinstance(asset, unreal.SkeletalMesh):  # is this the character mesh?
            skeletal_mesh = asset  # remember it
        elif isinstance(asset, unreal.AnimSequence):  # is this the animation?
            animation = asset  # remember it

    if animation is None:  # the import did not report an animation, but one may already exist in the folder
        for path in unreal.EditorAssetLibrary.list_assets(dest_folder, recursive=False, include_folder=False):  # every asset in the shot folder
            asset = unreal.EditorAssetLibrary.load_asset(path)  # load it so we can check its type
            if isinstance(asset, unreal.AnimSequence):  # an animation already in the folder
                animation = asset  # use it
                unreal.log_warning("The import did not report an animation, using the existing one: " + str(path))  # say so, because it could be out of date
                break  # one is enough
    if skeletal_mesh is None:  # the mesh is missing
        raise RuntimeError("The FBX import did not create a skeletal mesh. Import reported: " + "; ".join(found))  # stop with the details
    if animation is None:  # the mesh is there but no animation came with it
        raise RuntimeError("The FBX import created the mesh but NO animation, so the FBX probably contains no animation. "
                           "Check in Blender that the character has an active action and re-export. Import reported: " + "; ".join(found))  # stop with the details
    return skeletal_mesh, animation  # hand both assets back


def import_texture_file(file_path, dest_folder):
    """Import one image file as a texture asset and return the Texture2D, or None if the import failed."""
    task = unreal.AssetImportTask()  # one import job
    task.set_editor_property("filename", file_path)  # the image file to import
    task.set_editor_property("destination_path", dest_folder)  # the Content Browser folder to import into
    task.set_editor_property("automated", True)  # skip the import dialog window
    task.set_editor_property("replace_existing", True)  # overwrite a texture from a previous run
    task.set_editor_property("save", True)  # save the imported texture to disk
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])  # run the import
    for path in task.get_editor_property("imported_object_paths"):  # every asset this import created
        asset = unreal.EditorAssetLibrary.load_asset(path)  # load it so we can check its type
        if isinstance(asset, unreal.Texture2D):  # is it a texture?
            return asset  # this is the one we want
    return None  # nothing usable was imported


def configure_texture(texture, role):
    """Set colour space and compression so each texture type looks right in Unreal."""
    try:  # texture settings differ a little between versions, so never let this stop the build
        if role == "normal":  # a normal map
            texture.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_NORMALMAP)  # normal map compression
            texture.set_editor_property("srgb", False)  # normal maps are not colour data
        elif role in ("metallic", "roughness", "opacity"):  # grayscale data maps
            texture.set_editor_property("compression_settings", unreal.TextureCompressionSettings.TC_MASKS)  # mask compression
            texture.set_editor_property("srgb", False)  # linear data, not colour
        unreal.EditorAssetLibrary.save_loaded_asset(texture)  # save the changed settings
    except Exception as error:  # a property was not found
        unreal.log_warning("Could not set texture settings for " + str(role) + ": " + str(error))  # explain and carry on


def build_material(name, texture_by_role, dest_folder):
    """Create an Unreal Material from textures: a texture sample per role, wired into the right input."""
    library = unreal.MaterialEditingLibrary  # helper functions for building materials in Python
    path = dest_folder + "/" + name  # full Content Browser path of the new material
    if unreal.EditorAssetLibrary.does_asset_exist(path):  # a material from a previous run is there
        unreal.EditorAssetLibrary.delete_asset(path)  # remove it so we rebuild cleanly

    material = unreal.AssetToolsHelpers.get_asset_tools().create_asset(  # create the empty material
        asset_name=name,  # the asset's name
        package_path=dest_folder,  # the folder it goes in
        asset_class=unreal.Material,  # the kind of asset
        factory=unreal.MaterialFactoryNew(),  # the factory that makes materials
    )

    # role -> (material input, which output pin of the texture sample to use)
    wiring = {
        "base_color": (unreal.MaterialProperty.MP_BASE_COLOR, "RGB"),  # colour uses all three colour channels
        "metallic": (unreal.MaterialProperty.MP_METALLIC, "R"),  # grayscale, read the red channel
        "roughness": (unreal.MaterialProperty.MP_ROUGHNESS, "R"),  # grayscale, read the red channel
        "normal": (unreal.MaterialProperty.MP_NORMAL, "RGB"),  # normal maps use all three channels
        "emissive": (unreal.MaterialProperty.MP_EMISSIVE_COLOR, "RGB"),  # glow colour
    }

    row = 0  # vertical position counter so the nodes do not overlap in the material editor
    for role, texture in texture_by_role.items():  # one texture sample node per texture
        if role not in wiring:  # opacity is not wired in this version
            continue  # skip it
        material_input, pin = wiring[role]  # where this texture plugs in
        node = library.create_material_expression(material, unreal.MaterialExpressionTextureSample, -400, row * 300)  # add a texture sample node
        node.set_editor_property("texture", texture)  # point the node at the texture
        if role == "normal":  # normal maps need the matching sampler type
            try:  # property values can differ between versions
                node.set_editor_property("sampler_type", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL)  # treat the texture as a normal map
            except Exception:  # not available
                pass  # the texture's own compression setting usually covers this
        library.connect_material_property(node, pin, material_input)  # plug the node into the material
        row += 1  # move the next node down

    library.recompile_material(material)  # compile the shader so the result shows up
    unreal.EditorAssetLibrary.save_loaded_asset(material)  # save the material
    return material  # hand the finished material back


def assign_material_to_mesh(skeletal_mesh, blender_name, material):
    """Replace the mesh's material slot that matches the Blender material name. Returns True on success."""
    slots = list(skeletal_mesh.get_editor_property("materials"))  # the mesh's material slots (each is a SkeletalMaterial)
    target = None  # index of the slot we will replace
    for index, slot in enumerate(slots):  # check every slot
        slot_name = str(slot.get_editor_property("material_slot_name"))  # the slot's name
        imported_name = str(slot.get_editor_property("imported_material_slot_name"))  # the name it had in the FBX
        if blender_name in (slot_name, imported_name):  # exact match with the Blender material
            target = index  # remember it
            break  # stop looking
    if target is None and len(slots) == 1:  # a single-slot mesh: there is only one place it can go
        target = 0  # use that slot
    if target is None:  # still no match
        return False  # tell the caller we could not place it
    slots[target].set_editor_property("material_interface", material)  # put our new material in the slot
    skeletal_mesh.set_editor_property("materials", slots)  # write the slot list back to the mesh
    unreal.EditorAssetLibrary.save_loaded_asset(skeletal_mesh)  # save the mesh
    return True  # it worked


def import_textures_and_build_materials(data, export_folder, skeletal_mesh, shot_folder):
    """Import the exported textures, build a material for each Blender material and assign it to the mesh."""
    materials = data.get("materials", [])  # the list written by the Blender exporter (missing in older exports)
    if not materials:  # nothing to do
        unreal.log_warning("No materials in the JSON. Re-export from the updated Blender add-on to get textures.")  # explain why
        return  # keep the default materials

    texture_folder = shot_folder + "/Textures"  # where textures are imported
    material_folder = shot_folder + "/Materials"  # where new materials are created
    for entry in materials:  # one Blender material at a time
        texture_by_role = {}  # role -> imported texture asset
        for role, relative_path in entry["maps"].items():  # every texture this material uses
            file_path = export_folder + "/" + relative_path  # the image file next to the JSON
            if not os.path.isfile(file_path):  # the file is missing
                unreal.log_warning("Texture file not found: " + file_path)  # tell the user
                continue  # skip this map
            texture = import_texture_file(file_path, texture_folder)  # import it as a texture asset
            if texture is None:  # the import failed
                unreal.log_warning("Texture import failed: " + file_path)  # tell the user
                continue  # skip this map
            configure_texture(texture, role)  # set colour space and compression
            texture_by_role[role] = texture  # remember it
        if not texture_by_role:  # nothing usable for this material
            continue  # leave the default material alone

        material_name = "M_" + entry["name"].replace(" ", "_").replace(".", "_")  # a clean asset name, e.g. M_Ch03_Body
        material = build_material(material_name, texture_by_role, material_folder)  # create the material
        if assign_material_to_mesh(skeletal_mesh, entry["name"], material):  # put it on the mesh
            unreal.log("Assigned " + material_name + " to material slot " + entry["name"])  # success message
        else:  # no matching slot found
            unreal.log_warning("No mesh slot matched Blender material '" + entry["name"] + "'. The material " + material_name + " was created but not assigned.")  # explain


def add_transform_keys(binding, frames, frame_count):
    """Add a 3D transform track to a binding and key location and rotation every frame."""
    track = binding.add_track(unreal.MovieScene3DTransformTrack)  # the transform track
    section = track.add_section()  # one section that spans the shot
    section.set_range(0, frame_count)  # the section covers frame 0 up to the end of the shot
    channels = get_section_channels(section)  # nine channels: loc X,Y,Z then rot X(roll),Y(pitch),Z(yaw) then scale X,Y,Z
    if len(channels) < 9:  # the order below only works if all nine channels exist
        raise RuntimeError("Expected 9 transform channels but found " + str(len(channels)))  # stop with a clear message

    for scale_channel in channels[6:9]:  # the three scale channels
        scale_channel.add_key(unreal.FrameNumber(0), 1.0)  # keep scale at 1 for the whole shot

    previous_yaw = None  # last yaw we wrote, used for unwrapping
    previous_roll = None  # last roll we wrote, used for unwrapping

    for index, frame in enumerate(frames):  # index 0 is the first frame of the shot
        loc = frame["location_cm"]  # [x, y, z] in Unreal centimetres
        rot = frame["rotation_deg"]  # pitch, yaw, roll in degrees
        pitch = rot["pitch"]  # pitch needs no unwrapping because it stays between -90 and 90
        yaw = rot["yaw"] if previous_yaw is None else unwrap_angle(previous_yaw, rot["yaw"])  # unwrap yaw
        roll = rot["roll"] if previous_roll is None else unwrap_angle(previous_roll, rot["roll"])  # unwrap roll
        previous_yaw = yaw  # remember for the next frame
        previous_roll = roll  # remember for the next frame

        time = unreal.FrameNumber(index)  # the sequence frame this key lives on
        linear = unreal.MovieSceneKeyInterpolation.LINEAR  # straight lines between per-frame samples, no overshoot
        channels[0].add_key(time, loc[0], interpolation=linear)  # location X
        channels[1].add_key(time, loc[1], interpolation=linear)  # location Y
        channels[2].add_key(time, loc[2], interpolation=linear)  # location Z
        channels[3].add_key(time, roll, interpolation=linear)  # rotation X is roll in Unreal
        channels[4].add_key(time, pitch, interpolation=linear)  # rotation Y is pitch in Unreal
        channels[5].add_key(time, yaw, interpolation=linear)  # rotation Z is yaw in Unreal


def pick_enum(enum_class, *names):
    """Return the first enum member that exists under any of the given names (names differ between versions)."""
    for name in names:  # try each candidate name in order
        if hasattr(enum_class, name):  # this version has that member
            return getattr(enum_class, name)  # use it
    raise AttributeError("None of these names exist: " + ", ".join(names))  # nothing matched


def apply_camera_optics(camera_binding, data, frames):
    """Apply the Blender camera's optics to the camera stored inside the sequence (used while building)."""
    template = camera_binding.get_object_template()  # the camera actor stored inside the sequence
    component = template.get_cine_camera_component()  # its camera component holds the lens settings
    configure_camera_component(component, data, frames)  # do the real work


# Aperture used for depth of field when Blender's camera had depth of field switched OFF but we still want
# the character kept in focus with a gentle blur. Lower numbers give a stronger blur.
TRACKING_APERTURE = 5.6


def configure_camera_component(component, data, frames, focus_target=None):
    """Match a Unreal camera component's lens, sensor size and depth of field to the Blender camera.

    component: the CineCameraComponent to change (a template inside the sequence, or a live camera).
    focus_target: an actor to keep in focus using the Tracking focus method, or None for no tracking.

    Without this, Unreal keeps its default film back and a wide-open aperture focused very far away,
    which makes everything near the camera look blurry and changes the field of view.
    """
    # ---- focal length ----
    component.set_editor_property("current_focal_length", frames[0]["focal_length_mm"])  # the Blender lens in mm (first frame)

    # ---- sensor size (film back) ----
    res_x = float(data["resolution_x"])  # render width in pixels
    res_y = float(data["resolution_y"])  # render height in pixels
    blender_fit = data.get("sensor_fit", "AUTO")  # how Blender fits the sensor to the image
    if blender_fit == "AUTO":  # Blender applies the sensor width to the LARGER side of the image
        horizontal = res_x >= res_y  # landscape images are fitted horizontally
        fit_size = data["sensor_width_mm"]  # the size that applies to the fitted side
    elif blender_fit == "HORIZONTAL":  # fitted to the width
        horizontal = True  # horizontal fit
        fit_size = data["sensor_width_mm"]  # the sensor width
    else:  # VERTICAL, fitted to the height
        horizontal = False  # vertical fit
        fit_size = data["sensor_height_mm"]  # the sensor height
    if horizontal:  # the width is known, work out the height from the image shape
        sensor_width = fit_size  # width in mm
        sensor_height = fit_size * res_y / res_x  # height in mm
    else:  # the height is known, work out the width from the image shape
        sensor_height = fit_size  # height in mm
        sensor_width = fit_size * res_x / res_y  # width in mm
    try:  # the film back is a struct, so read it, edit the copy and write it back
        filmback = component.get_editor_property("filmback")  # the current film back settings
        filmback.set_editor_property("sensor_width", sensor_width)  # sensor width in mm
        filmback.set_editor_property("sensor_height", sensor_height)  # sensor height in mm
        component.set_editor_property("filmback", filmback)  # store the edited settings back
        unreal.log("Camera sensor set to " + str(round(sensor_width, 2)) + " x " + str(round(sensor_height, 2)) + " mm")  # report it
    except Exception as error:  # the property was not found
        unreal.log_warning("Could not set the camera sensor size: " + str(error))  # explain and carry on

    # ---- depth of field ----
    try:  # focus settings are a struct as well
        focus = component.get_editor_property("focus_settings")  # the current focus settings
        if focus_target is not None:  # keep this actor in focus using the Tracking focus method
            focus.set_editor_property("focus_method", pick_enum(unreal.CameraFocusMethod, "TRACKING"))  # follow an actor
            tracking = focus.get_editor_property("tracking_focus_settings")  # the tracking options (a struct)
            tracking.set_editor_property("actor_to_track", focus_target)  # the actor to keep in focus
            _origin, extent = focus_target.get_actor_bounds(False)  # the actor's bounding box centre and half-size
            height = extent.z * 2.0  # the character's approximate height in cm
            aim_height = (_origin.z - extent.z) + height * 0.8  # a point about 80 percent up the body (chest and face area)
            offset_z = aim_height - focus_target.get_actor_location().z  # that point relative to the actor's origin (its feet)
            tracking.set_editor_property("relative_offset", unreal.Vector(0.0, 0.0, offset_z))  # aim the focus there
            focus.set_editor_property("tracking_focus_settings", tracking)  # store the edited tracking settings back
            aperture = frames[0]["f_stop"] if data.get("use_dof", False) else TRACKING_APERTURE  # Blender's f-stop if it had one
            component.set_editor_property("current_aperture", aperture)  # set the aperture
            unreal.log("Focus tracking ON: following " + focus_target.get_actor_label() + ", aimed " + str(round(offset_z, 1)) + " cm above its origin, f/" + str(aperture))  # report it
        elif data.get("use_dof", False):  # the Blender camera has depth of field switched on
            focus.set_editor_property("focus_method", pick_enum(unreal.CameraFocusMethod, "MANUAL"))  # focus at a typed distance
            focus.set_editor_property("manual_focus_distance", frames[0]["focus_distance_cm"])  # the Blender focus distance in cm
            component.set_editor_property("current_aperture", frames[0]["f_stop"])  # the Blender f-stop
            unreal.log("Depth of field ON: focus " + str(round(frames[0]["focus_distance_cm"], 1)) + " cm, f/" + str(frames[0]["f_stop"]) + " (taken from the first frame)")  # report it
        else:  # Blender renders this shot sharp, so Unreal should too
            focus.set_editor_property("focus_method", pick_enum(unreal.CameraFocusMethod, "NONE", "DISABLE"))  # no focus method means no depth of field blur
            component.set_editor_property("current_aperture", 22.0)  # a very small aperture also keeps everything sharp
            unreal.log("Depth of field OFF (the Blender camera has it disabled)")  # report it
        component.set_editor_property("focus_settings", focus)  # store the edited settings back
    except Exception as error:  # a property or enum name was not found
        unreal.log_warning("Could not set depth of field: " + str(error))  # explain and carry on


def add_focal_length_keys(sequence, camera_binding, frames, frame_count):
    """Set the camera's lens. A fixed lens is stored directly; a zoom is keyed every frame."""
    lens_values = [frame["focal_length_mm"] for frame in frames]  # the focal length at every frame
    first_lens = lens_values[0]  # the lens on the first frame
    is_static = (max(lens_values) - min(lens_values)) < 0.001  # True when the lens never changes during the shot

    template = camera_binding.get_object_template()  # the camera actor stored INSIDE the sequence (the spawnable's blueprint)
    component = template.get_cine_camera_component()  # its camera component holds the lens settings
    component.set_editor_property("current_focal_length", first_lens)  # always store the first-frame lens, so a fixed lens works without a track
    applied = component.get_editor_property("current_focal_length")  # read it back, because Unreal clamps it to the lens limits
    unreal.log("Focal length from Blender: " + str(first_lens) + " mm, now set on the Unreal camera: " + str(applied) + " mm")  # prove it was applied

    if is_static:  # nothing animates, so no track is needed
        unreal.log("Camera lens is fixed (it does not change during the shot)")  # tell the Output Log
        return  # done

    try:  # keying a zoom needs a track on the camera COMPONENT, which is the less certain part of the API
        component_binding = sequence.add_possessable(component)  # bind the template's camera component so Sequencer can animate it
        track = component_binding.add_track(unreal.MovieSceneFloatTrack)  # a track that animates one float value
        track.set_property_name_and_path("CurrentFocalLength", "CurrentFocalLength")  # animate the focal length property
        section = track.add_section()  # one section for the shot
        section.set_range(0, frame_count)  # spans the whole shot
        channel = get_section_channels(section)[0]  # a float section has exactly one channel
        linear = unreal.MovieSceneKeyInterpolation.LINEAR  # straight lines between per-frame samples
        for index, value in enumerate(lens_values):  # one key per frame
            channel.add_key(unreal.FrameNumber(index), value, interpolation=linear)  # focal length in mm
    except Exception as error:  # the zoom track could not be created
        unreal.log_warning("Could not key the zoom, the camera keeps its first-frame lens: " + str(error))  # explain and carry on


# ----------------------------------------------------------------------
# MAIN BUILD FUNCTION
# ----------------------------------------------------------------------

def resolve_json_path(source):
    """Turn a folder or a file path into the full path of one *_camera.json file."""
    if any(ord(ch) < 32 for ch in source):  # control characters (like a carriage return) mean a backslash escape got in
        raise RuntimeError("The path contains a hidden control character. Re-type it using forward slashes only.")  # explain the fix

    json_path = source.replace("\\", "/").rstrip("/")  # forward slashes only, no trailing slash

    if os.path.isdir(json_path):  # the path points at a folder instead of a file
        candidates = glob.glob(json_path + "/*_camera.json")  # find every camera JSON in that folder
        if not candidates:  # nothing matching was found
            raise RuntimeError("No *_camera.json found in folder: " + json_path)  # tell the user where we looked
        json_path = max(candidates, key=os.path.getmtime).replace("\\", "/")  # use the most recently exported one
    elif not os.path.isfile(json_path):  # it is neither a folder nor an existing file
        raise RuntimeError("Path does not exist: " + json_path)  # tell the user which path failed

    return json_path  # the full path of the JSON file to use


# ----------------------------------------------------------------------
# SEQUENCE BUILD STEPS (each one does a single job and can be checked on its own)
# ----------------------------------------------------------------------

def describe_sequence(sequence, label):
    """Write what the sequence contains right now into the Output Log, so every step can be checked."""
    lines = []  # one text line per binding
    for binding in sequence.get_bindings():  # every character / camera binding in the sequence
        try:  # the name lookup differs slightly between versions
            binding_name = str(binding.get_display_name())  # the name shown in the Sequencer outliner
        except Exception:  # the function does not exist here
            binding_name = "(unnamed)"  # fall back to a placeholder
        track_names = [type(track).__name__ for track in binding.get_tracks()]  # the track types on this binding
        lines.append("    - " + binding_name + ": " + (", ".join(track_names) if track_names else "NO TRACKS"))  # one summary line
    unreal.log("[" + label + "] bindings in sequence: " + str(len(lines)) + ("\n" + "\n".join(lines) if lines else ""))  # print it


def log_camera_lens(sequence, label):
    """Write the camera's focal length and sensor width, as stored in the sequence, to the Output Log."""
    for binding in sequence.get_bindings():  # look at every binding in the sequence
        try:  # not every binding is a camera, so be careful
            template = binding.get_object_template()  # the actor stored inside the sequence for this binding
            component = template.get_cine_camera_component()  # the camera component (fails for non-cameras)
            filmback = component.get_editor_property("filmback")  # the sensor settings
            unreal.log("[" + label + "] camera lens stored in the sequence: " + str(component.get_editor_property("current_focal_length"))
                       + " mm, sensor width " + str(filmback.get_editor_property("sensor_width")) + " mm")  # print the stored values
        except Exception:  # this binding is not a camera
            continue  # skip it


def step_create_sequence(shot_folder, sequence_name, fps, frame_count):
    """STEP A: create a brand new, empty Level Sequence with the right frame rate and length."""
    sequence_path = shot_folder + "/" + sequence_name  # full Content Browser path of the sequence
    asset_editor = unreal.get_editor_subsystem(unreal.AssetEditorSubsystem)  # lets us open and close asset windows

    if unreal.EditorAssetLibrary.does_asset_exist(sequence_path):  # a sequence from a previous run is there
        old_sequence = unreal.EditorAssetLibrary.load_asset(sequence_path)  # load it so we can close its window
        asset_editor.close_all_editors_for_asset(old_sequence)  # close the old Sequencer tab first, otherwise it stays open showing a deleted (empty) asset
        if not unreal.EditorAssetLibrary.delete_asset(sequence_path):  # try to delete it
            raise RuntimeError("Could not delete the old sequence " + sequence_path + ". Close it in the Content Browser and try again.")  # stop with a clear message

    sequence = unreal.AssetToolsHelpers.get_asset_tools().create_asset(  # create a new Level Sequence asset
        asset_name=sequence_name,  # the asset's name
        package_path=shot_folder,  # the folder it goes in
        asset_class=unreal.LevelSequence,  # the kind of asset
        factory=unreal.LevelSequenceFactoryNew(),  # the factory that knows how to make Level Sequences
    )
    if sequence is None:  # creation failed
        raise RuntimeError("Could not create the Level Sequence " + sequence_path)  # stop with a clear message

    sequence.set_display_rate(unreal.FrameRate(fps, 1))  # set the frame rate, e.g. 24 over 1
    sequence.set_playback_start(0)  # the shot starts at frame 0
    sequence.set_playback_end(frame_count)  # and ends after the last frame
    describe_sequence(sequence, "A: sequence created")  # log the (empty) starting state
    return sequence  # hand the new sequence back


def check_binding_added(sequence, expected_count, what):
    """Stop with a clear message if the sequence did not really gain the binding we just added."""
    actual = len(sequence.get_bindings())  # how many bindings the sequence has right now
    if actual != expected_count:  # the sequence did not change the way we expected
        raise RuntimeError("Adding the " + what + " did not work: the sequence has " + str(actual) + " binding(s), expected " + str(expected_count) + ".")  # explain


def step_add_character(actor_subsystem, sequence, skeletal_mesh):
    """STEP B: add the character mesh to the sequence as a spawnable (the sequence creates it when it plays)."""
    # A spawnable is copied from an ACTOR, so we place a temporary actor made from the mesh, copy it into the sequence, then delete it.
    temp_actor = actor_subsystem.spawn_actor_from_object(skeletal_mesh, unreal.Vector(0.0, 0.0, 0.0))  # a temporary actor, used only as a pattern
    if temp_actor is None:  # the editor could not create the actor
        raise RuntimeError("Could not create a temporary actor from the skeletal mesh")  # stop with a clear message
    try:  # always remove the temporary actor, even if adding it fails
        binding = sequence.add_spawnable_from_instance(temp_actor)  # copy the actor into the sequence itself (no Sequencer window needed)
    finally:  # runs whether or not the line above worked
        actor_subsystem.destroy_actor(temp_actor)  # remove the temporary actor from the level
    check_binding_added(sequence, 1, "character")  # make sure the sequence really contains it now
    binding.set_display_name("Character")  # a friendly name in the Sequencer outliner
    describe_sequence(sequence, "B: character added")  # log what the sequence contains now
    return binding  # hand the binding back so the next step can add the animation to it


def step_add_animation(sequence, character_binding, animation, frame_count):
    """STEP C: add a skeletal animation track to the character and point it at the imported animation."""
    track = character_binding.add_track(unreal.MovieSceneSkeletalAnimationTrack)  # a track that plays animations
    section = track.add_section()  # one animation section
    section.set_range(0, frame_count)  # play it across the whole shot
    try:  # the safest way to change a struct property: read it, edit the copy, write it back
        params = section.get_editor_property("params")  # the section's animation settings
        params.set_editor_property("animation", animation)  # choose which animation to play
        section.set_editor_property("params", params)  # store the edited settings back on the section
    except Exception as error:  # this Unreal version does not allow it that way
        unreal.log_warning("Setting the animation the safe way failed (" + str(error) + "), trying the direct way")  # explain the fallback
        section.params.animation = animation  # the direct way, which works in many versions
    describe_sequence(sequence, "C: animation added")  # log what the sequence contains now


def step_add_camera(sequence, frames, frame_count, data):
    """STEP D: add a spawnable cine camera with its movement and lens from Blender."""
    binding = sequence.add_spawnable_from_class(unreal.CineCameraActor)  # a spawnable cine camera that lives inside the sequence (no Sequencer window needed)
    check_binding_added(sequence, 2, "camera")  # make sure the sequence really contains it now (character + camera)
    binding.set_display_name("Camera")  # a friendly name in the Sequencer outliner
    add_transform_keys(binding, frames, frame_count)  # key position and rotation every frame
    apply_camera_optics(binding, data, frames)  # match the sensor size and depth of field to Blender
    add_focal_length_keys(sequence, binding, frames, frame_count)  # set or key the lens
    describe_sequence(sequence, "D: camera added")  # log what the sequence contains now
    return binding  # hand the binding back so the camera cut can use it


def step_add_camera_cut(sequence, camera_binding, frame_count):
    """STEP E: add a camera cut so Sequencer looks through the camera."""
    cut_track = sequence.add_track(unreal.MovieSceneCameraCutTrack)  # the track that decides which camera is shown
    cut_section = cut_track.add_section()  # one cut covering the whole shot
    cut_section.set_range(0, frame_count)  # from the first frame to the end
    binding_id = get_binding_id(sequence, camera_binding)  # an id pointing at our camera
    cut_section.set_camera_binding_id(binding_id)  # make this cut look through our camera
    describe_sequence(sequence, "E: camera cut added")  # log what the sequence contains now


def report_step(progress, message):
    """Print a progress message and move the progress bar forward (if there is a bar)."""
    unreal.log(message)  # always write the message to the Output Log
    if progress is not None:  # the UI button passes a progress bar, a plain script run does not
        progress.enter_progress_frame(1, message)  # advance the bar by one step and show the message


def build_sequence(json_source=None, progress=None, sequence_name=None):
    """Do the whole job from JSON file to finished Level Sequence.

    json_source: a *_camera.json file, or a folder containing one. None means use JSON_PATH above.
    progress: an optional unreal.ScopedSlowTask (needs 6 steps) so the UI can show a progress bar.
    sequence_name: the name for the new Level Sequence asset. None means "<ShotName>_Seq".
    Returns the finished LevelSequence asset.
    """
    source = JSON_PATH if json_source is None else json_source  # pick the path to use
    json_path = resolve_json_path(source)  # turn a folder or file into one JSON file path

    report_step(progress, "Reading camera file")  # step 1 of 6
    unreal.log("Using camera file " + json_path)  # show which file was picked in the Output Log
    with open(json_path, "r") as handle:  # open the camera JSON for reading
        data = json.load(handle)  # parse it into a Python dictionary

    shot_name = data["shot_name"]  # e.g. Shot_01
    fps = int(round(data["fps"]))  # whole-number frame rate (Blender's validation warns about odd rates)
    frames = data["frames"]  # the per-frame camera records
    frame_count = len(frames)  # how many frames the shot has
    shot_folder = CONTENT_FOLDER + "/" + shot_name  # e.g. /Game/Shots/Shot_01
    fbx_path = json_path.rsplit("/", 1)[0] + "/" + data["character_fbx"]  # the FBX sits in the same folder as the JSON

    report_step(progress, "Importing character FBX")  # step 2 of 6
    unreal.log("Importing " + fbx_path)  # the full path, for the Output Log
    clean_shot_folder(shot_folder)  # remove the previous run's files so the import is always fresh (this also removes the old sequence)
    skeletal_mesh, animation = import_character(fbx_path, shot_folder)  # bring the character into the project
    try:  # textures are a bonus, so a failure here must NEVER stop the sequence from being built
        import_textures_and_build_materials(data, json_path.rsplit("/", 1)[0], skeletal_mesh, shot_folder)  # textures and materials, done BEFORE the character is added to the sequence
    except Exception:  # something in the texture step failed
        import traceback  # standard library module that formats the full error
        unreal.log_error("TEXTURE STEP FAILED (the sequence is still built without textures):\n" + traceback.format_exc())  # full details in the Output Log

    report_step(progress, "Creating Level Sequence")  # step 3 of 6
    sequence_name = sequence_name or (shot_name + "_Seq")  # use the name the user typed, or fall back to <ShotName>_Seq
    sequence_path = shot_folder + "/" + sequence_name  # full Content Browser path of the sequence
    sequence = step_create_sequence(shot_folder, sequence_name, fps, frame_count)  # STEP A: an empty sequence with the right settings

    actor_subsystem = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)  # used here only to clean up actors left in the level by older versions

    # Older versions of this script placed real actors in the level (possessables). Those are what turned RED
    # in Sequencer, because the sequence could not find them again. We now use spawnables instead, so this
    # just removes the old leftover actors (they carry these labels) so they do not sit in your level.
    remove_old_actors(actor_subsystem, [shot_name + "_Character", shot_name + "_Camera"])  # delete leftovers from earlier runs

    # ---- character ----
    report_step(progress, "Adding character and animation")  # step 4 of 6
    character_binding = step_add_character(actor_subsystem, sequence, skeletal_mesh)  # STEP B: the character mesh
    step_add_animation(sequence, character_binding, animation, frame_count)  # STEP C: the animation on that character

    # ---- camera ----
    report_step(progress, "Adding camera")  # step 5 of 6
    camera_binding = step_add_camera(sequence, frames, frame_count, data)  # STEP D: the camera, its movement, its lens and its optics
    step_add_camera_cut(sequence, camera_binding, frame_count)  # STEP E: look through that camera

    report_step(progress, "Saving sequence")  # step 6 of 6
    describe_sequence(sequence, "FINAL before saving")  # the last check of what we are about to save
    log_camera_lens(sequence, "before saving")  # the lens values we are about to save
    unreal.EditorAssetLibrary.save_loaded_asset(sequence)  # save the sequence to disk
    sequence = unreal.EditorAssetLibrary.load_asset(sequence_path)  # load the saved asset again so we open exactly what is on disk
    describe_sequence(sequence, "FINAL after reloading from disk")  # confirm that the saved asset really contains everything
    log_camera_lens(sequence, "after reloading")  # tells us whether the lens survived saving and loading
    unreal.get_editor_subsystem(unreal.AssetEditorSubsystem).open_editor_for_assets([sequence])  # open it in Sequencer
    unreal.log("Built " + sequence_path + " with " + str(frame_count) + " frames at " + str(fps) + " fps")  # done message
    return sequence  # hand the finished sequence back to the caller (the UI button uses this)


def get_binding_id(sequence, binding):
    """Return an object binding id for a binding, trying every way this Unreal version might offer."""
    attempts = (  # each entry is one possible way to get the id (none needs the enum that is missing in 5.7)
        lambda: unreal.MovieSceneSequenceExtensions.get_binding_id(sequence, binding),  # static helper on sequences
        lambda: unreal.MovieSceneBindingExtensions.get_binding_id(binding),  # static helper on bindings
        lambda: binding.get_binding_id(),  # method on the binding itself
        lambda: sequence.get_binding_id(binding),  # method on the sequence
    )
    last_error = None  # remember the last failure for the error message
    for attempt in attempts:  # try them one after another
        try:  # a missing function raises an error, which we ignore and move on
            return attempt()  # success
        except Exception as error:  # this way is not available here
            last_error = error  # remember why
    raise RuntimeError("Could not get a binding id in this Unreal version. Last error: " + str(last_error))  # nothing worked


def find_live_actor(sequence, binding):
    """Return the actor that a binding currently spawns in the level, or None if it is not spawned right now."""
    try:  # first choice: ask Sequencer what the binding is bound to
        binding_id = get_binding_id(sequence, binding)  # an id pointing at this binding
        for bound_object in unreal.LevelSequenceEditorBlueprintLibrary.get_bound_objects(binding_id):  # everything bound right now
            if isinstance(bound_object, unreal.Actor):  # we want the actor
                return bound_object  # found it
    except Exception as error:  # binding ids are not available here, use the name fallback below
        unreal.log_warning("Binding id lookup failed (" + str(error) + "), searching the level by name instead")  # tell the log
    wanted = str(binding.get_display_name()).lower()  # the name shown in Sequencer, e.g. the camera or character name
    editor_actors = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)  # lets us list actors in the open level
    for actor in editor_actors.get_all_level_actors():  # every actor in the level, including spawned ones
        names = (actor.get_actor_label().lower(), actor.get_name().lower())  # label and internal name
        if any(wanted == n or wanted in n for n in names):  # a match on either name
            return actor  # found it
    return None  # nothing is spawned for this binding at the moment


def set_camera_options(json_source, track_character=True):
    """Apply the Blender camera's lens, sensor and focus settings to the sequence that is open in Sequencer.

    Run this AFTER the sequence has been built. It changes the live camera, and keeps the character in focus
    with the Tracking focus method when track_character is True.
    """
    sequence = unreal.LevelSequenceEditorBlueprintLibrary.get_focused_level_sequence()  # the sequence open in the Sequencer window
    if sequence is None:  # nothing is open
        raise RuntimeError("Open your sequence in Sequencer first, then press the button again.")  # tell the user what to do

    json_path = resolve_json_path(json_source)  # turn a folder or file into one JSON file path
    with open(json_path, "r") as handle:  # open the camera JSON for reading
        data = json.load(handle)  # parse it into a Python dictionary
    frames = data["frames"]  # the per-frame camera records

    character = None  # the live character actor
    camera = None  # the live camera actor
    for binding in sequence.get_bindings():  # look at every binding in the open sequence
        name = str(binding.get_display_name())  # the name shown in the Sequencer outliner
        if name == "Character":  # our character binding
            character = find_live_actor(sequence, binding)  # the spawned character
        elif name == "Camera":  # our camera binding
            camera = find_live_actor(sequence, binding)  # the spawned camera
    if camera is None:  # the camera is not in the level yet
        raise RuntimeError("The camera is not spawned. Click the timeline in Sequencer once so the shot loads, then try again.")  # explain
    if track_character and character is None:  # we need the character to track it
        raise RuntimeError("The character is not spawned. Click the timeline in Sequencer once so the shot loads, then try again.")  # explain

    component = camera.get_cine_camera_component()  # the camera component holds the lens settings
    configure_camera_component(component, data, frames, character if track_character else None)  # apply lens, sensor and focus
    unreal.log("Camera options applied. Focal length is now " + str(component.get_editor_property("current_focal_length")) + " mm")  # read it back
    try:  # ask Sequencer to redraw so the change shows immediately
        unreal.LevelSequenceEditorBlueprintLibrary.refresh_current_level_sequence()  # refresh the Sequencer window
    except Exception:  # not available in this version
        pass  # the change is applied anyway


# Run the build only when this file is executed directly (py "path/to/ue_build_sequence.py").
# When the UI button imports this file as a module, __name__ is different, so nothing runs automatically.
if __name__ == "__main__":  # true only for a direct run
    build_sequence()  # start the whole process using JSON_PATH