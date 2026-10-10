# ======================================================================
# UE Sequencer UI - STAGE 2.5 (Unreal side, Unreal Engine 5 Python)
# ----------------------------------------------------------------------
# What this does:
#   - Adds an "Import Sequencer" button to the Level Editor toolbar
#   - Adds the same command to the Tools menu (a guaranteed fallback)
#   - Clicking it:
#       1. asks you to pick the Blender export FOLDER
#       2. asks you to name the new Level Sequence (pre-filled from the shot name)
#       3. asks before replacing a sequence that already has that name
#       4. shows a progress bar and builds the sequence using
#          ue_build_sequence.build_sequence()
#
# Install:
#   Put ue_build_sequence.py, ue_sequencer_ui.py and init_unreal.py in
#   <YourProject>/Content/Python/   (create the Python folder if needed)
#   then restart the editor. init_unreal.py registers the button on startup.
# ======================================================================

import unreal  # Unreal's Python module, only available inside the Unreal Editor
import json  # standard library module, used to read the shot name and remember the last folder
import os  # standard library module for checking whether files and folders exist
import subprocess  # standard library module, used to open the Windows dialogs

# The command Unreal runs when the button is clicked (a short Python snippet as text)
BUTTON_COMMAND = "import ue_sequencer_ui; ue_sequencer_ui.run_import()"

# The command for the second button, which applies the Blender camera settings to the open sequence
CAMERA_OPTIONS_COMMAND = "import ue_sequencer_ui; ue_sequencer_ui.run_camera_options()"

# Name of the small settings file that remembers the last folder you imported from
SETTINGS_FILE = unreal.Paths.project_saved_dir() + "ImportSequencer_settings.json"  # Unreal returns forward slashes


# ----------------------------------------------------------------------
# REMEMBERING THE LAST FOLDER
# ----------------------------------------------------------------------

def load_last_folder():
    """Return the folder you last imported from, or an empty string if there is none."""
    if not os.path.isfile(SETTINGS_FILE):  # first run, nothing saved yet
        return ""  # no remembered folder
    try:  # the file could be damaged, so be careful
        with open(SETTINGS_FILE, "r") as handle:  # open the settings file for reading
            return json.load(handle).get("last_folder", "")  # read the saved folder (empty if the key is missing)
    except Exception:  # unreadable or invalid JSON
        return ""  # behave as if nothing was saved


def save_last_folder(folder):
    """Remember this folder so the folder picker opens there next time."""
    try:  # saving is a nicety, so never let a failure break the import
        with open(SETTINGS_FILE, "w") as handle:  # open the settings file for writing
            json.dump({"last_folder": folder}, handle)  # store the folder as JSON
    except Exception as error:  # could not write the file
        unreal.log_warning("Could not save Import Sequencer settings: " + str(error))  # log it and carry on


# ----------------------------------------------------------------------
# WINDOWS DIALOGS (folder picker and name prompt)
# ----------------------------------------------------------------------

def run_powershell(script):
    """Run a PowerShell script, wait for it to finish, and return whatever it printed."""
    result = subprocess.run(  # start PowerShell and wait for the dialog to close
        ["powershell", "-NoProfile", "-STA", "-Command", script],  # -STA is required for Windows dialogs
        capture_output=True,  # capture what PowerShell prints
        text=True,  # give us text instead of raw bytes
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # do not flash a black console window
    )
    return result.stdout.strip()  # the printed text without surrounding whitespace


def escape_for_powershell(text):
    """Double any single quotes so the text cannot break out of a PowerShell string."""
    return text.replace("'", "''")  # PowerShell escapes a single quote by doubling it


def choose_folder(initial_folder):
    """Open a Windows folder picker and return the chosen folder (forward slashes), or '' if cancelled."""
    start_dir = escape_for_powershell(initial_folder.replace("/", "\\")) if initial_folder else ""  # Windows wants backslashes here

    script = (  # a small PowerShell program that shows the standard folder dialog
        "Add-Type -AssemblyName System.Windows.Forms; "  # load the Windows Forms library
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "  # create the folder dialog
        "$d.Description = 'Select the Blender export folder'; "  # text shown at the top of the dialog
        "$d.ShowNewFolderButton = $false; "  # we only want existing folders
        "$d.SelectedPath = '" + start_dir + "'; "  # start in the last used folder
        "if ($d.ShowDialog() -eq 'OK') { Write-Output $d.SelectedPath }"  # print the folder if the user pressed OK
    )
    return run_powershell(script).replace("\\", "/")  # the chosen folder with forward slashes ('' if cancelled)


