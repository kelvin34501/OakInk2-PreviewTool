from __future__ import annotations

import os
import numpy as np
import torch
import trimesh
import networkx as nx
import json
import pickle
import ast
import typing

if typing.TYPE_CHECKING:
    from typing import Optional

from . import meta
from . import tool
from . import program
from .structure.affordance import OakInk2__Affordance
from .structure.primitive_task import OakInk2__PrimitiveTask
from .structure.complex_task import OakInk2__ComplexTask


def load_obj(obj_prefix: str, obj_id: str):
    obj_filedir = os.path.join(obj_prefix, obj_id)
    candidate_list = [el for el in os.listdir(obj_filedir) if os.path.splitext(el)[-1] in [".obj", ".ply"]]
    assert len(candidate_list) == 1
    obj_filename = candidate_list[0]
    obj_filepath = os.path.join(obj_filedir, obj_filename)
    if os.path.splitext(obj_filename)[-1] == ".obj":
        mesh = trimesh.load(obj_filepath, process=False, skip_materials=True, force="mesh")
    else:
        mesh = trimesh.load(obj_filepath, process=False, force="mesh")
    return mesh


def load_json(json_filepath: str):
    with open(json_filepath, "r") as f:
        data = json.load(f)
    return data


def try_load_json(json_filepath: str):
    if os.path.exists(json_filepath):
        with open(json_filepath, "r") as ifs:
            data = json.load(ifs)
    else:
        data = None
    return data


def standardize_tuple(t):
    return str(ast.literal_eval(t))


def load_program_info(program_info_filepath: str):
    program_info = load_json(program_info_filepath)
    program_info = {standardize_tuple(k): v for k, v in program_info.items()}
    return program_info


def load_desc_info(desc_info_filepath: str):
    desc_info = load_json(desc_info_filepath)
    desc_info = {standardize_tuple(k): v for k, v in desc_info.items()}
    return desc_info


def fix_g_info(g_info, affordance_task_namemap):
    # if all node in g_info is in affordance_task_namemap, then return g_info
    # else, find all edges related to the node and contract it
    node_to_contact = []
    for k in g_info["id_map"]:
        k = standardize_tuple(k)
        if k not in affordance_task_namemap:
            node_to_contact.append(k)
    if len(node_to_contact) == 0:
        return g_info

    # contract the node
    # the graph is directed, link in-edge's parent to out-edge's child
    for node in node_to_contact:
        node_id = g_info["id_map"][node]
        in_edges = [e for e in g_info["e"] if e[1] == node_id]
        out_edges = [e for e in g_info["e"] if e[0] == node_id]
        for e in in_edges:
            for e2 in out_edges:
                g_info["e"].append((e[0], e2[1]))
        g_info["e"] = [e for e in g_info["e"] if e[0] != node_id and e[1] != node_id]
        g_info["id_map"] = {k: v for k, v in g_info["id_map"].items() if k != node}

    return g_info


def rev_part_tree(part_tree):
    res = {}
    for k, v in part_tree.items():
        for part in v:
            res[part] = k
    return res


def get_part_tree_root(rev_part_tree, obj_id):
    while obj_id in rev_part_tree:
        obj_id = rev_part_tree[obj_id]
    return obj_id


