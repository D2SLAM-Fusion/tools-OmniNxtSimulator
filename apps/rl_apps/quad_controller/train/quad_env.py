# Copyright (c) 2022-2024, The Isaac Lab Project Developers.
# All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause

from __future__ import annotations

import gymnasium as gym
import torch

import omni.isaac.lab.sim as sim_utils
from omni.isaac.lab.assets import Articulation, ArticulationCfg
from omni.isaac.lab.envs import DirectRLEnv, DirectRLEnvCfg
from omni.isaac.lab.envs.ui import BaseEnvWindow
from omni.isaac.lab.markers import VisualizationMarkers
from omni.isaac.lab.scene import InteractiveSceneCfg
from omni.isaac.lab.sim import SimulationCfg
from omni.isaac.lab.terrains import TerrainImporterCfg
from omni.isaac.lab.utils import configclass
from omni.isaac.lab.utils.math import subtract_frame_transforms

##
# Pre-defined configs
##
from omni.isaac.lab_assets import CRAZYFLIE_CFG  # isort: skip
from omni.isaac.lab.markers import CUBOID_MARKER_CFG, GREEN_ARROW_X_MARKER_CFG, BLUE_ARROW_X_MARKER_CFG  # isort: skip
from vehicles.omninxt import OmniNxt

OmniNxt = OmniNxt(id=0, init_pos=(0.0, 0.0, 2), enable_cameras=True)


class QuadcopterEnvWindow(BaseEnvWindow):
    """Window manager for the Quadcopter environment."""

    def __init__(self, env: QuadcopterWithYawEnv, window_name: str = "IsaacLab"):
        """Initialize the window.

        Args:
            env: The environment object.
            window_name: The name of the window. Defaults to "IsaacLab".
        """
        # initialize base window
        super().__init__(env, window_name)
        # add custom UI elements
        with self.ui_window_elements["main_vstack"]:
            with self.ui_window_elements["debug_frame"]:
                with self.ui_window_elements["debug_vstack"]:
                    # add command manager visualization
                    self._create_debug_vis_ui_element("targets", self.env)


@configclass
class QuadcopterEnvCfg(DirectRLEnvCfg):
    # env
    episode_length_s = 10.0
    decimation = 2
    action_space = 4
    observation_space = 16
    state_space = 0
    debug_vis = True

    ui_window_class_type = QuadcopterEnvWindow

    # simulation
    sim: SimulationCfg = SimulationCfg(
        dt=1 / 100,
        render_interval=decimation,
        disable_contact_processing=True,
        physics_material=sim_utils.RigidBodyMaterialCfg(
            friction_combine_mode="multiply",
            restitution_combine_mode="multiply",
            static_friction=1.0,
            dynamic_friction=1.0,
            restitution=0.0,
        ),
    )
    terrain = TerrainImporterCfg(
        prim_path="/World/ground",
        terrain_type="plane",
        collision_group=-1,
        physics_material=sim_utils.RigidBodyMaterialCfg(
            friction_combine_mode="multiply",
            restitution_combine_mode="multiply",
            static_friction=1.0,
            dynamic_friction=1.0,
            restitution=0.0,
        ),
        debug_vis=False
    )

    # scene
    scene: InteractiveSceneCfg = InteractiveSceneCfg(num_envs=32, env_spacing=5, replicate_physics=True)

    # robot
    robot: ArticulationCfg = CRAZYFLIE_CFG.replace(prim_path="/World/envs/env_.*/Robot")
    # robot: ArticulationCfg = OmniNxt.ISAAC_SIM_CFG.replace(prim_path="/World/envs/env_.*/OmniNxt")
    thrust_to_weight = 1.9
    moment_scale = 0.01

    # reward scales
    lin_vel_reward_scale = -0.01
    ang_vel_reward_scale = -0.002
    distance_to_goal_reward_scale = 30.0
    radial_to_goal_yaw_reward_scale = 5.0


