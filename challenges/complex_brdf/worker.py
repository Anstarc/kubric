# Copyright 2022 The Kubric Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import pathlib
import logging
import numpy as np
import os

import kubric as kb
from kubric.renderer import Blender as KubricRenderer
import bpy
import os.path as osp
from glob import glob
import mathutils

# --- CLI arguments (and modified defaults)
parser = kb.ArgumentParser()
parser.set_defaults(
  seed=50000,
  frame_start=0,
  frame_end=23,
  resolution=(256,256))
parser.add_argument('--rubber',
  action='store_true',
  help='use rubber metal')
FLAGS = parser.parse_args()

# --- Common setups
kb.utils.setup_logging(FLAGS.logging_level)
kb.utils.log_my_flags(FLAGS)
job_dir = kb.as_path(FLAGS.job_dir)
rng = np.random.RandomState(FLAGS.seed)
scene = kb.Scene.from_flags(FLAGS)

# --- Load the cameras
local_path = pathlib.Path(__file__).parent.resolve()
data = np.load(local_path / "cameras.npz")

# --- Add a renderer
renderer = KubricRenderer(scene,
  use_denoising=True,
  adaptive_sampling=False,
  background_transparency=True)

# TODO(klausg): why is this necessary?
bpy.context.scene.render.resolution_x = FLAGS.resolution[0]
bpy.context.scene.render.resolution_y = FLAGS.resolution[1]

# --- Add Klevr-like lights to the scene
scene += kb.assets.utils.get_lfn_lights(rng=rng)
scene.ambient_illumination = kb.Color(0.05, 0.05, 0.05)

# --- Fetch shapenet
source_path = os.getenv("SHAPENET_GCP_BUCKET", "gs://kubric-public/assets/ShapeNetCore.v2.json")
asset_source = kb.AssetSource.from_manifest(source_path)

# --- Fetch a random asset from shapenet
ids = list(asset_source._assets.keys())
asset_id = ids[FLAGS.seed % len(ids)]
obj = asset_source.create(asset_id=asset_id)
logging.info(f"selected '{asset_id}'")

# --- make object flat on X/Y and not penetrate floor
obj.quaternion = kb.Quaternion(axis=[1,0,0], degrees=90)

# --- Add floor (~infinitely large sphere)
scene += kb.Sphere(name="floor", scale=1000, position=(0, 0, +1000 + obj.aabbox[0][2]), background=True, static=True)

obj.metadata = {
    "asset_id": obj.asset_id,
    "category": asset_source._assets[asset_id]["metadata"]["category"]
    # TODO(klausg): check this matches in the new API
    # "category": asset_source.db[asset_source.db["id"] == obj.asset_id].iloc[0]["category_name"],
}
scene.add(obj)
object = bpy.context.scene.objects[-1]

# --- Renormalize objects
v_min = []
v_max = []
for i in range(3):
    v_min.append(min([vertex.co[i] for vertex in object.data.vertices]))
    v_max.append(max([vertex.co[i] for vertex in object.data.vertices]))

v_min = mathutils.Vector(v_min)
v_max = mathutils.Vector(v_max)
scale = max(v_max - v_min)
v_shift = (v_max - v_min) / 2 / scale
# 
for v in object.data.vertices:
    v.co -= v_min
    v.co /= scale
    v.co -= v_shift
    v.co *= 1.0

scene.camera = kb.PerspectiveCamera()
azimuths = np.linspace(0, 360., FLAGS.frame_end - FLAGS.frame_start + 2)
counter = 0

for frame in range(FLAGS.frame_start, FLAGS.frame_end + 1):
  # scene.camera.position = (1, 1, 1)  #< frozen camera
  mat = data['world_mat_inv_{}'.format(frame)]
  scene.camera.position = mat[:3, -1][[0, 2, 1]]
  scene.camera.look_at((0, 0, 0))
  scene.camera.keyframe_insert("position", frame)
  scene.camera.keyframe_insert("quaternion", frame)

  counter = counter + 1

print("counter: ", counter)
print("azimuth shape: ", azimuths.shape)

def add_material(name, obj, **properties):
  """
  Create a new material and assign it to the active object. "name" should be the
  name of a material that has been previously loaded using load_materials.
  """
  # Figure out how many materials are already in the scene
  mat_count = len(bpy.data.materials)

  # Create a new material; it is not attached to anything and
  # it will be called "Material"
  bpy.ops.material.new()

  # Get a reference to the material we just created and rename it;
  # then the next time we make a new material it will still be called
  # "Material" and we will still be able to look it up by name
  mat = bpy.data.materials['Material']
  mat.name = 'Material_%d' % mat_count
  mat.use_nodes = True

  for i in range(len(obj.material_slots)):
      bpy.ops.object.material_slot_remove({'object': obj})

  # if obj.data.materials:
  #     obj.data.materials[0] = mat
  # else:
  assert len(obj.data.materials) == 0
  obj.data.materials.append(mat)

  # Find the output node of the new material
  output_node = None
  for n in mat.node_tree.nodes:
    if n.name == 'Material Output':
      output_node = n
      break

  # Add a new GroupNode to the node tree of the active material,
  # and copy the node tree from the preloaded node group to the
  # new group node. This copying seems to happen by-value, so
  # we can create multiple materials of the same type without them
  # clobbering each other
  group_node = mat.node_tree.nodes.new('ShaderNodeGroup')
  group_node.node_tree = bpy.data.node_groups[name]

  # Find and set the "Color" input of the new group node
  for inp in group_node.inputs:
    if inp.name in properties:
      inp.default_value = properties[inp.name]

  # Wire the output of the new group node to the input of
  # the MaterialOutput node
  mat.node_tree.links.new(
      group_node.outputs['Shader'],
      output_node.inputs['Surface'],
  )


bpy.ops.wm.append(filename=str(local_path/"MyMetal.blend"/"NodeTree"/"MyMetal"))
bpy.ops.wm.append(filename=str(local_path/"Rubber.blend"/"NodeTree"/"Rubber"))

rand_color = rng.uniform(0, 1, (3,))
color = (rand_color[0], rand_color[1], rand_color[2], 1)

if FLAGS.rubber:
    add_material('Rubber', object, Color=color)
    object.cycles_visibility.shadow = False
else:
    add_material('MyMetal', object, Color=color)

# --- Saving state;  WARNING: uses a lot of disk space
# logging.info("Saving 'scene.blend' file...")
# renderer.save_state(job_dir / "scene.blend")

# --- Rendering
logging.info("Rendering the scene ...")
data_stack = renderer.render()

# --- Postprocessing
data_stack["segmentation"] = kb.adjust_segmentation_idxs(
    data_stack["segmentation"],
    scene.assets,
    [obj]).astype(np.uint8)

# --- Save to image files
kb.file_io.write_rgba_batch(data_stack["rgba"], job_dir)
kb.file_io.write_depth_batch(data_stack["depth"], job_dir)
kb.file_io.write_segmentation_batch(data_stack["segmentation"], job_dir)

# --- Collect metadata
logging.info("Collecting and storing metadata for each object.")
data = {
  "metadata": kb.get_scene_metadata(scene),
  "camera": kb.get_camera_info(scene.camera),
  "instances": kb.get_instance_info(scene),
}
kb.file_io.write_json(filename=job_dir / "metadata.json", data=data)
kb.done()