class OakInk2__Dataset(torch.utils.data.Dataset):
    def __init__(
        self,
        dataset_prefix: str,
        return_instantiated: bool = False,
        anno_offset: str = "anno_preview",
        obj_offset: str = "object_raw",
        affordance_offset: str = "object_affordance",
    ):
        self.dataset_prefix = dataset_prefix

        self.data_prefix = os.path.join(self.dataset_prefix, "data")
        self.anno_prefix = os.path.join(self.dataset_prefix, anno_offset)
        self.obj_prefix = os.path.join(self.dataset_prefix, obj_offset)
        self.obj_model_prefix = os.path.join(self.obj_prefix, "align_ds")
        self.program_prefix = os.path.join(self.dataset_prefix, "program")
        self.program_extension_prefix = os.path.join(self.dataset_prefix, "program_extension")
        self.obj_affordance_prefix = os.path.join(self.dataset_prefix, "object_affordance")

        task_target_filepath = os.path.join(self.program_prefix, "task_target.json")
        self.task_target = load_json(task_target_filepath)
        self.program_info_filedir = os.path.join(self.program_prefix, "program_info")
        self.pdg_filedir = os.path.join(self.program_prefix, "pdg")
        self.desc_info_filedir = os.path.join(self.program_prefix, "desc_info")
        self.initial_condition_info_filedir = os.path.join(self.program_prefix, "initial_condition_info")

        obj_desc_filepath = os.path.join(self.obj_prefix, "obj_desc.json")
        self.obj_desc = load_json(obj_desc_filepath)

        # affordance related
        self.obj_affordance_part_filedir = os.path.join(self.obj_affordance_prefix, "affordance_part")
        part_desc_filepath = os.path.join(self.obj_affordance_prefix, "part_desc.json")
        self.part_desc = load_json(part_desc_filepath)
        instance_id_filepath = os.path.join(self.obj_affordance_prefix, "instance_id.json")
        self.instance_id = load_json(instance_id_filepath)
        part_tree_filepath = os.path.join(self.obj_affordance_prefix, "object_part_tree.json")
        self.part_tree = load_json(part_tree_filepath)
        self.rev_part_tree = rev_part_tree(self.part_tree)
        object_affordance_filepath = os.path.join(self.obj_affordance_prefix, "object_affordance.json")
        self.object_affordance = load_json(object_affordance_filepath)

        self.all_seq_list = list(self.task_target.keys())

        # mode
        self.return_instantiated = return_instantiated

        # cache
        self.obj_cache = {}

    def __getitem__(self, index):
        seq_key = self.all_seq_list[index]
        res = self.load_complex_task(seq_key, self.return_instantiated)
        return res

    def __len__(self):
        return len(self.all_seq_list)

    def _load_obj(self, obj_part_id: str) -> trimesh.Trimesh:
        try:
            res = load_obj(self.obj_model_prefix, obj_part_id)
        except FileNotFoundError:
            res = load_obj(self.obj_affordance_part_filedir, obj_part_id)
        return res

    def instantiate_affordance(self, affordance_data: OakInk2__Affordance):
        if not affordance_data.instantiated:
            if not affordance_data.has_model:
                affordance_data.obj_mesh = {}
                for obj_part_id in affordance_data.obj_part_id:
                    if obj_part_id not in self.obj_cache:
                        self.obj_cache[obj_part_id] = self._load_obj(obj_part_id)
                    obj_part_mesh = self.obj_cache[obj_part_id]
                    affordance_data.obj_mesh[obj_part_id] = obj_part_mesh
            else:
                obj_part_id = affordance_data.obj_id
                if obj_part_id not in self.obj_cache:
                    self.obj_cache[obj_part_id] = self._load_obj(obj_part_id)
                obj_part_mesh = self.obj_cache[obj_part_id]
                affordance_data.obj_mesh = obj_part_mesh
            affordance_data.instantiated = True
        return affordance_data

    def instantiate_primitive_task(
        self,
        primitive_task_data: OakInk2__PrimitiveTask,
        complex_task_data: Optional[OakInk2__ComplexTask] = None,
    ):
        if not primitive_task_data.instantiated:
            if complex_task_data is None or not complex_task_data.instantiated:
                complex_task_data = self.load_complex_task(primitive_task_data.seq_key, return_instantiated=True)
            # use frame_range_lh and frame_range_rh to quick index the tensor
            # and pad with zeros to be the same length with frame_range
            frame_range = primitive_task_data.frame_range
            frame_range_lh, frame_range_rh = primitive_task_data.frame_range_lh, primitive_task_data.frame_range_rh
            frame_list = list(range(frame_range[0], frame_range[1]))
            smplx_param, _ = tool.index_param(complex_task_data.smplx_param, frame_list)
            if frame_range_lh is not None:
                frame_list_lh = list(range(frame_range_lh[0], frame_range_lh[1]))
                l_pad = max(0, frame_range_lh[0] - frame_range[0])
                r_pad = max(0, frame_range[1] - frame_range_lh[1])
                lh_param, lh_in_range_mask = tool.index_param(
                    complex_task_data.lh_param, frame_list_lh, l_pad=l_pad, r_pad=r_pad
                )
            else:
                lh_param, lh_in_range_mask = tool.zero_param(complex_task_data.lh_param, len(frame_list))
            if frame_range_rh is not None:
                frame_list_rh = list(range(frame_range_rh[0], frame_range_rh[1]))
                l_pad = max(0, frame_range_rh[0] - frame_range[0])
                r_pad = max(0, frame_range[1] - frame_range_rh[1])
                rh_param, rh_in_range_mask = tool.index_param(
                    complex_task_data.rh_param, frame_list_rh, l_pad=l_pad, r_pad=r_pad
                )
            else:
                rh_param, rh_in_range_mask = tool.zero_param(complex_task_data.rh_param, len(frame_list))
            obj_transf = {}
            for obj_id in primitive_task_data.task_obj_list:
                if obj_id not in complex_task_data.obj_transf:
                    continue
                obj_transf[obj_id] = complex_task_data.obj_transf[obj_id][frame_list]
            # assignment
            primitive_task_data.scene_obj_list = complex_task_data.scene_obj_list
            primitive_task_data.smplx_param = smplx_param
            primitive_task_data.lh_param = lh_param
            primitive_task_data.rh_param = rh_param
            primitive_task_data.lh_in_range_mask = lh_in_range_mask
            primitive_task_data.rh_in_range_mask = rh_in_range_mask
            primitive_task_data.obj_transf = obj_transf
            # clean obj_list
            primitive_task_data.task_obj_list = [
                el for el in primitive_task_data.task_obj_list if el in primitive_task_data.obj_transf
            ]
            if primitive_task_data.lh_obj_list is not None:
                primitive_task_data.lh_obj_list = [
                    el for el in primitive_task_data.lh_obj_list if el in primitive_task_data.obj_transf
                ]
            if primitive_task_data.rh_obj_list is not None:
                primitive_task_data.rh_obj_list = [
                    el for el in primitive_task_data.rh_obj_list if el in primitive_task_data.obj_transf
                ]
            # conclude
            primitive_task_data.instantiated = True
        return primitive_task_data

    def instantiate_complex_task(self, complex_task_data: OakInk2__ComplexTask):
        if not complex_task_data.instantiated:
            # load annotation file
            anno_filepath = os.path.join(self.anno_prefix, f"{complex_task_data.seq_token}.pkl")
            with open(anno_filepath, "rb") as ifs:
                anno_data = pickle.load(ifs)
            # frame range
            mocap_frame_id_list = anno_data["mocap_frame_id_list"]
            frame_range = (min(mocap_frame_id_list), max(mocap_frame_id_list))
            complex_task_data.frame_range = frame_range
            # obj list
            obj_list = anno_data["obj_list"]
            complex_task_data.scene_obj_list = obj_list
            # smplx param
            smplx_param = self._collect_smplx(anno_data, mocap_frame_id_list)
            complex_task_data.smplx_param = smplx_param
            # left_hand param
            lh_param = self._collect_mano(anno_data, "lh", mocap_frame_id_list)
            complex_task_data.lh_param = lh_param
            # right hand param
            rh_param = self._collect_mano(anno_data, "rh", mocap_frame_id_list)
            complex_task_data.rh_param = rh_param
            # obj transf
            obj_transf = self._collect_obj_transf(anno_data, obj_list, mocap_frame_id_list)
            complex_task_data.obj_transf = obj_transf
            # conclude
            complex_task_data.instantiated = True
        return complex_task_data

    def _collect_smplx(self, anno_data, mocap_frame_id_list):
        smplx_handle = anno_data["raw_smplx"]
        smplx_key_list = list(next(iter(smplx_handle.values())).keys())
        smplx_param = {}
        for k in smplx_key_list:
            _param_tensor = []
            for fid in mocap_frame_id_list:
                _param_tensor.append(smplx_handle[fid][k])
            _param_tensor = torch.cat(_param_tensor, dim=0)
            smplx_param[k] = _param_tensor
        return smplx_param

    def _collect_mano(self, anno_data, hand_side, mocap_frame_id_list):
        mano_bh_handle = anno_data["raw_mano"]
        mano_bh_key_list = list(next(iter(mano_bh_handle.values())).keys())
        # only pick key prefixed with hand_side. also remove f"{hand_side}__" to get underlying key
        ori_key_list, key_list, key_prefix = [], [], f"{hand_side}__"
        for k in mano_bh_key_list:
            if k.startswith(key_prefix):
                ori_key_list.append(k)
                key_list.append(k[len(key_prefix) :])
        # collect res
        mano_param = {}
        for k, ori_key in zip(key_list, ori_key_list):
            _param_tensor = []
            for fid in mocap_frame_id_list:
                _param_tensor.append(mano_bh_handle[fid][ori_key])
            _param_tensor = torch.cat(_param_tensor, dim=0)
            mano_param[k] = _param_tensor
        return mano_param

    def _collect_obj_transf(self, anno_data, obj_list, mocap_frame_id_list):
        obj_transf_handle = anno_data["obj_transf"]
        res = {}
        for obj_id in obj_list:
            obj_transf_curr = obj_transf_handle[obj_id]
            _res = []
            for fid in mocap_frame_id_list:
                _res.append(obj_transf_curr[fid])
            _res = np.stack(_res, axis=0)
            res[obj_id] = _res
        return res

    def load_complex_task(self, seq_key, return_instantiated=None):
        if return_instantiated is None:
            return_instantiated = self.return_instantiated

        seq_token = seq_key.replace("/", "++")

        # preparation
        program_info_filepath = os.path.join(self.program_info_filedir, f"{seq_token}.json")
        program_info = load_program_info(program_info_filepath)
        affordance_task_namemap = program.suffix_affordance_primitive_segment(program_info)
        transient_task_namemap = program.suffix_transient_primitive_segment(program_info)
        _full_task_namemap = {**affordance_task_namemap, **transient_task_namemap}
        # reorder the tasks according to program_info
        full_task_namemap = {}
        for k in program_info.keys():
            full_task_namemap[k] = _full_task_namemap[k]
        rev_full_task_namemap = {v: ast.literal_eval(k) for k, v in full_task_namemap.items()}

        is_complex = len(affordance_task_namemap) > 0
        exec_path = list(full_task_namemap.values())
        exec_path_affordance = list(affordance_task_namemap.values())
        exec_range_map = rev_full_task_namemap
        with open(os.path.join(self.pdg_filedir, f"{seq_token}.json"), "r") as ifs:
            _g_info = json.load(ifs)
            _g_info = fix_g_info(_g_info, affordance_task_namemap)
            _g = nx.DiGraph()
            for k in _g_info["id_map"]:
                k = standardize_tuple(k)
                _g.add_node(affordance_task_namemap[k])
            _rev_id_map = {v: k for k, v in _g_info["id_map"].items()}
            for e in _g_info["e"]:
                _e_from, _e_to = e
                _seg_from, _seg_to = _rev_id_map[_e_from], _rev_id_map[_e_to]
                _seg_from, _seg_to = standardize_tuple(_seg_from), standardize_tuple(_seg_to)
                _g.add_edge(affordance_task_namemap[_seg_from], affordance_task_namemap[_seg_to])
        pdg = _g

        task_target = self.task_target[seq_key]
        # scene_desc & recipe
        _fpath = os.path.join(self.initial_condition_info_filedir, f"{seq_token}.json")
        if os.path.exists(_fpath):
            with open(_fpath, "r") as ifs:
                _i_info = json.load(ifs)
            scene_desc, recipe = _i_info["initial_condition"], _i_info["recipe"]
        else:
            scene_desc, recipe = None, None
        # frame_id
        if not return_instantiated:
            _fpath = os.path.join(self.program_extension_prefix, "frame_id", f"{seq_token}.pkl")
            if os.path.exists(_fpath):
                with open(_fpath, "rb") as ifs:
                    _fid_info = pickle.load(ifs)
                    frame_list = _fid_info["mocap_frame_id_list"]
                    frame_range = (min(frame_list), max(frame_list))
            else:
                frame_range = None
        else:
            frame_range = None
        # scene_obj_list
        if not return_instantiated:
            _fpath = os.path.join(self.program_extension_prefix, "obj_list", f"{seq_token}.json")
            scene_obj_list = try_load_json(_fpath)
        else:
            scene_obj_list = None

        res = OakInk2__ComplexTask(
            seq_key=seq_key,
            seq_token=seq_token,
            is_complex=is_complex,
            exec_path=exec_path,
            exec_path_affordance=exec_path_affordance,
            exec_range_map=exec_range_map,
            pdg=pdg,
            task_target=task_target,
            scene_desc=scene_desc,
            recipe=recipe,
            frame_range=frame_range,
            scene_obj_list=scene_obj_list,
        )
        if return_instantiated:
            self.instantiate_complex_task(res)
        return res

    def load_primitive_task(
        self, complex_task_data: OakInk2__ComplexTask, primitive_identifier=None, return_instantiated=None
    ):
        # if primitive_identifier is None, load all primitive tasks
        # else if it is a list, load the primitive tasks with the identifiers in the list
        # else if it is a string, load the primitive task with the identifier
        if return_instantiated is None:
            return_instantiated = self.return_instantiated
        # instantiate complex task if not already
        if return_instantiated and not complex_task_data.instantiated:
            self.instantiate_complex_task(complex_task_data)
        # determine load list
        if primitive_identifier is None:
            handle_list = complex_task_data.exec_path
        elif isinstance(primitive_identifier, str):
            handle_list = [primitive_identifier]
        else:
            handle_list = primitive_identifier

        # cache program info & desc info
        program_info_filepath = os.path.join(self.program_info_filedir, f"{complex_task_data.seq_token}.json")
        program_info = load_program_info(program_info_filepath)
        desc_info_filepath = os.path.join(self.desc_info_filedir, f"{complex_task_data.seq_token}.json")
        desc_info = load_desc_info(desc_info_filepath)

        res = []
        for p_ident in handle_list:
            p_res = self._load_primitive_task_from_identifier(
                complex_task_data.seq_key,
                p_ident,
                return_instantiated=return_instantiated,
                program_info=program_info,
                desc_info=desc_info,
            )
            if return_instantiated:
                p_res = self.instantiate_primitive_task(p_res, complex_task_data)
            res.append(p_res)
        if isinstance(primitive_identifier, str):
            return res[0]
        else:
            return res

    def _load_primitive_task_from_def(
        self,
        seq_key,
        frame_range_def,
        return_instantiated=None,
        program_info=None,
        desc_info=None,
    ):
        # internal method to load a primitive task *from ground up*
        if return_instantiated is None:
            return_instantiated = self.return_instantiated
        seq_token = seq_key.replace("/", "++")
        # preparation
        if program_info is None:
            program_info_filepath = os.path.join(self.program_info_filedir, f"{seq_token}.json")
            program_info = load_program_info(program_info_filepath)
        if desc_info is None:
            desc_info_filepath = os.path.join(self.desc_info_filedir, f"{seq_token}.json")
            desc_info = load_desc_info(desc_info_filepath)
        # get the program annotations
        frame_range_def_key = str(frame_range_def)
        program_item = program_info[frame_range_def_key]

        frame_range = program.frame_range_def_enclose(frame_range_def)
        frame_range_lh = frame_range_def[0]
        frame_range_rh = frame_range_def[1]

        primitive = program_item["primitive"]
        task_desc = desc_info[frame_range_def_key]["seg_desc"]
        transient = program.is_transient(primitive)

        hand_involved = program.determine_hand_involved(frame_range_def)
        interaction_mode = program_item["interaction_mode"]

        # scene_obj_list
        if not return_instantiated:
            _fpath = os.path.join(self.program_extension_prefix, "obj_list", f"{seq_token}.json")
            scene_obj_list = try_load_json(_fpath)
        else:
            scene_obj_list = None
        # task_obj_list...
        task_obj_list = program_item["obj_list"]
        lh_obj_list = program_item["obj_list_lh"]
        rh_obj_list = program_item["obj_list_rh"]
        res = OakInk2__PrimitiveTask(
            seq_key=seq_key,
            seq_token=seq_token,
            primitive_task=primitive,
            task_desc=task_desc,
            transient=transient,
            hand_involved=hand_involved,
            interaction_mode=interaction_mode,
            frame_range=frame_range,
            frame_range_lh=frame_range_lh,
            frame_range_rh=frame_range_rh,
            scene_obj_list=scene_obj_list,
            task_obj_list=task_obj_list,
            lh_obj_list=lh_obj_list,
            rh_obj_list=rh_obj_list,
        )
        return res

    def _load_primitive_task_from_identifier(
        self,
        seq_key,
        primitive_identifier,
        return_instantiated=None,
        program_info=None,
        desc_info=None,
    ):
        # internal method to load a primitive task *from ground up*
        if return_instantiated is None:
            return_instantiated = self.return_instantiated
        seq_token = seq_key.replace("/", "++")  # preparation
        if program_info is None:
            program_info_filepath = os.path.join(self.program_info_filedir, f"{seq_token}.json")
            program_info = load_program_info(program_info_filepath)
        affordance_task_namemap = program.suffix_affordance_primitive_segment(program_info)
        transient_task_namemap = program.suffix_transient_primitive_segment(program_info)
        _full_task_namemap = {**affordance_task_namemap, **transient_task_namemap}
        # reorder the tasks according to program_info
        full_task_namemap = {}
        for k in program_info.keys():
            full_task_namemap[k] = _full_task_namemap[k]
        rev_full_task_namemap = {v: ast.literal_eval(k) for k, v in full_task_namemap.items()}

        # handle primitive def
        frame_range_def = rev_full_task_namemap[primitive_identifier]
        # load
        return self._load_primitive_task_from_def(
            seq_key=seq_key,
            frame_range_def=frame_range_def,
            return_instantiated=return_instantiated,
            program_info=program_info,
            desc_info=desc_info,
        )

    def load_affordance(self, obj_id, return_instantiated=None):
        if return_instantiated is None:
            return_instantiated = self.return_instantiated

        if obj_id in self.obj_desc:
            obj_name = self.obj_desc[obj_id]["obj_name"]
        else:
            obj_name = self.part_desc[obj_id]["obj_name"]
        has_model = self.object_affordance[obj_id]["has_model"]
        obj_instance_id = get_part_tree_root(self.rev_part_tree, obj_id)
        obj_part_id = self.part_tree[obj_id]

        affordance_list = self.object_affordance[obj_id]["affordance"]
        affordance_instantiation_list = self.object_affordance[obj_id]["affordance_instantiation"]

        res = OakInk2__Affordance(
            obj_id=obj_id,
            obj_name=obj_name,
            has_model=has_model,
            obj_instance_id=obj_instance_id,
            obj_part_id=obj_part_id,
            affordance_list=affordance_list,
            affordance_instantiation_list=affordance_instantiation_list,
        )
        if return_instantiated:
            self.instantiate_affordance(res)
        return res

    def load_affordance_part(self, affordance_data, return_instantiated=None):
        if return_instantiated is None:
            return_instantiated = self.return_instantiated

        if affordance_data.is_part:
            if return_instantiated:
                affordance_data = self.instantiate_affordance(affordance_data)
            return [affordance_data]
        else:
            # load each part
            res = []
            for obj_id in affordance_data.obj_part_id:
                part_data = self.load_affordance(obj_id, return_instantiated=return_instantiated)
                res.append(part_data)
            return res