class QuadcopterWithYawEnv(DirectRLEnv):
    cfg: QuadcopterEnvCfg

    def __init__(self, cfg: QuadcopterEnvCfg, render_mode: str | None = None, **kwargs):
        super().__init__(cfg, render_mode, **kwargs)

        # Total thrust and moment applied to the base of the quadcopter
        self._actions = torch.zeros(self.num_envs, gym.spaces.flatdim(self.single_action_space), device=self.device)
        self._thrust = torch.zeros(self.num_envs, 1, 3, device=self.device)
        self._moment = torch.zeros(self.num_envs, 1, 3, device=self.device)

        # Goal position
        self._desired_pos_w = torch.zeros(self.num_envs, 3, device=self.device)

        # Goal yaw radians in world frame (yaw is around the z-axis, -pi to pi)
        self._desired_yaw_w = torch.zeros(self.num_envs, 1, device=self.device)

        # Convert yaw to quaternion representation
        # Quaternion (w, x, y, z) for yaw rotation around z-axis
        self._desired_yaw_quat = torch.zeros(self.num_envs, 4, device=self.device)
        self._desired_yaw_quat[:, 0] = torch.cos(self._desired_yaw_w[:, 0] / 2)  # w
        self._desired_yaw_quat[:, 3] = torch.sin(self._desired_yaw_w[:, 0] / 2)  # z

        # Logging
        self._episode_sums = {
            key: torch.zeros(self.num_envs, dtype=torch.float, device=self.device)
            for key in [
                "lin_vel",
                "ang_vel",
                "distance_to_goal_pose",
                "radial_to_goal_yaw",
            ]
        }
        # Get specific body indices
        self._body_id = self._robot.find_bodies("body")[0]
        self._robot_mass = self._robot.root_physx_view.get_masses()[0].sum()
        self._gravity_magnitude = torch.tensor(self.sim.cfg.gravity, device=self.device).norm()
        self._robot_weight = (self._robot_mass * self._gravity_magnitude).item()

        # add handle for debug visualization (this is set to a valid handle inside set_debug_vis)
        self.set_debug_vis(self.cfg.debug_vis)

    def _setup_scene(self):
        self._robot = Articulation(self.cfg.robot)
        self.scene.articulations["robot"] = self._robot

        self.cfg.terrain.num_envs = self.scene.cfg.num_envs
        self.cfg.terrain.env_spacing = self.scene.cfg.env_spacing
        self._terrain = self.cfg.terrain.class_type(self.cfg.terrain)
        # clone, filter, and replicate
        self.scene.clone_environments(copy_from_source=False)
        self.scene.filter_collisions(global_prim_paths=[self.cfg.terrain.prim_path])
        # add lights
        light_cfg = sim_utils.DomeLightCfg(intensity=2000.0, color=(0.75, 0.75, 0.75))
        light_cfg.func("/World/Light", light_cfg)

    def _pre_physics_step(self, actions: torch.Tensor):
        self._actions = actions.clone().clamp(-1.0, 1.0)
        self._thrust[:, 0, 2] = self.cfg.thrust_to_weight * self._robot_weight * (self._actions[:, 0] + 1.0) / 2.0
        self._moment[:, 0, :] = self.cfg.moment_scale * self._actions[:, 1:]

    def _apply_action(self):
        self._robot.set_external_force_and_torque(self._thrust, self._moment, body_ids=self._body_id)

    def _get_observations(self) -> dict:
        desired_pos_b, desired_yaw_b = subtract_frame_transforms(
            self._robot.data.root_state_w[:, :3], self._robot.data.root_state_w[:, 3:7], self._desired_pos_w, self._desired_yaw_quat
        )
        obs = torch.cat(
            [
                self._robot.data.root_lin_vel_b,
                self._robot.data.root_ang_vel_b,
                self._robot.data.projected_gravity_b,
                desired_pos_b,
                desired_yaw_b,
            ],
            dim=-1,
        )
        observations = {"policy": obs}
        return observations

    def _get_rewards(self) -> torch.Tensor:
        # Calculate linear velocity reward
        lin_vel = torch.sum(torch.square(self._robot.data.root_lin_vel_b), dim=1)

        # Calculate angular velocity reward
        ang_vel = torch.sum(torch.square(self._robot.data.root_ang_vel_b), dim=1)

        # Calculate distance to goal pose
        distance_to_goal_pose = torch.linalg.norm(self._desired_pos_w - self._robot.data.root_pos_w, dim=1)
        distance_to_goal_pose_mapped = 1 - torch.tanh(distance_to_goal_pose / 0.8)

        # Calculate quaternion difference
        desired_quat = self._desired_yaw_quat  # Shape: (num_envs, 4)
        current_quat = self._robot.data.root_quat_w  # Shape: (num_envs, 4)
        current_quat_conj = current_quat.clone()
        current_quat_conj[:, 1:] = -current_quat_conj[:, 1:]  # Negate the vector part (x, y, z)
        # Compute the quaternion difference (desired_quat * current_quat_conj)
        quat_diff = torch.stack([
            desired_quat[:, 0] * current_quat_conj[:, 0] - desired_quat[:, 1] * current_quat_conj[:, 1] - desired_quat[:, 2] * current_quat_conj[:, 2] - desired_quat[:, 3] * current_quat_conj[:, 3],
            desired_quat[:, 0] * current_quat_conj[:, 1] + desired_quat[:, 1] * current_quat_conj[:, 0] + desired_quat[:, 2] * current_quat_conj[:, 3] - desired_quat[:, 3] * current_quat_conj[:, 2],
            desired_quat[:, 0] * current_quat_conj[:, 2] - desired_quat[:, 1] * current_quat_conj[:, 3] + desired_quat[:, 2] * current_quat_conj[:, 0] + desired_quat[:, 3] * current_quat_conj[:, 1],
            desired_quat[:, 0] * current_quat_conj[:, 3] + desired_quat[:, 1] * current_quat_conj[:, 2] - desired_quat[:, 2] * current_quat_conj[:, 1] + desired_quat[:, 3] * current_quat_conj[:, 0]
        ], dim=1)
        quat_diff = quat_diff / torch.linalg.norm(quat_diff, dim=1, keepdim=True)

        # Calculate angular difference from quaternion
        angular_diff = 2 * torch.acos(torch.clamp(quat_diff[:, 0], -1.0, 1.0))
        angular_diff = torch.min(angular_diff, 2 * torch.pi - angular_diff)

        # Reward based on angular difference
        radial_to_goal_yaw = angular_diff

        # Define rewards with scaling factors and time step
        rewards = {
            "lin_vel": lin_vel * self.cfg.lin_vel_reward_scale * self.step_dt,
            "ang_vel": ang_vel * self.cfg.ang_vel_reward_scale * self.step_dt,
            "distance_to_goal_pose": distance_to_goal_pose_mapped * self.cfg.distance_to_goal_reward_scale * self.step_dt,
            "radial_to_goal_yaw": (1 - radial_to_goal_yaw / torch.pi) * self.cfg.radial_to_goal_yaw_reward_scale * self.step_dt,
        }
        # Sum all rewards
        reward = torch.sum(torch.stack(list(rewards.values())), dim=0)

        # Logging rewards for the episode
        for key, value in rewards.items():
            self._episode_sums[key] += value

        return reward

    def _get_dones(self) -> tuple[torch.Tensor, torch.Tensor]:
        time_out = self.episode_length_buf >= self.max_episode_length - 1
        died = torch.logical_or(self._robot.data.root_pos_w[:, 2] < 0.1, self._robot.data.root_pos_w[:, 2] > 2.0)
        return died, time_out

    def _reset_idx(self, env_ids: torch.Tensor | None):
        if env_ids is None or len(env_ids) == self.num_envs:
            env_ids = self._robot._ALL_INDICES

        # Logging
        final_distance_to_goal = torch.linalg.norm(
            self._desired_pos_w[env_ids] - self._robot.data.root_pos_w[env_ids], dim=1
        ).mean()
        # Calculate quaternion difference
        desired_quat = self._desired_yaw_quat  # Shape: (num_envs, 4)
        current_quat = self._robot.data.root_quat_w  # Shape: (num_envs, 4)
        current_quat_conj = current_quat.clone()
        current_quat_conj[:, 1:] = -current_quat_conj[:, 1:]  # Negate the vector part (x, y, z)
        # Compute the quaternion difference (desired_quat * current_quat_conj)
        quat_diff = torch.stack([
            desired_quat[:, 0] * current_quat_conj[:, 0] - desired_quat[:, 1] * current_quat_conj[:, 1] - desired_quat[:, 2] * current_quat_conj[:, 2] - desired_quat[:, 3] * current_quat_conj[:, 3],
            desired_quat[:, 0] * current_quat_conj[:, 1] + desired_quat[:, 1] * current_quat_conj[:, 0] + desired_quat[:, 2] * current_quat_conj[:, 3] - desired_quat[:, 3] * current_quat_conj[:, 2],
            desired_quat[:, 0] * current_quat_conj[:, 2] - desired_quat[:, 1] * current_quat_conj[:, 3] + desired_quat[:, 2] * current_quat_conj[:, 0] + desired_quat[:, 3] * current_quat_conj[:, 1],
            desired_quat[:, 0] * current_quat_conj[:, 3] + desired_quat[:, 1] * current_quat_conj[:, 2] - desired_quat[:, 2] * current_quat_conj[:, 1] + desired_quat[:, 3] * current_quat_conj[:, 0]
        ], dim=1)
        quat_diff = quat_diff / torch.linalg.norm(quat_diff, dim=1, keepdim=True)
        # Calculate angular difference from quaternion
        angular_diff = 2 * torch.acos(torch.clamp(quat_diff[:, 0], -1.0, 1.0))
        angular_diff = torch.min(angular_diff, 2 * torch.pi - angular_diff)
        final_yaw_diff = angular_diff
        extras = dict()
        for key in self._episode_sums.keys():
            episodic_sum_avg = torch.mean(self._episode_sums[key][env_ids])
            extras["Episode_Reward/" + key] = episodic_sum_avg / self.max_episode_length_s
            self._episode_sums[key][env_ids] = 0.0
        self.extras["log"] = dict()
        self.extras["log"].update(extras)
        extras = dict()
        extras["Episode_Termination/died"] = torch.count_nonzero(self.reset_terminated[env_ids]).item()
        extras["Episode_Termination/time_out"] = torch.count_nonzero(self.reset_time_outs[env_ids]).item()
        extras["Metrics/final_distance_to_goal"] = final_distance_to_goal.item()
        extras["Metrics/final_yaw_diff"] = final_yaw_diff.mean().item()
        self.extras["log"].update(extras)

        self._robot.reset(env_ids)
        super()._reset_idx(env_ids)
        if len(env_ids) == self.num_envs:
            # Spread out the resets to avoid spikes in training when many environments reset at a similar time
            self.episode_length_buf = torch.randint_like(self.episode_length_buf, high=int(self.max_episode_length))

        self._actions[env_ids] = 0.0
        # Sample new commands
        self._desired_pos_w[env_ids, :2] = torch.zeros_like(self._desired_pos_w[env_ids, :2]).uniform_(-3.0, 3.0)
        self._desired_pos_w[env_ids, :2] += self._terrain.env_origins[env_ids, :2]
        self._desired_pos_w[env_ids, 2] = torch.zeros_like(self._desired_pos_w[env_ids, 2]).uniform_(0.5, 3.0)
        self._desired_yaw_w[env_ids, 0] = torch.zeros_like(self._desired_yaw_w[env_ids, 0]).uniform_(0, 6.28)
        self._desired_yaw_quat[env_ids, 0] = torch.cos(self._desired_yaw_w[env_ids, 0] / 2)
        self._desired_yaw_quat[env_ids, 3] = torch.sin(self._desired_yaw_w[env_ids, 0] / 2)

        # Reset robot state
        joint_pos = self._robot.data.default_joint_pos[env_ids]
        joint_vel = self._robot.data.default_joint_vel[env_ids]
        default_root_state = self._robot.data.default_root_state[env_ids]
        default_root_state[:, :3] += self._terrain.env_origins[env_ids]
        self._robot.write_root_pose_to_sim(default_root_state[:, :7], env_ids)
        self._robot.write_root_velocity_to_sim(default_root_state[:, 7:], env_ids)
        self._robot.write_joint_state_to_sim(joint_pos, joint_vel, None, env_ids)

    def _set_debug_vis_impl(self, debug_vis: bool):
        # create markers if necessary for the first tome
        if debug_vis:
            if not hasattr(self, "goal_pos_visualizer"):
                marker_cfg = CUBOID_MARKER_CFG.copy()
                marker_cfg.markers["cuboid"].size = (0.05, 0.05, 0.05)
                # -- goal pose
                marker_cfg.prim_path = "/Visuals/Command/goal_position"
                self.goal_pos_visualizer = VisualizationMarkers(marker_cfg)
                print("Created goal_pos_visualizer")
            # set their visibility to true
            self.goal_pos_visualizer.set_visibility(True)

            if not hasattr(self, "goal_yaw_visualizer"):
                goal_arrow_cfg = GREEN_ARROW_X_MARKER_CFG.copy()
                goal_arrow_cfg.markers["arrow"].scale = (0.05, 0.05, 0.2)
                # -- goal yaw
                goal_arrow_cfg.prim_path = "/Visuals/Command/goal_yaw"
                self.goal_yaw_visualizer = VisualizationMarkers(goal_arrow_cfg)
                print("Created goal_yaw_visualizer")
            # set their visibility to true
            self.goal_yaw_visualizer.set_visibility(True)

            if not hasattr(self, "current_yaw_visualizer"):
                current_arrow_cfg = BLUE_ARROW_X_MARKER_CFG.copy()
                current_arrow_cfg.markers["arrow"].scale = (0.05, 0.05, 0.2)
                # -- current yaw
                current_arrow_cfg.prim_path = "/Visuals/Command/current_yaw"
                self.current_yaw_visualizer = VisualizationMarkers(current_arrow_cfg)
                print("Created current_yaw_visualizer")
            # set their visibility to true
            self.current_yaw_visualizer.set_visibility(True)
        else:
            if hasattr(self, "goal_pos_visualizer"):
                self.goal_pos_visualizer.set_visibility(False)
            if hasattr(self, "goal_yaw_visualizer"):
                self.goal_yaw_visualizer.set_visibility(False)
            if hasattr(self, "current_yaw_visualizer"):
                self.current_yaw_visualizer.set_visibility(False)

    def _debug_vis_callback(self, event):
        # update the markers
        self.goal_pos_visualizer.visualize(self._desired_pos_w)
        self.goal_yaw_visualizer.visualize(self._desired_pos_w, self._desired_yaw_quat)
        self.current_yaw_visualizer.visualize(self._robot.data.root_pos_w, self._robot.data.root_quat_w)
