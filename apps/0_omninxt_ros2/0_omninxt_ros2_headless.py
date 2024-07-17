#!/usr/bin/env python
# -----------------------------------
# The actual script should start here
# -----------------------------------
import sys, os
BASE_DIR = "/omninxt-sim"
sys.path.append(os.path.join(BASE_DIR, 'PegasusSimulator/extensions/pegasus.simulator'))

import omni.timeline
from omni.isaac.core.world import World
from omni.isaac.core.utils.extensions import disable_extension, enable_extension

# Enable/disable ROS bridge extensions to keep only ROS2 Bridge
disable_extension("omni.isaac.ros_bridge")
enable_extension("omni.isaac.ros2_bridge")

# Import the Pegasus API for simulating drones
from pegasus.simulator.params import ROBOTS, SIMULATION_ENVIRONMENTS
from pegasus.simulator.logic.state import State
from pegasus.simulator.logic.backends.ros2_backend import ROS2Backend
from pegasus.simulator.logic.graphs import ROS2Camera
from pegasus.simulator.logic.vehicles.multirotor import Multirotor, MultirotorConfig
from pegasus.simulator.logic.interface.pegasus_interface import PegasusInterface

# Auxiliary scipy and numpy modules
from scipy.spatial.transform import Rotation

class PegasusApp:
    """
    A Template class that serves as an example on how to build a simple Isaac Sim standalone App.
    """

    def __init__(self):
        """
        Method that initializes the PegasusApp and is used to setup the simulation environment.
        """

        # Acquire the timeline that will be used to start/stop the simulation
        self.timeline = omni.timeline.get_timeline_interface()

        # Start the Pegasus Interface
        self.pg = PegasusInterface()

        # Acquire the World, .i.e, the singleton that controls that is a one stop shop for setting up physics, 
        # spawning asset primitives, etc.
        self.pg._world = World(**self.pg._world_settings)
        self.world = self.pg.world

        # Launch one of the worlds provided by NVIDIA
        self.pg.load_environment(SIMULATION_ENVIRONMENTS["Curved Gridroom"])

        # Create the vehicle
        # Try to spawn the selected robot in the world to the specified namespace
        config_multirotor = MultirotorConfig()
        config_multirotor.backends = [ROS2Backend(vehicle_id=1, config={"namespace": 'drone'})]
        config_multirotor.graphs = [
            ROS2Camera("/vehicle/body/OmniCam/cam_0", config={"types": ['rgb', 'camera_info', 'depth_pcl', 'depth'], "namespace": 'omninxt0', "topic": 'cam_0', "tf_frame_id": 'map', 'resolution': [1280, 960]}),
            ROS2Camera("/vehicle/body/OmniCam/cam_1", config={"types": ['rgb', 'camera_info', 'depth_pcl', 'depth'], "namespace": 'omninxt0', "topic": 'cam_1', "tf_frame_id": 'map', 'resolution': [1280, 960]}),
            ROS2Camera("/vehicle/body/OmniCam/cam_2", config={"types": ['rgb', 'camera_info', 'depth_pcl', 'depth'], "namespace": 'omninxt0', "topic": 'cam_2', "tf_frame_id": 'map', 'resolution': [1280, 960]}),
            ROS2Camera("/vehicle/body/OmniCam/cam_3", config={"types": ['rgb', 'camera_info', 'depth_pcl', 'depth'], "namespace": 'omninxt0', "topic": 'cam_3', "tf_frame_id": 'map', 'resolution': [1280, 960]})]

        Multirotor(
            "/World/quadrotor",
            ROBOTS['OmniNxt'],
            0,
            [0.0, 0.0, 0.07],
            Rotation.from_euler("XYZ", [0.0, 0.0, 0.0], degrees=True).as_quat(),
            config=config_multirotor,
        )

        # Reset the simulation environment so that all articulations (aka robots) are initialized
        self.world.reset()

        # Auxiliar variable for the timeline callback example
        self.stop_sim = False

    def run(self):
        """
        Method that implements the application main loop, where the physics steps are executed.
        """

        # Start the simulation
        self.timeline.play()

        # The "infinite" loop
        while not self.stop_sim:
            # Update the UI of the app and perform the physics step
            self.world.step(render=True)

        # Cleanup and stop
        self.timeline.stop()

def main():

    # Instantiate the template app
    pg_app = PegasusApp()

    # Run the application loop
    pg_app.run()

if __name__ == "__main__":
    main()