def ask_text(prompt, title, default):
    """Show a small text box asking for a value and return what was typed ('' if cancelled)."""
    script = (  # a small PowerShell program that shows an input box
        "Add-Type -AssemblyName Microsoft.VisualBasic; "  # load the library that provides InputBox
        "[Microsoft.VisualBasic.Interaction]::InputBox('"  # call the input box
        + escape_for_powershell(prompt) + "', '"  # the question shown to the user
        + escape_for_powershell(title) + "', '"  # the window title
        + escape_for_powershell(default) + "')"  # the pre-filled answer
    )
    return run_powershell(script)  # whatever the user typed ('' if they cancelled)


def clean_asset_name(name):
    """Make a safe Unreal asset name: letters, digits and underscores only."""
    safe = ""  # we build the cleaned name in this string
    for ch in name.strip():  # look at every character of the trimmed name
        if ch.isalnum() or ch == "_":  # letters, digits and underscores are allowed
            safe += ch  # keep it
        else:  # spaces, dots, dashes, slashes and so on are not allowed in asset names
            safe += "_"  # replace it with an underscore
    return safe  # the cleaned name ('' if the input was empty)


# ----------------------------------------------------------------------
# THE BUTTON'S ACTION
# ----------------------------------------------------------------------

def show_message(text):
    """Show a simple pop-up with an OK button."""
    unreal.EditorDialog.show_message("Import Sequencer", text, unreal.AppMsgType.OK)  # title, text, button type


def run_import():
    """Runs when the Import Sequencer button is clicked."""
    import importlib  # lets us reload a module without restarting Unreal
    import ue_build_sequence  # our builder module, found in Content/Python
    importlib.reload(ue_build_sequence)  # reload it so any edits you make take effect on the next click

    try:  # catch any error so we can show it nicely instead of failing silently
        # ---- step 1: choose the export folder ----
        folder = choose_folder(load_last_folder())  # open the folder picker, starting in the last used folder
        if not folder:  # the user closed the dialog without choosing
            unreal.log("Import Sequencer cancelled")  # nothing to do
            return  # stop quietly

        # ---- step 2: read the shot name from the export so we can suggest a sequence name ----
        json_path = ue_build_sequence.resolve_json_path(folder)  # find the newest *_camera.json in the folder
        with open(json_path, "r") as handle:  # open it for reading
            shot_name = json.load(handle)["shot_name"]  # e.g. Shot_01
        default_name = shot_name + "_Seq"  # the suggested sequence name, e.g. Shot_01_Seq

        # ---- step 3: ask what to call the sequence ----
        typed = ask_text("Name for the new Level Sequence:", "Import Sequencer", default_name)  # show the name prompt
        sequence_name = clean_asset_name(typed)  # make sure the name is valid for Unreal
        if not sequence_name:  # empty means the user cancelled
            unreal.log("Import Sequencer cancelled")  # nothing to do
            return  # stop quietly

        # ---- step 4: ask before replacing an existing sequence ----
        sequence_path = ue_build_sequence.CONTENT_FOLDER + "/" + shot_name + "/" + sequence_name  # where the sequence will live
        if unreal.EditorAssetLibrary.does_asset_exist(sequence_path):  # a sequence with this name is already there
            answer = unreal.EditorDialog.show_message(  # ask a yes or no question
                "Import Sequencer",  # pop-up title
                sequence_name + " already exists. Replace it?",  # the question
                unreal.AppMsgType.YES_NO,  # Yes and No buttons
            )
            if answer != unreal.AppReturnType.YES:  # the user did not say yes
                unreal.log("Import Sequencer cancelled (sequence not replaced)")  # nothing to do
                return  # stop quietly

        save_last_folder(folder)  # remember this folder for next time

        # ---- step 5: build the sequence with a progress bar ----
        with unreal.ScopedSlowTask(6, "Importing sequencer...") as task:  # a progress bar with 6 steps
            task.make_dialog()  # show the progress window
            ue_build_sequence.build_sequence(json_path, task, sequence_name)  # build everything, advancing the bar as it goes

    except Exception as error:  # something failed along the way
        unreal.log_error("Import Sequencer failed: " + str(error))  # full message in the Output Log
        show_message("The import failed:\n\n" + str(error) + "\n\nSee the Output Log for details.")  # and a pop-up so it is not silent


