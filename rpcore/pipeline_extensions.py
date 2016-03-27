"""

RenderPipeline

Copyright (c) 2014-2016 tobspr <tobias.springer1@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

"""

# Disable the member not found errors, since we are just extending the render
# pipeline class, so pylint cannot find those members.
# pylint: disable=E1101

import math

from panda3d.core import Vec3, Mat4, CS_zup_right, CS_yup_right

from rpcore.globals import Globals
from rpcore.effect import Effect
from rpcore.native import PointLight, SpotLight
from rpcore.gui.loading_screen import LoadingScreen, EmptyLoadingScreen

from rpcore.stages.ambient_stage import AmbientStage
from rpcore.stages.gbuffer_stage import GBufferStage
from rpcore.stages.final_stage import FinalStage
from rpcore.stages.downscale_z_stage import DownscaleZStage
from rpcore.stages.combine_velocity_stage import CombineVelocityStage
from rpcore.stages.upscale_stage import UpscaleStage

class PipelineExtensions(object):

    """ This class provides utility functions like generating a skybox, setting
    the loading screen, and much more. The render pipeline derives from this
    class. The functions are kept seperate to simplify the interface. """

    def set_loading_screen(self, loading_screen):
        """ Sets a loading screen to be used while loading the pipeline. When
        the pipeline gets constructed (and creates the showbase), create()
        will be called on the object. During the loading progress,
        progress(msg) will be called. After the loading is finished,
        remove() will be called. If a custom loading screen is passed, those
        methods should be implemented. """
        self._loading_screen = loading_screen

    def set_default_loading_screen(self):
        """ Tells the pipeline to use the default loading screen. """
        self._loading_screen = LoadingScreen(self)

    def set_empty_loading_screen(self):
        """ Tells the pipeline to use no loading screen """
        self._loading_screen = EmptyLoadingScreen()

    @property
    def loading_screen(self):
        """ Returns the current loading screen """
        return self._loading_screen

    def add_light(self, light):
        """ Adds a new light to the rendered lights, check out the LightManager
        add_light documentation for further information. """
        self.light_mgr.add_light(light)

    def remove_light(self, light):
        """ Removes a previously attached light, check out the LightManager
        remove_light documentation for further information. """
        self.light_mgr.remove_light(light)

    def _create_default_skybox(self, size=40000):
        """ Returns the default skybox, with a scale of <size>, and all
        proper effects and shaders already applied. The skybox is already
        parented to render as well. """
        skybox = self._com_resources.load_default_skybox()
        skybox.set_scale(size)
        skybox.reparent_to(Globals.render)
        self.set_effect(skybox, "effects/skybox.yaml", {
            "render_shadows": False,
            "render_envmap": False,
            "render_voxel": False,
            "alpha_testing": False,
            "normal_mapping": False,
            "parallax_mapping": False
        }, 1000)
        return skybox

    def load_ies_profile(self, filename):
        """ Loads an IES profile from a given filename and returns a handle which
        can be used to set an ies profile on a light """
        return self._ies_profile_mgr.load(filename)

    def set_effect(self, nodepath, effect_src, options=None, sort=30):
        """ Sets an effect to the given object, using the specified options.
        Check out the effect documentation for more information about possible
        options and configurations. The object should be a nodepath, and the
        effect will be applied to that nodepath and all nodepaths below whose
        current effect sort is less than the new effect sort (passed by the
        sort parameter). """

        effect = Effect.load(effect_src, options)
        if effect is None:
            return self.error("Could not apply effect")

        # Apply default stage shader
        if not effect.get_option("render_gbuffer"):
            nodepath.hide(self._tag_mgr.get_gbuffer_mask())
        else:
            nodepath.set_shader(effect.get_shader_obj("gbuffer"), sort)
            nodepath.show(self._tag_mgr.get_gbuffer_mask())

        # Apply shadow stage shader
        if not effect.get_option("render_shadows"):
            nodepath.hide(self._tag_mgr.get_shadow_mask())
        else:
            shader = effect.get_shader_obj("shadows")
            self._tag_mgr.apply_shadow_state(
                nodepath, shader, str(effect.effect_id), 25 + sort)
            nodepath.show(self._tag_mgr.get_shadow_mask())

        # Apply voxelization stage shader
        if not effect.get_option("render_voxel"):
            nodepath.hide(self._tag_mgr.get_voxelize_mask())
        else:
            shader = effect.get_shader_obj("voxelize")
            self._tag_mgr.apply_voxelize_state(
                nodepath, shader, str(effect.effect_id), 35 + sort)
            nodepath.show(self._tag_mgr.get_voxelize_mask())

        # Apply envmap stage shader
        if not effect.get_option("render_envmap"):
            nodepath.hide(self._tag_mgr.get_envmap_mask())
        else:
            shader = effect.get_shader_obj("envmap")
            self._tag_mgr.apply_envmap_state(
                nodepath, shader, str(effect.effect_id), 45 + sort)
            nodepath.show(self._tag_mgr.get_envmap_mask())

    def add_environment_probe(self):
        """ Constructs a new environment probe and returns the handle, so that
        the probe can be modified """

        # TODO: This method is super hacky
        if not self.plugin_mgr.is_plugin_enabled("env_probes"):
            self.warn("EnvProbe plugin is not loaded, can not add environment probe")
            class _dummy_probe(object):
                def __getattr__(self, *args, **kwargs):
                    return lambda *args, **kwargs: None
            return _dummy_probe()

        from rpplugins.env_probes.environment_probe import EnvironmentProbe
        probe = EnvironmentProbe()
        self.plugin_mgr.get_plugin_handle("env_probes").probe_mgr.add_probe(probe)
        return probe

    def prepare_scene(self, scene):
        """ Prepares a given scene, by converting panda lights to render pipeline
        lights """
        # TODO: IES profiles
        ies_profile = self.load_ies_profile("x_arrow_diffuse.ies")
        lights = []

        for light in scene.find_all_matches("**/+PointLight"):
            light_node = light.node()
            rp_light = PointLight()
            rp_light.pos = light.get_pos(Globals.base.render)
            rp_light.radius = light_node.max_distance
            rp_light.lumens = 100.0 * light_node.color.w
            rp_light.color = light_node.color.xyz
            rp_light.casts_shadows = light_node.shadow_caster
            rp_light.shadow_map_resolution = light_node.shadow_buffer_size.x
            self.add_light(rp_light)
            light.remove_node()
            lights.append(rp_light)

        for light in scene.find_all_matches("**/+Spotlight"):
            light_node = light.node()
            rp_light = SpotLight()
            rp_light.pos = light.get_pos(Globals.base.render)
            rp_light.radius = light_node.max_distance
            rp_light.lumens = 100.0 * light_node.color.w
            rp_light.color = light_node.color.xyz
            rp_light.casts_shadows = light_node.shadow_caster
            rp_light.shadow_map_resolution = light_node.shadow_buffer_size.x
            rp_light.fov = light_node.exponent / math.pi * 180.0
            lpoint = light.get_mat(Globals.base.render).xform_vec(Vec3(0, 0, -1))
            rp_light.direction = lpoint
            self.add_light(rp_light)
            light.remove_node()
            lights.append(rp_light)

            # XXX: Support IES profiles (Have to add it to the BAM exporter first)
            # rp_light.ies_profile = ies_profile

        envprobes = []

        # Add environment probes
        for np in scene.find_all_matches("**/ENVPROBE*"):
            probe = self.add_environment_probe()
            probe.set_mat(np.get_mat())
            probe.border_smoothness = 0.05
            np.remove_node()
            envprobes.append(probe)

        return {"lights": lights, "envprobes": envprobes}

    def _check_version(self):
        """ Internal method to check if the required Panda3D version is met. Returns
        True if the version is new enough, and False if the version is outdated. """

        from panda3d.core import PointLight as Panda3DPointLight
        if not hasattr(Panda3DPointLight(""), "shadow_caster"):
            return False

        return True

    def _init_common_stages(self):
        """ Inits the commonly used stages, which don't belong to any plugin,
        but yet are necessary and widely used. """
        add_stage = self.stage_mgr.add_stage

        self._ambient_stage = AmbientStage(self)
        add_stage(self._ambient_stage)

        self._gbuffer_stage = GBufferStage(self)
        add_stage(self._gbuffer_stage)

        self._final_stage = FinalStage(self)
        add_stage(self._final_stage)

        self._downscale_stage = DownscaleZStage(self)
        add_stage(self._downscale_stage)

        self._combine_velocity_stage = CombineVelocityStage(self)
        add_stage(self._combine_velocity_stage)

        # Add an upscale/downscale stage in case we render at a different resolution
        if abs(1 - self.settings["pipeline.resolution_scale"]) > 0.05:
            self._upscale_stage = UpscaleStage(self)
            add_stage(self._upscale_stage)

    def _set_default_effect(self):
        """ Sets the default effect used for all objects if not overridden """
        self.set_effect(Globals.render, "effects/default.yaml", {}, -10)
