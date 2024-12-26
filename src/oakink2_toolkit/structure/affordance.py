from __future__ import annotations


from typing import Optional, Union
from ..type_def import NamedData
import trimesh
from dataclasses import dataclass


@dataclass
class OakInk2__Affordance(NamedData):
    instantiated: bool = False

    obj_id: str = None
    obj_name: str = None

    has_model: bool = False
    obj_instance_id: str = None
    obj_part_id: list[str] = None
    obj_urdf_filepath: Optional[str] = None

    affordance_list: list[str] = None  # affordance, function-level
    affordance_instantiation_list: list[str] = None  # instantiated affordance, interaction (task)-level

    # field to be instantiated
    obj_mesh: Optional[
        Union[
            trimesh.Trimesh,
            dict[str, trimesh.Trimesh],
        ]
    ] = None