def run_camera_options():
    """Runs when the Set Camera Options button is clicked: applies the Blender camera settings to the open sequence."""
    import importlib  # lets us reload a module without restarting Unreal
    import ue_build_sequence  # our builder module, found in Content/Python
    importlib.reload(ue_build_sequence)  # reload it so any edits you make take effect on the next click

    try:  # catch any error so we can show it nicely instead of failing silently
        sequence = unreal.LevelSequenceEditorBlueprintLibrary.get_focused_level_sequence()  # the sequence open in Sequencer
        if sequence is None:  # nothing is open
            show_message("Open your sequence in Sequencer first, then press Set Camera Options again.")  # tell the user what to do
            return  # stop quietly

        folder = load_last_folder()  # the export folder used by the last import
        if not folder or not os.path.isdir(folder):  # nothing remembered, or the folder is gone
            folder = choose_folder(folder)  # ask the user for the export folder
            if not folder:  # the user closed the dialog without choosing
                unreal.log("Set Camera Options cancelled")  # nothing to do
                return  # stop quietly

        # Prefer the JSON that belongs to this sequence: /Game/Shots/Shot_03/Shot_03_Seq -> Shot_03_camera.json
        shot_name = sequence.get_path_name().split("/")[-2] if "/" in sequence.get_path_name() else ""  # the shot folder name
        matching = folder + "/" + shot_name + "_camera.json"  # where that shot's JSON should be
        source = matching if os.path.isfile(matching) else folder  # use it, otherwise let the builder pick the newest JSON
        unreal.log("Set Camera Options using " + source)  # show which file is used in the Output Log

        ue_build_sequence.set_camera_options(source, track_character=True)  # apply the camera settings and track the character
        show_message("Camera options applied.\n\nThe lens and sensor now match Blender, and the focus follows the character.")  # confirm
    except Exception as error:  # something failed along the way
        unreal.log_error("Set Camera Options failed: " + str(error))  # full message in the Output Log
        show_message("Set Camera Options failed:\n\n" + str(error) + "\n\nSee the Output Log for details.")  # and a pop-up so it is not silent


# ----------------------------------------------------------------------
# REGISTERING THE BUTTON AND MENU ENTRY
# ----------------------------------------------------------------------

def make_entry(entry_type, entry_name="ImportSequencerEntry", label="Import Sequencer",
               tool_tip="Import a Blender animation export and build a Level Sequence", command=BUTTON_COMMAND):
    """Create a menu or toolbar entry that runs a Python command."""
    entry = unreal.ToolMenuEntry(name=entry_name, type=entry_type)  # the entry, with a fixed name so reruns replace it
    entry.set_label(label)  # the text on the button
    entry.set_tool_tip(tool_tip)  # hover text
    entry.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", command)  # run our Python snippet on click
    try:  # the icon is cosmetic, so a missing icon must not stop registration
        entry.set_icon("AppStyle", "ClassIcon.LevelSequence")  # use Unreal's Level Sequence icon
    except Exception:  # style name not found in this version
        pass  # carry on without an icon
    return entry  # hand the finished entry back


def register():
    """Add the button to the toolbar and the entry to the Tools menu."""
    menus = unreal.ToolMenus.get()  # the editor's menu system

    # ---- Tools menu entry (stable, works in every UE5 version) ----
    try:  # wrap each registration so one failure does not block the other
        tools_menu = menus.find_menu("LevelEditor.MainMenu.Tools")  # the Tools menu in the top menu bar
        tools_menu.add_menu_entry("ImportSequencerSection", make_entry(unreal.MultiBlockType.MENU_ENTRY))  # add our entry in its own section
        tools_menu.add_menu_entry("ImportSequencerSection", make_entry(  # the second entry, in the same section
            unreal.MultiBlockType.MENU_ENTRY, "SetCameraOptionsEntry", "Set Camera Options",
            "Apply the Blender lens, sensor and focus settings to the sequence open in Sequencer", CAMERA_OPTIONS_COMMAND))
        unreal.log("Import Sequencer: added to the Tools menu")  # confirmation in the Output Log
    except Exception as error:  # the menu was not found
        unreal.log_warning("Import Sequencer: Tools menu registration failed: " + str(error))  # explain what happened

    # ---- Level Editor toolbar button ----
    try:  # the toolbar layout changes between versions, so be careful
        toolbar = menus.find_menu("LevelEditor.LevelEditorToolBar.PlayToolBar")  # the toolbar next to the Play button
        toolbar.add_menu_entry("ImportSequencerSection", make_entry(unreal.MultiBlockType.TOOL_BAR_BUTTON))  # add our button
        toolbar.add_menu_entry("ImportSequencerSection", make_entry(  # the second button, next to the first
            unreal.MultiBlockType.TOOL_BAR_BUTTON, "SetCameraOptionsEntry", "Set Camera Options",
            "Apply the Blender lens, sensor and focus settings to the sequence open in Sequencer", CAMERA_OPTIONS_COMMAND))
        unreal.log("Import Sequencer: added to the toolbar")  # confirmation in the Output Log
    except Exception as error:  # the toolbar was not found
        unreal.log_warning("Import Sequencer: toolbar registration failed: " + str(error))  # explain what happened

    menus.refresh_all_widgets()  # redraw the editor menus so the new entries appear straight away


# Lets you test by running this file directly (the button then needs the files in Content/Python)
if __name__ == "__main__":  # true only for a direct run
    register()  # add the button and menu entry