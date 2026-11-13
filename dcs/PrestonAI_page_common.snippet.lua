-- GeorgeBridge addition for PrestonAI_page_common.lua (DCS AH-64D).
-- Paste directly after AddElement(mode_text), before the --CPG AI comment.
-- See README.md, installation step 3. Back up the original file first.

--GeorgeBridge: off-screen mirror of mode_text. mode_text itself is parented
--into compass_anchor, whose show_compass controller hides the whole subtree
--when the wheel is not drawn, so list_indication() reports nothing and the
--menu mode becomes unreadable. This copy is parentless, so it never inherits
--that visibility, and sits outside the viewport so it never renders.
local mode_text_bridge			= CreateElement "ceStringPoly"
mode_text_bridge.name			= "mode_text_bridge"
mode_text_bridge.material		= arial_font
mode_text_bridge.stringdefs		= text_defs_compass
mode_text_bridge.init_pos		= {9.0, 9.0}
mode_text_bridge.alignment		= "CenterCenter"
mode_text_bridge.controllers	= {{'mode_text'}}
AddElement(mode_text_bridge)

--GeorgeBridge: off-screen mirrors of the CP/G wheel labels, for the same
--reason as mode_text_bridge above. The originals are parented into
--weap_contol, whose show_weap_control controller hides them when the wheel is
--not drawn. The near ring is each direction's short-press function and the far
--ring its long-press function, so reading these says what the wheel is
--actually offering right now -- the CP/G wheel changes its functions with
--context, so a direction does not always mean the same thing.
local weapon_center_bridge	= CreateElement "ceStringPoly"
weapon_center_bridge.name	= "weapon_center_bridge"
weapon_center_bridge.material	= arial_font
weapon_center_bridge.stringdefs	= text_defs_weap_control
weapon_center_bridge.init_pos	= {9.0, 9.0}
weapon_center_bridge.alignment	= "CenterCenter"
weapon_center_bridge.controllers	= {{'weapon_center'}}
AddElement(weapon_center_bridge)
local weapon_up_bridge	= CreateElement "ceStringPoly"
weapon_up_bridge.name	= "weapon_up_bridge"
weapon_up_bridge.material	= arial_font
weapon_up_bridge.stringdefs	= text_defs_weap_control
weapon_up_bridge.init_pos	= {9.0, 9.0}
weapon_up_bridge.alignment	= "CenterCenter"
weapon_up_bridge.controllers	= {{'weapon_up'}}
AddElement(weapon_up_bridge)
local weapon_right_bridge	= CreateElement "ceStringPoly"
weapon_right_bridge.name	= "weapon_right_bridge"
weapon_right_bridge.material	= arial_font
weapon_right_bridge.stringdefs	= text_defs_weap_control
weapon_right_bridge.init_pos	= {9.0, 9.0}
weapon_right_bridge.alignment	= "CenterCenter"
weapon_right_bridge.controllers	= {{'weapon_right'}}
AddElement(weapon_right_bridge)
local weapon_down_bridge	= CreateElement "ceStringPoly"
weapon_down_bridge.name	= "weapon_down_bridge"
weapon_down_bridge.material	= arial_font
weapon_down_bridge.stringdefs	= text_defs_weap_control
weapon_down_bridge.init_pos	= {9.0, 9.0}
weapon_down_bridge.alignment	= "CenterCenter"
weapon_down_bridge.controllers	= {{'weapon_down'}}
AddElement(weapon_down_bridge)
local weapon_left_bridge	= CreateElement "ceStringPoly"
weapon_left_bridge.name	= "weapon_left_bridge"
weapon_left_bridge.material	= arial_font
weapon_left_bridge.stringdefs	= text_defs_weap_control
weapon_left_bridge.init_pos	= {9.0, 9.0}
weapon_left_bridge.alignment	= "CenterCenter"
weapon_left_bridge.controllers	= {{'weapon_left'}}
AddElement(weapon_left_bridge)
local weapon_far_up_bridge	= CreateElement "ceStringPoly"
weapon_far_up_bridge.name	= "weapon_far_up_bridge"
weapon_far_up_bridge.material	= arial_font
weapon_far_up_bridge.stringdefs	= text_defs_weap_control
weapon_far_up_bridge.init_pos	= {9.0, 9.0}
weapon_far_up_bridge.alignment	= "CenterCenter"
weapon_far_up_bridge.controllers	= {{'weapon_far_up'}}
AddElement(weapon_far_up_bridge)
local weapon_far_right_bridge	= CreateElement "ceStringPoly"
weapon_far_right_bridge.name	= "weapon_far_right_bridge"
weapon_far_right_bridge.material	= arial_font
weapon_far_right_bridge.stringdefs	= text_defs_weap_control
weapon_far_right_bridge.init_pos	= {9.0, 9.0}
weapon_far_right_bridge.alignment	= "CenterCenter"
weapon_far_right_bridge.controllers	= {{'weapon_far_right'}}
AddElement(weapon_far_right_bridge)
local weapon_far_down_bridge	= CreateElement "ceStringPoly"
weapon_far_down_bridge.name	= "weapon_far_down_bridge"
weapon_far_down_bridge.material	= arial_font
weapon_far_down_bridge.stringdefs	= text_defs_weap_control
weapon_far_down_bridge.init_pos	= {9.0, 9.0}
weapon_far_down_bridge.alignment	= "CenterCenter"
weapon_far_down_bridge.controllers	= {{'weapon_far_down'}}
AddElement(weapon_far_down_bridge)
local weapon_far_left_bridge	= CreateElement "ceStringPoly"
weapon_far_left_bridge.name	= "weapon_far_left_bridge"
weapon_far_left_bridge.material	= arial_font
weapon_far_left_bridge.stringdefs	= text_defs_weap_control
weapon_far_left_bridge.init_pos	= {9.0, 9.0}
weapon_far_left_bridge.alignment	= "CenterCenter"
weapon_far_left_bridge.controllers	= {{'weapon_far_left'}}
AddElement(weapon_far_left_bridge)

