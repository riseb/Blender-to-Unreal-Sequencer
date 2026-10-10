# ======================================================================
# init_unreal.py
# ----------------------------------------------------------------------
# Unreal automatically runs a file with this exact name when the editor
# starts, if it sits in <YourProject>/Content/Python/.
# Here it registers the "Import Sequencer" button.
# ======================================================================

import unreal  # Unreal's Python module

# We wait for the editor's first UI tick before adding menu entries, because the menus
# do not exist yet while init_unreal.py runs at the very start of startup.
_tick_handle = None  # will hold the id of our one-time tick callback


def _register_once(delta_seconds):
    """Runs once on the first editor UI tick, then removes itself."""
    global _tick_handle  # we need to change the module-level variable above
    unreal.unregister_slate_post_tick_callback(_tick_handle)  # stop this callback from running again
    try:  # never let a registration problem break editor startup
        import ue_sequencer_ui  # our UI module, found in Content/Python
        ue_sequencer_ui.register()  # add the toolbar button and Tools menu entry
    except Exception as error:  # something went wrong
        unreal.log_error("Import Sequencer UI failed to register: " + str(error))  # show it in the Output Log


_tick_handle = unreal.register_slate_post_tick_callback(_register_once)  # ask Unreal to call _register_once on the next tick
