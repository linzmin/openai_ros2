import numpy
import math
from typing import Dict
import forward_kinematics_py as fk
from openai_ros2.utils import ut_launch, ut_gazebo
from openai_ros2.robots import LobotArmSim
from ament_index_python.packages import get_package_share_directory
from gym.spaces import Box
import os
import rclpy


class LobotArmRandomGoal:
    def __init__(self, node: rclpy.node.Node, robot, task_kwargs: Dict = None, max_time_step: int = 500):
        if task_kwargs is None:
            task_kwargs = dict()
        self.node = node
        self.robot = robot
        self.accepted_dist_to_bounds = task_kwargs.get('accepted_dist_to_bounds', 0.001)
        self.accepted_error = task_kwargs.get('accepted_error', 0.001)
        self.reach_target_bonus_reward = task_kwargs.get('reach_target_bonus_reward', 0.0)
        self.reach_bounds_penalty = task_kwargs.get('reach_bounds_penalty', 0.0)
        self.contact_penalty = task_kwargs.get('contact_penalty', 0.0)
        print(f'------Setting task parameters------')
        print('accepted_dist_to_bounds: %f   # Allowable distance to joint limits' % self.accepted_dist_to_bounds)
        print('accepted_error: %f            # Allowable distance from target coordinates' % self.accepted_error)
        print('reach_target_bonus_reward: %f # Bonus reward upon reaching target' % self.reach_target_bonus_reward)
        print('reach_bounds_penalty: %f      # Reward penalty when reaching joint limit' % self.reach_bounds_penalty)
        print('contact_penalty: %f           # Reward penalty for collision' % self.contact_penalty)
        print(f'-----------------------------------')

        if self.accepted_dist_to_bounds < 0.0:
            raise Exception("Allowable distance to joint limits should be positive")

        if self.accepted_error < 0.0:
            raise Exception("Accepted error to end coordinates should be positive")

        if self.reach_target_bonus_reward < 0.0:
            raise Exception("Reach target bonus reward should be positive")

        if self.contact_penalty < 0.0:
            raise Exception("Contact penalty should be positive")

        if self.reach_bounds_penalty < 0.0:
            raise Exception("Reach bounds penalty should be positive")

        self._max_time_step = max_time_step
        lobot_desc_share_path = get_package_share_directory('lobot_description')
        arm_urdf_path = os.path.join(lobot_desc_share_path, "robots/arm_standalone.urdf")
        self._fk = fk.ForwardKinematics(arm_urdf_path)

        self.target_coords = self.__generate_target_coords()
        target_x = self.target_coords[0]
        target_y = self.target_coords[1]
        target_z = self.target_coords[2]
        if isinstance(robot, LobotArmSim):  # Check if is gazebo
            # Spawn the target marker if it is gazebo
            print(f'Spawning to: {(target_x, target_y, target_z)}')
            spawn_success = ut_gazebo.create_marker(node, target_x, target_y, target_z, diameter=0.004)
        self.previous_coords = numpy.array([0.0, 0.0, 0.0])

    def is_done(self, joint_states: numpy.ndarray, contact_count: int, observation_space: Box, time_step: int = -1) -> bool:
        # If there is any contact (collision), we consider the episode done
        if contact_count > 0:
            return True

        current_coords = self.__get_coords(joint_states)

        # Highest done priority is if time step exceeds limit, so we check this first
        if time_step > self._max_time_step:
            return True

        # Check that joint values are not approaching limits
        upper_bound = observation_space.high[:3]  # First 3 values are the joint states
        lower_bound = observation_space.low[:3]
        min_dist_to_upper_bound = min(abs(joint_states - upper_bound))
        min_dist_to_lower_bound = min(abs(joint_states - lower_bound))
        # self.accepted_dist_to_bounds is basically how close to the joint limits can the joints go,
        # i.e. limit of 1.57 with accepted dist of 0.1, then the joint can only go until 1.47
        if min_dist_to_lower_bound < self.accepted_dist_to_bounds:
            joint_index = abs(joint_states - lower_bound).argmin()
            print(f'Joint {joint_index} approach joint limits, '
                  f'current joint value: {joint_states[joint_index]}, '
                  f'minimum joint value: {lower_bound[joint_index]}')
            return True
        if min_dist_to_upper_bound < self.accepted_dist_to_bounds:
            joint_index = abs(joint_states - upper_bound).argmin()
            print(f'Joint {joint_index} approach joint limits, '
                  f'current joint value: {joint_states[joint_index]}, '
                  f'maximum joint value: {upper_bound[joint_index]}')
            return True

        # If time step still within limits, as long as any coordinate is out of acceptance range, we are not done
        for i in range(3):
            if abs(self.target_coords[i] - current_coords[i]) > self.accepted_error:
                return False
        # If all coordinates within acceptance range AND time step within limits, we are done
        print(f"Reached destination, target coords: {self.target_coords}, current coords: {current_coords}")
        return True

    def compute_reward(self, joint_states: numpy.ndarray, contact_count: int, observation_space: Box) -> float:
        if len(joint_states) != 3:
            print(f"Expected 3 values for joint states, but got {len(joint_states)} values instead")
            return -1
        coords_get_result = self.__get_coords(joint_states)
        if len(coords_get_result) != 3:
            print(f"Expected 3 values after getting coordinates, but got {len(coords_get_result)} values instead")
            return -1
        current_coords = coords_get_result

        # Give 0 reward on initial state
        if numpy.array_equal(self.previous_coords, numpy.array([0.0, 0.0, 0.0])):
            # print("Initial state detected, giving 0 reward")
            reward = 0.0
        else:
            reward = self.__calc_dist_change(self.previous_coords, current_coords)

        # Scale up reward so that it is not so small
        reward *= 100
        self.previous_coords = current_coords

        # Reward shaping logic

        # Check if it has reached target destination
        # If any coords out of acceptance range, we set reached to False, else it is True
        reached_destination = True
        for i in range(3):
            if abs(self.target_coords[i] - current_coords[i]) > self.accepted_error:
                reached_destination = False
                break
        if reached_destination:
            reward += self.reach_target_bonus_reward

        # Check if it has approached any joint limits
        upper_bound = observation_space.high[:3]  # First 3 values are the joint states
        lower_bound = observation_space.low[:3]
        min_dist_to_upper_bound = min(abs(joint_states - upper_bound))
        min_dist_to_lower_bound = min(abs(joint_states - lower_bound))
        # self.accepted_dist_to_bounds is basically how close to the joint limits can the joints go,
        # i.e. limit of 1.57 with accepted dist of 0.1, then the joint can only go until 1.47
        if (min_dist_to_lower_bound < self.accepted_dist_to_bounds) or \
                (min_dist_to_upper_bound < self.accepted_dist_to_bounds):  # when reached the joint limit
            reward -= self.reach_bounds_penalty

        # Check if it has any contacts
        if contact_count > 0:
            reward -= self.contact_penalty

        return reward

    def reset(self):
        self.previous_coords = numpy.array([0.0, 0.0, 0.0])
        self.target_coords = self.__generate_target_coords()
        if isinstance(self.robot, LobotArmSim):  # Check if is simulator or real
            # Move the target marker if it is gazebo
            print(f'Moving to {(self.target_coords[0], self.target_coords[1], self.target_coords[2])}')
            spawn_success = ut_gazebo.create_marker(self.node, self.target_coords[0],
                                                    self.target_coords[1], self.target_coords[2], diameter=0.004)

    def __calc_dist_change(self, coords_init: numpy.ndarray,
                           coords_next: numpy.ndarray) -> float:
        # Efficient euclidean distance calculation by numpy, most likely uses SIMD
        diff_abs_init = numpy.linalg.norm(coords_init - self.target_coords)
        diff_abs_next = numpy.linalg.norm(coords_next - self.target_coords)

        return diff_abs_init - diff_abs_next

    def __get_coords(self, joint_states: numpy.ndarray) -> numpy.ndarray:
        if len(joint_states) != 3:
            print(f"Expected 3 values for joint states, but got {len(joint_states)} values instead")
            return numpy.array([0.0, 0.0, 0.0])

        res = self._fk.calculate('world', 'grip_end_point', joint_states)
        return numpy.array([res.translation.x, res.translation.y, res.translation.z])

    def __generate_target_coords(self) -> numpy.ndarray:
        while True :
            random_joint_values = numpy.random.uniform([-2.3562, -1.5708, -1.5708], [2.3562, 0.5, 1.5708])
            res = self._fk.calculate('world', 'grip_end_point', random_joint_values)
            if res.translation.z > 0.0:
                break
        target_coords = numpy.array([res.translation.x, res.translation.y, res.translation.z])
        return target_coords

